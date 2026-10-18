"""
BizHawk MCP Server
Model Context Protocol server for Nintendo DS game debugging with BizHawk/EmuHawk (MelonDS core)

Tools act on the running emulator. A few also touch files on the host:
save_state (writes savestates), and raw_command actions such as
screenshot.take, memory.dump_to_file and system.open_rom. Each tool carries
MCP annotations (readOnlyHint / destructiveHint) saying which kind it is.

Works with both the mcp 1.x (decorator) and 2.x (constructor callback)
low-level server APIs; see create_server().
"""

import json
import os
import sys
import time
import logging
import logging.handlers
import threading
import asyncio

# Setup logging - write to stderr AND file
LOG_DIR = os.path.dirname(os.path.abspath(__file__))
LOG_FILE = os.path.join(LOG_DIR, "bizhawk_mcp.log")

# Configure logging
logging.basicConfig(
    level=logging.DEBUG,
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[
        logging.handlers.RotatingFileHandler(LOG_FILE, maxBytes=5_000_000, backupCount=2, encoding='utf-8'),
        logging.StreamHandler(sys.stderr)
    ]
)
logger = logging.getLogger("bizhawk_mcp")

logger.info("=" * 50)
logger.info("BizHawk MCP Server starting...")
logger.info(f"Log file: {LOG_FILE}")

# MCP imports
from mcp.server import Server
from mcp.types import Tool, TextContent, ToolAnnotations, CallToolResult, ListToolsResult
from mcp.server.stdio import stdio_server
logger.info("MCP package imported successfully")

# Configuration
BASE_PATH = os.path.dirname(os.path.abspath(__file__))
COMMAND_FILE = os.path.join(BASE_PATH, "debug_commands.json")
STATE_FILE = os.path.join(BASE_PATH, "debug_state.json")
RESPONSE_FILE = os.path.join(BASE_PATH, "debug_response.json")

logger.info(f"Base path: {BASE_PATH}")
logger.info(f"Command file: {COMMAND_FILE}")
logger.info(f"State file: {STATE_FILE}")

# Command ID counter. Seeded from the clock so ids from a restarted server
# never repeat the previous session's: the Lua file poller skips a command
# whose id equals the last one it ran, and a stale debug_response.json with
# a matching commandId would otherwise be taken as this command's reply.
command_id = int(time.time() * 1000) % 1_000_000_000

# Timeout configuration - BizHawk is more reliable, can use shorter timeouts
DEFAULT_TIMEOUT = 30.0  # seconds (file-fallback). Must cover a full batched
# input.sequence navigation (with multi-second wall-clock sleeps).
FAST_TIMEOUT = 5.0      # for simple operations

# The bridge runs one command at a time; tool calls run in worker threads
# (so the asyncio loop stays responsive) and take this lock.
_bridge_lock = threading.Lock()

# Optional region params shared by search / snapshot (Lua resolveRegion)
REGION_PROPERTIES = {
    "domain": {"type": "string", "description": "Memory domain (optional, default='ARM9 System Bus')"},
    "start": {"type": "string", "description": "Start address (optional; default 0x02000000 on the system buses, 0 in other domains)"},
    "length": {"type": "integer", "description": "Bytes to scan (optional; default 4194304 = all of main RAM)"},
}

# ═══════════════════════════════════════════════════════════════
# FREEZE FEATURE CONFIGURATION
# Set to True to enable freeze address feature, False to disable (default)
# This prevents AI from automatically relying on freeze functionality
# ═══════════════════════════════════════════════════════════════
FREEZE_FEATURE_ENABLED = False  # Default: Disabled - enable manually when needed


def normalize_address(addr: str) -> int:
    """Convert address string to integer for consistent comparison.
    Handles: 0x02000000, $02000000, 02000000, 33554432 (decimal)
    """
    if isinstance(addr, int):
        return addr
    addr = str(addr).strip()
    # Remove $ or 0x prefix
    if addr.startswith('$'):
        return int(addr[1:], 16)
    if addr.lower().startswith('0x'):
        return int(addr[2:], 16)
    # Try hex first (if looks like hex), then decimal
    try:
        # If all chars are hex digits and starts with letter, treat as hex
        if any(c in 'abcdefABCDEF' for c in addr):
            return int(addr, 16)
        return int(addr)  # Decimal
    except ValueError:
        return int(addr, 16)  # Fallback to hex


def format_address(addr: int) -> str:
    """Format address as canonical $XXXXXXXX hex string (matches Lua hexAddr)"""
    return f"${addr:08X}"


def get_next_id() -> int:
    """Get next command ID"""
    global command_id
    command_id += 1
    return command_id


def read_json_file(path: str, attempts: int = 5) -> dict:
    """Read a JSON file the Lua side may be rewriting right now.

    Lua writes in place (truncate, then write), so a read can land on an
    empty or half-written file; retry briefly instead of failing the call.
    """
    for i in range(attempts):
        try:
            with open(path, 'r', encoding='utf-8', errors='replace') as f:
                return json.load(f)
        except ValueError:
            if i == attempts - 1:
                raise
            time.sleep(0.02)


def write_command_file(command: dict) -> None:
    """Write the command atomically so Lua never parses a half-written file."""
    tmp = COMMAND_FILE + ".tmp"
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(command, f)
    for i in range(20):
        try:
            os.replace(tmp, COMMAND_FILE)
            return
        except PermissionError:
            # Windows: Lua has the file open for reading this instant.
            if i == 19:
                raise
            time.sleep(0.01)


def check_bizhawk_alive() -> tuple[bool, str]:
    """Check if BizHawk + Lua script are running by checking state file timestamp"""
    try:
        if not os.path.exists(STATE_FILE):
            return False, "State file not found. Start BizHawk, load ROM, then load bizhawk_debug_api.lua in Tools > Lua Console"

        state = read_json_file(STATE_FILE)
        timestamp = state.get('timestamp', 0)
        is_paused = state.get('isPaused', False)
        age = time.time() - timestamp

        # BizHawk is more reliable - allow longer stale time when paused
        max_age = 120 if is_paused else 30

        if age > max_age:
            status = "PAUSED" if is_paused else "STALE"
            return False, f"BizHawk not responding (status={status}, last update {int(age)}s ago). If using BizHawk pause, the script should still update. Reload bizhawk_debug_api.lua."
        return True, "OK"
    except Exception as e:
        return False, f"Error checking BizHawk status: {e}"


# ═══════════════════════════════════════════════════════════════
# SOCKET CONFIGURATION (NEW - Fast Communication)
# ═══════════════════════════════════════════════════════════════

SOCKET_HOST = "127.0.0.1"
SOCKET_PORT = 9876
SOCKET_TIMEOUT = 30.0  # seconds. Must exceed the longest single command's
# execution time. input.sequence runs many frame advances / wall-clock sleeps
# in one command (a batched navigation sequence with real-time waits), so keep
# this generous enough that a full boot-to-screen batch never trips the socket
# read timeout and falls back to file.
socket_connection = None


def get_socket_connection():
    """Get or create socket connection to BizHawk"""
    global socket_connection
    
    if socket_connection is not None:
        return socket_connection
    
    try:
        import socket
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(SOCKET_TIMEOUT)
        sock.connect((SOCKET_HOST, SOCKET_PORT))
        socket_connection = sock
        logger.info(f"[SOCKET] Connected to BizHawk on port {SOCKET_PORT}")
        return sock
    except Exception as e:
        logger.debug(f"[SOCKET] Connection failed: {e}")
        return None


def close_socket_connection():
    """Close socket connection"""
    global socket_connection
    if socket_connection:
        try:
            socket_connection.close()
        except:
            pass
        socket_connection = None


def build_command(action: str, cmd_id: int, params: dict) -> dict:
    """The envelope's id/action always win over same-named params (a param
    called "id" used to replace the command id and the reply was never matched)."""
    return {**params, "id": cmd_id, "action": action}


def send_command_socket(action: str, cmd_id: int, params: dict) -> dict | None:
    """Send command via socket (fast path).

    Returns None only when no live bridge took the command (no socket, the
    send failed, or the peer had closed the connection, e.g. after a Lua
    reload), so the caller may safely retry over the file channel. A timeout
    or a garbled reply is an error instead: the bridge may already be running
    the command, and re-sending would run it twice (e.g. a whole input.sequence).
    """
    sock = get_socket_connection()
    if sock is None:
        return None  # Signal to use file-based

    message = json.dumps(build_command(action, cmd_id, params)) + "\n"
    try:
        sock.sendall(message.encode('utf-8'))
        logger.debug(f"[SOCKET] Sent: {action} (id={cmd_id})")
    except OSError as e:
        logger.warning(f"[SOCKET] Send failed: {e}, falling back to file-based")
        close_socket_connection()
        return None

    try:
        buffer = b""
        while b"\n" not in buffer:
            chunk = sock.recv(4096)
            if not chunk:
                raise ConnectionError("Connection closed")
            buffer += chunk

        response_line = buffer.split(b"\n")[0]
        response = json.loads(response_line.decode('utf-8', errors='replace'))
        logger.debug(f"[SOCKET] Response received for command {cmd_id}")
        return response
    except ConnectionError as e:
        # Closed/reset by the peer: that bridge instance is gone (no partial
        # reply was read, since the reply is one line), so nothing is running it.
        logger.warning(f"[SOCKET] Connection lost ({e}), falling back to file-based")
        close_socket_connection()
        return None
    except Exception as e:
        logger.warning(f"[SOCKET] No reply for {action} (id={cmd_id}): {e}")
        close_socket_connection()
        return {"error": f"No reply from BizHawk for {action} after sending it ({e}). "
                         "It may still be running; not re-sending.", "commandId": cmd_id}


def send_command_file(action: str, cmd_id: int, params: dict, timeout: float) -> dict:
    """Send command via file (fallback)"""
    logger.debug(f"[FILE] Sending command: {action} (id={cmd_id})")

    try:
        write_command_file(build_command(action, cmd_id, params))
        logger.debug(f"[FILE] Command written to {COMMAND_FILE}")
    except Exception as e:
        logger.error(f"[FILE] Failed to write command: {e}")
        return {"error": str(e), "commandId": cmd_id}

    # Wait for response
    poll_interval = 0.05  # 50ms polling
    deadline = time.monotonic() + timeout
    last_error = None

    while time.monotonic() < deadline:
        time.sleep(poll_interval)
        try:
            if os.path.exists(RESPONSE_FILE):
                with open(RESPONSE_FILE, 'r', encoding='utf-8', errors='replace') as f:
                    response = json.load(f)
                if response.get('commandId') == cmd_id:
                    logger.debug(f"[FILE] Response received for command {cmd_id}")
                    return response
        except Exception as e:
            last_error = e  # half-written response; poll again

    if last_error:
        logger.warning(f"[FILE] Last error reading response: {last_error}")
    logger.warning(f"[FILE] Timeout waiting for response to command {cmd_id}")
    return {"error": f"Timeout after {timeout}s. Check if BizHawk has Lua Console open and bizhawk_debug_api.lua loaded.", "commandId": cmd_id}


def send_command(action: str, timeout: float | None = None, **params) -> dict:
    """Send command to BizHawk with keyword params (see send_raw)"""
    return send_raw(action, params, timeout)


def send_raw(action: str, params: dict, timeout: float | None = None) -> dict:
    """Send command to BizHawk - tries socket first, then file

    Socket advantages:
    - Near-instant response (< 1ms)
    - No file I/O overhead

    Falls back to file-based if no socket bridge is listening.
    """
    if not isinstance(action, str) or not action:
        return {"error": "action must be a non-empty string"}
    if timeout is None:
        timeout = DEFAULT_TIMEOUT
    # First check if BizHawk is alive
    alive, message = check_bizhawk_alive()
    if not alive:
        logger.error(f"BizHawk not available: {message}")
        return {"error": message, "bizhawk_not_running": True}

    cmd_id = get_next_id()

    # Try socket first (fast path)
    result = send_command_socket(action, cmd_id, params)
    if result is not None:
        return result

    # Fall back to file-based
    return send_command_file(action, cmd_id, params, timeout)


def read_state() -> dict:
    """Read current BizHawk state"""
    try:
        state = read_json_file(STATE_FILE)
        state["ageSeconds"] = round(time.time() - state.get("timestamp", 0), 1)
        return state
    except Exception as e:
        logger.error(f"Cannot read state file: {e}")
        return {"error": f"Cannot read state file: {e}"}


def data_of(response: dict, default=None):
    """`data` of a successful bridge response, else `default`."""
    if not isinstance(response, dict) or "error" in response or response.get("success") is False:
        return default
    return response.get("data", default)


def tool_definitions() -> list[Tool]:
    """All BizHawk debugging tools (annotations are attached in list_tools)"""
    return [
        # ═══════════════════════════════════════════════════════════════
        # MEMORY TOOLS (Emulator Runtime Only)
        # ═══════════════════════════════════════════════════════════════
        Tool(
            name="emu_debug_read_memory",
            description="Read a byte from emulator memory. Address can be hex (0x02000000 or $02000000) or decimal. The default domain 'ARM9 System Bus' takes physical DS addresses (0x02000000 main RAM, 0x04000000 I/O, 0x05000000 palette, 0x06000000 VRAM, 0x07000000 OAM); other domains such as 'Main RAM' take offsets.",
            inputSchema={
                "type": "object",
                "properties": {
                    "address": {"type": "string", "description": "Memory address (hex or decimal)"},
                    "domain": {"type": "string", "description": "Memory domain (optional, default='ARM9 System Bus')"}
                },
                "required": ["address"]
            }
        ),
        Tool(
            name="emu_debug_write_memory",
            description="Write a byte to emulator RAM. Useful for modifying game values like lives, health, score in the running emulator.",
            inputSchema={
                "type": "object",
                "properties": {
                    "address": {"type": "string", "description": "Memory address (hex or decimal)"},
                    "value": {"type": "integer", "description": "Value to write (0-255)"},
                    "domain": {"type": "string", "description": "Memory domain (optional)"}
                },
                "required": ["address", "value"]
            }
        ),
        Tool(
            name="emu_debug_read_memory_range",
            description="Read a range of bytes from emulator memory. Returns array of values.",
            inputSchema={
                "type": "object",
                "properties": {
                    "address": {"type": "string", "description": "Start address"},
                    "length": {"type": "integer", "description": "Number of bytes to read (max 65536)"},
                    "domain": {"type": "string", "description": "Memory domain (optional)"}
                },
                "required": ["address", "length"]
            }
        ),
        Tool(
            name="emu_debug_search_memory",
            description="Search memory for bytes equal to a value. Defaults to all of main RAM (0x02000000-0x023FFFFF on ARM9 System Bus). Returns up to 1000 matching addresses; 'total' gives the full count.",
            inputSchema={
                "type": "object",
                "properties": {
                    "value": {"type": "integer", "description": "Value to search for (0-255)"},
                    **REGION_PROPERTIES
                },
                "required": ["value"]
            }
        ),
        Tool(
            name="emu_debug_snapshot_memory",
            description="Take a snapshot of memory (default: all 4 MB of main RAM) for later comparison. Use before the value changes.",
            inputSchema={
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Snapshot name (optional, default='default')"},
                    **REGION_PROPERTIES
                }
            }
        ),
        Tool(
            name="emu_debug_compare_memory",
            description="Compare current memory to a snapshot. Filter: 'changed', 'increased', 'decreased', 'same'. Returns up to 1000 entries; 'total' gives the full count.",
            inputSchema={
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Snapshot name to compare to"},
                    "filter": {"type": "string", "description": "Filter type: changed/increased/decreased/same"}
                }
            }
        ),
        Tool(
            name="emu_debug_list_memory_domains",
            description="Get list of available emulator memory domains (Main RAM, Shared WRAM, Instruction TCM, Data TCM, ARM9 System Bus, ...).",
            inputSchema={"type": "object", "properties": {}}
        ),
        
        # ═══════════════════════════════════════════════════════════════
        # CPU TOOLS (Emulator Runtime Only)
        # ═══════════════════════════════════════════════════════════════
        Tool(
            name="emu_debug_get_cpu_state",
            description="Get ARM9 and ARM7 registers r0-r15 as numbers (sp=r13, lr=r14, pc=r15; r15 is the pipelined PC, ahead of the executing instruction). BizHawk does not expose CPSR, so there are no flags or Thumb bit.",
            inputSchema={"type": "object", "properties": {}}
        ),
        Tool(
            name="emu_debug_get_cpu_registers",
            description="Get ARM9 and ARM7 registers r0-r15 as hex strings.",
            inputSchema={"type": "object", "properties": {}}
        ),
        
        # ═══════════════════════════════════════════════════════════════
        # EXECUTION CONTROL (Emulator Runtime Only)
        # ═══════════════════════════════════════════════════════════════
        Tool(
            name="emu_debug_pause",
            description="Pause emulator execution. All other tools keep working while paused; frame steps advance and then re-pause.",
            inputSchema={"type": "object", "properties": {}}
        ),
        Tool(
            name="emu_debug_resume",
            description="Resume emulator execution.",
            inputSchema={"type": "object", "properties": {}}
        ),
        Tool(
            name="emu_debug_step",
            description="Advance the emulator by N frames. Works while paused (stays paused afterwards). Only stepType='frame' exists: the MelonDS core cannot single-step instructions.",
            inputSchema={
                "type": "object",
                "properties": {
                    "count": {"type": "integer", "description": "Number of frames (default=1)"},
                    "stepType": {"type": "string", "description": "Must be 'frame' (default)", "enum": ["frame"]}
                }
            }
        ),
        Tool(
            name="emu_debug_frame_advance",
            description="Advance emulator by one or more frames. Works while paused (stays paused afterwards).",
            inputSchema={
                "type": "object",
                "properties": {
                    "count": {"type": "integer", "description": "Number of frames (default=1)"}
                }
            }
        ),
        
        # ═══════════════════════════════════════════════════════════════
        # BREAKPOINTS (Emulator Runtime Only)
        # ═══════════════════════════════════════════════════════════════
        Tool(
            name="emu_debug_add_breakpoint",
            description="Add a debug breakpoint on an ARM9 System Bus address. Types: 'read', 'write', 'execute'. Uses BizHawk memory callbacks (MelonDS JIT must be off). Hits are recorded; read them with emu_debug_get_breakpoint_hits.",
            inputSchema={
                "type": "object",
                "properties": {
                    "type": {"type": "string", "description": "Breakpoint type: read/write/execute"},
                    "address": {"type": "string", "description": "Address to break on"}
                },
                "required": ["type", "address"]
            }
        ),
        Tool(
            name="emu_debug_list_breakpoints",
            description="List all active emulator breakpoints.",
            inputSchema={"type": "object", "properties": {}}
        ),
        Tool(
            name="emu_debug_remove_breakpoint",
            description="Remove an emulator breakpoint by ID.",
            inputSchema={
                "type": "object",
                "properties": {
                    "id": {"type": "integer", "description": "Breakpoint ID to remove"}
                },
                "required": ["id"]
            }
        ),
        Tool(
            name="emu_debug_clear_breakpoints",
            description="Remove all emulator breakpoints.",
            inputSchema={"type": "object", "properties": {}}
        ),
        
        # Breakpoint hit tracking (AI-friendly)
        Tool(
            name="emu_debug_set_breakpoint_auto_pause",
            description="Enable/disable auto-pause on breakpoint hits. When disabled (default), breakpoints only record hits. The pause takes effect at the end of the current frame, not at the instruction.",
            inputSchema={
                "type": "object",
                "properties": {
                    "enabled": {"type": "boolean", "description": "True = pause on breakpoint hit, False = non-blocking logging mode"}
                },
                "required": ["enabled"]
            }
        ),
        Tool(
            name="emu_debug_get_breakpoint_hits",
            description="Get the recorded breakpoint hits (most recent 100).",
            inputSchema={"type": "object", "properties": {}}
        ),
        Tool(
            name="emu_debug_get_last_breakpoint_hit",
            description="Get the most recent breakpoint hit with ARM9/ARM7 registers.",
            inputSchema={"type": "object", "properties": {}}
        ),
        Tool(
            name="emu_debug_clear_breakpoint_hits",
            description="Clear the emulator breakpoint hit queue.",
            inputSchema={"type": "object", "properties": {}}
        ),

        # ═══════════════════════════════════════════════════════════════
        # TRACE LOGGER (Emulator Runtime Only)
        # ═══════════════════════════════════════════════════════════════
        Tool(
            name="emu_debug_start_trace",
            description="Record the addresses of the next N ARM9 instructions executed (exec callback per instruction: emulation slows heavily while recording; it stops by itself at the limit). MelonDS JIT must be off.",
            inputSchema={
                "type": "object",
                "properties": {
                    "limit": {"type": "integer", "description": "Instructions to record (default=1000, max=1000)"}
                }
            }
        ),
        Tool(
            name="emu_debug_stop_trace",
            description="Stop emulator trace logging.",
            inputSchema={"type": "object", "properties": {}}
        ),
        Tool(
            name="emu_debug_get_trace",
            description="Get recorded trace entries (executed instruction addresses, oldest first).",
            inputSchema={
                "type": "object",
                "properties": {
                    "count": {"type": "integer", "description": "Number of entries to get (default=100)"}
                }
            }
        ),
        Tool(
            name="emu_debug_clear_trace",
            description="Clear the emulator trace log.",
            inputSchema={"type": "object", "properties": {}}
        ),
        
        # ═══════════════════════════════════════════════════════════════
        # WATCH LIST (Emulator Runtime Only)
        # ═══════════════════════════════════════════════════════════════
        Tool(
            name="emu_debug_add_watch",
            description="Add emulator memory address to watch list. Watched values are updated in real-time.",
            inputSchema={
                "type": "object",
                "properties": {
                    "address": {"type": "string", "description": "Address to watch"},
                    "name": {"type": "string", "description": "Name/label for this watch (optional)"}
                },
                "required": ["address"]
            }
        ),
        Tool(
            name="emu_debug_list_watches",
            description="Get all watched emulator memory values with current readings.",
            inputSchema={"type": "object", "properties": {}}
        ),
        Tool(
            name="emu_debug_remove_watch",
            description="Remove address from emulator watch list.",
            inputSchema={
                "type": "object",
                "properties": {
                    "address": {"type": "string", "description": "Address to remove"}
                },
                "required": ["address"]
            }
        ),
        Tool(
            name="emu_debug_clear_watches",
            description="Clear all emulator watches.",
            inputSchema={"type": "object", "properties": {}}
        ),
        
        # ═══════════════════════════════════════════════════════════════
        # STATE & UTILITIES (Emulator Runtime Only)
        # ═══════════════════════════════════════════════════════════════
        Tool(
            name="emu_debug_get_emulator_state",
            description="Get current emulator state from the bridge heartbeat: paused status, frame count, CPU registers, watched values, ageSeconds.",
            inputSchema={"type": "object", "properties": {}}
        ),
        Tool(
            name="emu_debug_save_state",
            description="Save emulator state to a numbered slot or a file path. Writes a savestate file on disk (slots go to BizHawk's SaveStates folder), overwriting any existing one.",
            inputSchema={
                "type": "object",
                "properties": {
                    "slot": {"type": "integer", "description": "Save state slot (1-10)"},
                    "path": {"type": "string", "description": "Or save to specific file path"}
                }
            }
        ),
        Tool(
            name="emu_debug_load_state",
            description="Load emulator state from a slot or file path, replacing the current emulator state.",
            inputSchema={
                "type": "object",
                "properties": {
                    "slot": {"type": "integer", "description": "Load state slot (1-10)"},
                    "path": {"type": "string", "description": "Or load from specific file path"}
                }
            }
        ),
        Tool(
            name="emu_debug_set_input",
            description="Set controller input. Buttons (A B X Y L R Up Down Left Right Start Select Touch) apply to the next frame; touch coords 'Touch X' (0-255) / 'Touch Y' (0-191) are sticky until raw_command input.touch_release. To hold input over several frames use emu_debug_raw_command with input.press / input.touch / input.sequence.",
            inputSchema={
                "type": "object",
                "properties": {
                    "buttons": {"type": "object", "description": "Button states, e.g. {\"A\": true, \"Up\": true} or {\"Touch\": true, \"Touch X\": 128, \"Touch Y\": 96}"},
                    "player": {"type": "integer", "description": "Player number (1-4, default=1)"}
                }
            }
        ),
        Tool(
            name="emu_debug_raw_command",
            description="Send any bridge action with its params (reference: NDS_SETUP.md), e.g. input.press, input.touch, input.sequence, screenshot.take, oam.parse, bg.read, powcnt.read, system.open_rom. Some actions write host files (screenshot.take, memory.dump_to_file, state.save) or load/close ROMs.",
            inputSchema={
                "type": "object",
                "properties": {
                    "action": {"type": "string", "description": "Command action name"},
                    "params": {"type": "object", "description": "Command parameters"}
                },
                "required": ["action"]
            }
        ),
        
        # ═══════════════════════════════════════════════════════════════
        # HIGH-LEVEL CHEAT TOOLS - Freeze/Lock (Emulator Runtime Only)
        # ═══════════════════════════════════════════════════════════════
        Tool(
            name="emu_cheat_freeze_address",
            description="Freeze an address to a constant value (writes a single byte once per bridge loop, i.e. every frame while idle). Use for 'infinite lives', 'max health' cheats. Max 16 concurrent freezes. Note: byte-only (0-255).",
            inputSchema={
                "type": "object",
                "properties": {
                    "address": {"type": "string", "description": "Memory address to freeze (hex or decimal)"},
                    "value": {"type": "integer", "description": "Value to maintain (0-255, single byte)"},
                    "label": {"type": "string", "description": "Optional friendly name for this freeze"},
                    "domain": {"type": "string", "description": "Memory domain (optional, default='ARM9 System Bus')"}
                },
                "required": ["address", "value"]
            }
        ),
        Tool(
            name="emu_cheat_unfreeze_address",
            description="Remove a freeze by ID or address, or clear all freezes.",
            inputSchema={
                "type": "object",
                "properties": {
                    "freezeId": {"type": "integer", "description": "Freeze ID to remove"},
                    "address": {"type": "string", "description": "Or remove by address"},
                    "all": {"type": "boolean", "description": "Or clear all freezes"}
                }
            }
        ),
        Tool(
            name="emu_cheat_list_freezes",
            description="List all currently active address freezes.",
            inputSchema={"type": "object", "properties": {}}
        ),
        
        # ═══════════════════════════════════════════════════════════════
        # HIGH-LEVEL CHEAT TOOLS - Value Discovery (Emulator Runtime Only)
        # ═══════════════════════════════════════════════════════════════
        Tool(
            name="emu_cheat_find_decrementing_value",
            description="Find main RAM addresses holding a value that decrements over time (e.g., lives, timer). Snapshots main RAM, advances frames, finds decreased bytes.",
            inputSchema={
                "type": "object",
                "properties": {
                    "initialValue": {"type": "integer", "description": "Optional: known starting value to filter by"},
                    "frames": {"type": "integer", "description": "Frames to advance before comparing (default=60, max=300)"},
                    "minDecrement": {"type": "integer", "description": "Minimum decrease amount (default=1)"}
                }
            }
        ),
        Tool(
            name="emu_cheat_find_value_on_event",
            description="Two-phase value finder: Phase 1 snapshots RAM and returns. Phase 2 (after user triggers event) compares and finds changed values. Use same snapshotName for both calls.",
            inputSchema={
                "type": "object",
                "properties": {
                    "phase": {"type": "integer", "description": "1 = take snapshot, 2 = compare after event", "enum": [1, 2]},
                    "snapshotName": {"type": "string", "description": "Snapshot name (default='event_hunt')"},
                    "filter": {"type": "string", "description": "For phase 2: changed/increased/decreased (default='changed')"}
                },
                "required": ["phase"]
            }
        ),
        Tool(
            name="emu_cheat_narrow_candidates",
            description="Narrow a list of candidate addresses using changed/unchanged/increased/decreased filter. Takes snapshot, you trigger change, call again with filter.",
            inputSchema={
                "type": "object",
                "properties": {
                    "addresses": {"type": "array", "items": {"type": "string"}, "description": "List of candidate addresses to check"},
                    "action": {"type": "string", "description": "snapshot = take snapshot, filter = compare", "enum": ["snapshot", "filter"]},
                    "filter": {"type": "string", "description": "For filter action: changed/unchanged/increased/decreased"},
                    "snapshotName": {"type": "string", "description": "Snapshot name (default='narrow')"}
                },
                "required": ["addresses", "action"]
            }
        ),
        
        # ═══════════════════════════════════════════════════════════════
        # HIGH-LEVEL DEBUG TOOLS - Breakpoint Workflows (Emulator Runtime Only)
        # ═══════════════════════════════════════════════════════════════
        Tool(
            name="emu_debug_capture_write_source",
            description="Set write breakpoint on address, run for N frames, summarize what code wrote to it. Auto-removes breakpoint after.",
            inputSchema={
                "type": "object",
                "properties": {
                    "address": {"type": "string", "description": "Address to monitor for writes"},
                    "frames": {"type": "integer", "description": "Frames to capture (default=180, max=600)"}
                },
                "required": ["address"]
            }
        ),
        Tool(
            name="emu_debug_trace_and_summarize",
            description="Count executed ARM9 instruction addresses over N frames and return the hottest. Runs a Lua callback per instruction, so emulation is very slow while it runs; keep frames small. MelonDS JIT must be off.",
            inputSchema={
                "type": "object",
                "properties": {
                    "frames": {"type": "integer", "description": "Frames to trace (default=1, max=60)"},
                    "topN": {"type": "integer", "description": "Number of hotspots to return (default=20)"}
                }
            }
        )
    ]


# Tools that only observe, and tools whose effects reach beyond the running
# emulator (savestate files on disk, arbitrary raw actions). Everything else
# changes volatile emulator state only.
READ_ONLY_TOOLS = {
    "emu_debug_read_memory", "emu_debug_read_memory_range", "emu_debug_search_memory",
    "emu_debug_list_memory_domains", "emu_debug_get_cpu_state", "emu_debug_get_cpu_registers",
    "emu_debug_list_breakpoints", "emu_debug_get_breakpoint_hits",
    "emu_debug_get_last_breakpoint_hit", "emu_debug_get_trace", "emu_debug_list_watches",
    "emu_debug_get_emulator_state", "emu_cheat_list_freezes",
}
DESTRUCTIVE_TOOLS = {"emu_debug_save_state", "emu_debug_load_state", "emu_debug_raw_command"}


async def list_tools() -> list[Tool]:
    """List all available BizHawk debugging tools"""
    logger.debug("list_tools called")
    tools = tool_definitions()
    for tool in tools:
        read_only = tool.name in READ_ONLY_TOOLS
        tool.annotations = ToolAnnotations(
            readOnlyHint=read_only,
            destructiveHint=None if read_only else tool.name in DESTRUCTIVE_TOOLS,
            openWorldHint=False,
        )
    return tools


def run_tool(name: str, arguments: dict) -> dict:
    """Run one tool call against the bridge (blocking; called in a worker thread)"""
    logger.info(f"Tool called: {name} with args: {arguments}")
    result = None

    try:
        # ═══════════════════════════════════════════════════════════════
        # MEMORY TOOLS
        # ═══════════════════════════════════════════════════════════════
        if name == "emu_debug_read_memory":
            result = send_command("memory.read", 
                                 address=arguments.get("address"),
                                 domain=arguments.get("domain"))
        elif name == "emu_debug_write_memory":
            result = send_command("memory.write", 
                                 address=arguments.get("address"), 
                                 value=arguments.get("value"),
                                 domain=arguments.get("domain"))
        elif name == "emu_debug_read_memory_range":
            result = send_command("memory.readRange", 
                                 address=arguments.get("address"),
                                 length=arguments.get("length"),
                                 domain=arguments.get("domain"))
        elif name == "emu_debug_search_memory":
            result = send_command("memory.search",
                                 value=arguments.get("value"),
                                 **region_args(arguments))
        elif name == "emu_debug_snapshot_memory":
            result = send_command("memory.snapshot", name=arguments.get("name", "default"),
                                  **region_args(arguments))
        elif name == "emu_debug_compare_memory":
            result = send_command("memory.compare", 
                                 name=arguments.get("name", "default"),
                                 filter=arguments.get("filter", "changed"))
        elif name == "emu_debug_list_memory_domains":
            result = send_command("memory.getDomains")
        
        # ═══════════════════════════════════════════════════════════════
        # CPU TOOLS
        # ═══════════════════════════════════════════════════════════════
        elif name == "emu_debug_get_cpu_state":
            result = send_command("cpu.getState")
        elif name == "emu_debug_get_cpu_registers":
            result = send_command("cpu.getRegisters")
        
        # ═══════════════════════════════════════════════════════════════
        # EXECUTION CONTROL
        # ═══════════════════════════════════════════════════════════════
        elif name == "emu_debug_pause":
            result = send_command("execution.pause")
        elif name == "emu_debug_resume":
            result = send_command("execution.resume")
        elif name == "emu_debug_step":
            result = send_command("execution.step",
                                 count=arguments.get("count", 1),
                                 stepType=arguments.get("stepType", "frame"))
        elif name == "emu_debug_frame_advance":
            result = send_command("execution.step",
                                 count=arguments.get("count", 1),
                                 stepType="frame")
        
        # ═══════════════════════════════════════════════════════════════
        # BREAKPOINTS
        # ═══════════════════════════════════════════════════════════════
        elif name == "emu_debug_add_breakpoint":
            result = send_command("breakpoint.add",
                                 type=arguments.get("type"),
                                 address=arguments.get("address"))
        elif name == "emu_debug_list_breakpoints":
            result = send_command("breakpoint.list")
        elif name == "emu_debug_remove_breakpoint":
            result = send_command("breakpoint.remove", bpId=arguments.get("id"))
        elif name == "emu_debug_clear_breakpoints":
            result = send_command("breakpoint.clear")
        
        # Breakpoint hit tracking
        elif name == "emu_debug_set_breakpoint_auto_pause":
            result = send_command("breakpoint.setAutoMode", enabled=arguments.get("enabled", False))
        elif name == "emu_debug_get_breakpoint_hits":
            result = send_command("breakpoint.getHits")
        elif name == "emu_debug_get_last_breakpoint_hit":
            result = send_command("breakpoint.getLastHit")
        elif name == "emu_debug_clear_breakpoint_hits":
            result = send_command("breakpoint.clearHits")
        
        # ═══════════════════════════════════════════════════════════════
        # TRACE LOGGER
        # ═══════════════════════════════════════════════════════════════
        elif name == "emu_debug_start_trace":
            result = send_command("trace.start", limit=arguments.get("limit"))
        elif name == "emu_debug_stop_trace":
            result = send_command("trace.stop")
        elif name == "emu_debug_get_trace":
            result = send_command("trace.get", count=arguments.get("count", 100))
        elif name == "emu_debug_clear_trace":
            result = send_command("trace.clear")
        
        # ═══════════════════════════════════════════════════════════════
        # WATCH LIST
        # ═══════════════════════════════════════════════════════════════
        elif name == "emu_debug_add_watch":
            result = send_command("watch.add",
                                 address=arguments.get("address"),
                                 name=arguments.get("name"))
        elif name == "emu_debug_list_watches":
            result = send_command("watch.list")
        elif name == "emu_debug_remove_watch":
            result = send_command("watch.remove", address=arguments.get("address"))
        elif name == "emu_debug_clear_watches":
            result = send_command("watch.clear")
        
        # ═══════════════════════════════════════════════════════════════
        # STATE & UTILITIES
        # ═══════════════════════════════════════════════════════════════
        elif name == "emu_debug_get_emulator_state":
            result = read_state()
        elif name == "emu_debug_save_state":
            result = send_command("state.save",
                                 slot=arguments.get("slot"),
                                 path=arguments.get("path"))
        elif name == "emu_debug_load_state":
            result = send_command("state.load",
                                 slot=arguments.get("slot"),
                                 path=arguments.get("path"))
        elif name == "emu_debug_set_input":
            result = send_command("input.set",
                                 buttons=arguments.get("buttons", {}),
                                 player=arguments.get("player", 1))
        elif name == "emu_debug_raw_command":
            result = send_raw(arguments.get("action"), arguments.get("params") or {})
        
        # ═══════════════════════════════════════════════════════════════
        # HIGH-LEVEL CHEAT TOOLS - Freeze/Lock
        # ═══════════════════════════════════════════════════════════════
        elif name == "emu_cheat_freeze_address":
            # Check if freeze feature is enabled
            if not FREEZE_FEATURE_ENABLED:
                result = {
                    "error": "Freeze feature is DISABLED by default.",
                    "message": "To enable this feature, set FREEZE_FEATURE_ENABLED = True in bizhawk_mcp_server.py",
                    "reason": "This prevents AI from automatically relying on freeze functionality. Enable manually when needed."
                }
            else:
                result = send_command("freeze.add",
                                     address=arguments.get("address"),
                                     value=arguments.get("value"),
                                     label=arguments.get("label"),
                                     domain=arguments.get("domain"))
        elif name == "emu_cheat_unfreeze_address":
            result = send_command("freeze.remove",
                                 freezeId=arguments.get("freezeId"),
                                 address=arguments.get("address"),
                                 removeAll=arguments.get("all", False))
        elif name == "emu_cheat_list_freezes":
            result = send_command("freeze.list")
        
        # ═══════════════════════════════════════════════════════════════
        # HIGH-LEVEL CHEAT TOOLS - Value Discovery
        # ═══════════════════════════════════════════════════════════════
        elif name == "emu_cheat_find_decrementing_value":
            # Orchestration: snapshot -> advance frames -> compare(decreased)
            frames = min(arguments.get("frames", 60), 300)
            initial_value = arguments.get("initialValue")
            min_decrement = arguments.get("minDecrement", 1)
            
            # Check initial pause state to restore later
            initial_state = read_state()
            was_paused = initial_state.get("isPaused", False)
            logger.debug(f"[find_decrementing_value] Starting, was_paused={was_paused}, frames={frames}")
            
            # Step 1: Pause and snapshot
            pause_result = send_command("execution.pause", timeout=FAST_TIMEOUT)
            logger.debug(f"[find_decrementing_value] Pause result: {pause_result.get('commandId', 'N/A')}")
            
            snap_result = send_command("memory.snapshot", name="decr_hunt")
            logger.debug(f"[find_decrementing_value] Snapshot result: {snap_result.get('commandId', 'N/A')}")
            
            if data_of(snap_result) is None:
                result = snap_result
            else:
                # Step 2: Advance frames (execution.step works while paused and re-pauses)
                logger.debug(f"[find_decrementing_value] Advancing {frames} frames")
                step_result = send_command("execution.step", count=frames, stepType="frame")
                logger.debug(f"[find_decrementing_value] Step result: {data_of(step_result)}")

                # Step 3: Compare for decreased values; the bridge applies the
                # filters before capping the list at 1000 entries.
                compare_result = send_command("memory.compare", name="decr_hunt", filter="decreased",
                                              minDelta=min_decrement, oldValue=initial_value)
                logger.debug(f"[find_decrementing_value] Compare result: commandId={compare_result.get('commandId', 'N/A')}")

                candidates = data_of(compare_result)
                if candidates is None:
                    result = compare_result
                else:
                    result = {
                        "candidates": candidates[:200],
                        "totalFound": compare_result.get("total", len(candidates)),
                        "returned": min(len(candidates), 200),
                        "framesAdvanced": frames,
                        "status": "completed"
                    }
                
                # Restore original pause state
                if not was_paused:
                    logger.debug("[find_decrementing_value] Restoring running state")
                    send_command("execution.resume", timeout=FAST_TIMEOUT)
                else:
                    logger.debug("[find_decrementing_value] Keeping paused state")
        
        elif name == "emu_cheat_find_value_on_event":
            phase = arguments.get("phase", 1)
            snapshot_name = arguments.get("snapshotName", "event_hunt")
            filter_type = arguments.get("filter", "changed")
            
            if phase == 1:
                # Phase 1: Take snapshot
                logger.debug(f"[find_value_on_event] Phase 1: Taking snapshot '{snapshot_name}'")
                send_command("execution.pause", timeout=FAST_TIMEOUT)
                snap_result = send_command("memory.snapshot", name=snapshot_name)
                send_command("execution.resume", timeout=FAST_TIMEOUT)  # Resume so user can trigger event
                logger.debug("[find_value_on_event] Phase 1 complete, game resumed")
                result = {
                    "phase": "snapshot_taken",
                    "snapshotName": snapshot_name,
                    "message": "Snapshot taken. Game is running - trigger the in-game event, then call with phase=2 to compare."
                }
            else:
                # Phase 2: Compare after event
                logger.debug(f"[find_value_on_event] Phase 2: Comparing with snapshot '{snapshot_name}', filter='{filter_type}'")
                send_command("execution.pause", timeout=FAST_TIMEOUT)
                compare_result = send_command("memory.compare", name=snapshot_name, filter=filter_type)
                send_command("execution.resume", timeout=FAST_TIMEOUT)  # Resume after comparison
                logger.debug("[find_value_on_event] Phase 2 complete, game resumed")
                
                candidates = data_of(compare_result)
                if candidates is None:
                    result = compare_result
                else:
                    total = compare_result.get("total", len(candidates))
                    result = {
                        "phase": "comparison_complete",
                        "candidates": candidates[:200],
                        "candidateCount": total,
                        "filter": filter_type,
                        "message": f"Found {total} addresses that {filter_type} (showing up to 200). Game is running."
                    }
        
        elif name == "emu_cheat_narrow_candidates":
            addresses = arguments.get("addresses", [])
            action = arguments.get("action", "snapshot")
            snapshot_name = arguments.get("snapshotName", "narrow")
            filter_type = arguments.get("filter", "changed")
            
            # Candidates as ints (invalid ones skipped); the snapshot covers
            # exactly their span on ARM9 System Bus, wherever they live.
            addr_ints = []
            for addr in addresses:
                try:
                    addr_ints.append(normalize_address(addr))
                except (ValueError, TypeError):
                    continue  # Skip invalid addresses

            if not addr_ints:
                result = {"error": "No valid candidate addresses given"}
            elif max(addr_ints) - min(addr_ints) >= 0x400000:
                result = {"error": "Candidate addresses span more than 4 MB; narrow them in batches"}
            elif action == "snapshot":
                start = min(addr_ints)
                span = max(addr_ints) - start + 1
                logger.debug(f"[narrow_candidates] Snapshot action: {len(addr_ints)} addresses, name='{snapshot_name}'")
                send_command("execution.pause", timeout=FAST_TIMEOUT)
                snap_result = send_command("memory.snapshot", name=snapshot_name,
                                           start=start, length=span)
                send_command("execution.resume", timeout=FAST_TIMEOUT)  # Resume so user can trigger change
                logger.debug("[narrow_candidates] Snapshot complete, game resumed")
                if data_of(snap_result) is None:
                    result = snap_result
                else:
                    result = {
                        "action": "snapshot",
                        "addressCount": len(addr_ints),
                        "snapshotName": snapshot_name,
                        "message": "Snapshot taken. Game is running - trigger change, then call with action='filter' to narrow."
                    }
            else:
                # Filter mode: compare only the candidate addresses
                logger.debug(f"[narrow_candidates] Filter action: {len(addr_ints)} addresses, filter='{filter_type}'")
                send_command("execution.pause", timeout=FAST_TIMEOUT)
                compare_result = send_command("memory.compare", name=snapshot_name, filter=filter_type,
                                              addresses=addr_ints)
                send_command("execution.resume", timeout=FAST_TIMEOUT)  # Resume after filtering
                logger.debug("[narrow_candidates] Filter complete, game resumed")

                details = data_of(compare_result)
                if details is None:
                    result = compare_result
                else:
                    narrowed = [format_address(c["address"]) for c in details]
                    result = {
                        "action": "filter",
                        "originalCount": len(addresses),
                        "narrowedCount": len(narrowed),
                        "narrowed": narrowed,
                        "details": details[:50],  # Include value changes
                        "filter": filter_type,
                        "message": "Filtering complete. Game is running."
                    }
        
        # ═══════════════════════════════════════════════════════════════
        # HIGH-LEVEL DEBUG TOOLS - Breakpoint Workflows
        # ═══════════════════════════════════════════════════════════════
        elif name == "emu_debug_capture_write_source":
            address = arguments.get("address")
            frames = min(arguments.get("frames", 180), 600)
            
            # Step 1: Clear previous hits and set non-blocking mode
            send_command("breakpoint.clearHits", timeout=FAST_TIMEOUT)
            send_command("breakpoint.setAutoMode", enabled=False, timeout=FAST_TIMEOUT)
            
            # Step 2: Add write breakpoint
            bp_result = send_command("breakpoint.add", type="write", address=address)
            if data_of(bp_result) is None:
                result = bp_result
            else:
                bp_id = bp_result["data"].get("id")

                # Step 3: Resume and advance frames
                send_command("execution.resume", timeout=FAST_TIMEOUT)
                send_command("execution.step", count=frames, stepType="frame")
                send_command("execution.pause", timeout=FAST_TIMEOUT)

                # Step 4: Get this breakpoint's hits (the queue keeps the last 100)
                hits = [h for h in data_of(send_command("breakpoint.getHits"), [])
                        if h.get("breakpointId") == bp_id]

                # Step 5: Remove breakpoint
                if bp_id:
                    send_command("breakpoint.remove", bpId=bp_id, timeout=FAST_TIMEOUT)
                
                # Step 6: Aggregate hits by PC
                pc_counts = {}
                for hit in hits:
                    pc = hit.get("PC", "unknown")
                    if pc not in pc_counts:
                        pc_counts[pc] = {"pc": pc, "count": 0, "lastValue": None}
                    pc_counts[pc]["count"] += 1
                    pc_counts[pc]["lastValue"] = hit.get("value")
                
                sorted_hits = sorted(pc_counts.values(), key=lambda x: x["count"], reverse=True)[:20]
                
                result = {
                    "targetAddress": address,
                    "framesAdvanced": frames,
                    "totalHits": len(hits),
                    "uniqueSites": len(pc_counts),
                    "hits": sorted_hits,
                    "summary": f"{len(pc_counts)} unique code site(s) wrote to {address}"
                }

        elif name == "emu_debug_trace_and_summarize":
            # Counting happens in the bridge (an exec callback per instruction);
            # see Trace.hotspots in bizhawk_debug_api.lua.
            result = send_command("trace.hotspots",
                                  frames=min(arguments.get("frames", 1), 60),
                                  topN=arguments.get("topN", 20))
        
        else:
            result = {"error": f"Unknown tool: {name}"}
            logger.warning(f"Unknown tool: {name}")
        
        logger.info(f"Tool {name} completed successfully")
        
    except Exception as e:
        logger.error(f"Error in tool {name}: {e}", exc_info=True)
        result = {"error": str(e)}

    return result


def region_args(arguments: dict) -> dict:
    """Optional domain/start/length for search/snapshot, omitting unset ones"""
    return {k: arguments[k] for k in ("domain", "start", "length") if arguments.get(k) is not None}


async def call_tool(name: str, arguments: dict) -> CallToolResult:
    """Handle tool calls off the event loop (bridge calls block for up to
    DEFAULT_TIMEOUT), one at a time, flagging failures with isError."""
    def locked():
        with _bridge_lock:
            return run_tool(name, arguments or {})

    result = await asyncio.to_thread(locked)
    is_error = isinstance(result, dict) and ("error" in result or result.get("success") is False)
    return CallToolResult(content=[TextContent(type="text", text=json.dumps(result, indent=2))],
                          isError=is_error)


class ToolCallError(Exception):
    """Carries an error result's JSON text out of an mcp 1.x call_tool handler"""


def create_server() -> Server:
    """Register the handlers on whichever low-level API the installed mcp SDK
    has: 1.x uses decorators, 2.x takes on_* callbacks in the constructor."""
    if hasattr(Server, "list_tools"):
        srv = Server("bizhawk-nds")

        async def v1_call_tool(name: str, arguments: dict) -> list[TextContent]:
            result = await call_tool(name, arguments)
            if result.isError:
                raise ToolCallError(result.content[0].text)  # reported with isError=True
            return result.content

        srv.list_tools()(list_tools)
        srv.call_tool()(v1_call_tool)
        return srv

    async def on_list_tools(ctx, params) -> ListToolsResult:
        return ListToolsResult(tools=await list_tools())

    async def on_call_tool(ctx, params) -> CallToolResult:
        return await call_tool(params.name, params.arguments or {})

    return Server("bizhawk-nds", on_list_tools=on_list_tools, on_call_tool=on_call_tool)


server = create_server()
logger.info("BizHawk MCP Server instance created")


async def main():
    """Run the MCP server"""
    logger.info("Starting BizHawk MCP server main loop")
    try:
        async with stdio_server() as (read_stream, write_stream):
            logger.info("stdio_server started, running server...")
            await server.run(read_stream, write_stream, server.create_initialization_options())
    except Exception as e:
        logger.error(f"Server error: {e}", exc_info=True)
        raise


if __name__ == "__main__":
    # Ensure any direct prints go to stderr so they don't break the MCP protocol (stdout)
    def print_err(*args, **kwargs):
        print(*args, file=sys.stderr, **kwargs)

    logger.info("Running BizHawk MCP server...")
    print_err("=" * 60)
    print_err("BizHawk MCP Server - Emulator Debug Tools")
    print_err("=" * 60)
    print_err(f"Log file: {LOG_FILE}")
    print_err("")
    print_err("Setup:")
    print_err("   1. Open BizHawk and load a ROM")
    print_err("   2. Tools > Lua Console")
    print_err("   3. Load bizhawk_debug_api.lua")
    print_err("=" * 60)
    
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Server stopped by user")
    except Exception as e:
        logger.error(f"Fatal error: {e}", exc_info=True)
        sys.exit(1)
