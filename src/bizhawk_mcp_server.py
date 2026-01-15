"""
BizHawk MCP Server
Model Context Protocol server for NES game debugging with BizHawk/EmuHawk emulator

IMPORTANT: All tools in this server operate ONLY on volatile emulator runtime state.
They do NOT modify files, server code, or configuration.

Key Advantages over Mesen:
- NO execution time limits - full AI control!
- client.pause() works perfectly
- No Virtual Pause/Hard Pause confusion
- Memory breakpoints with event.onmemory* callbacks
- Reliable command/response cycle

This server exposes BizHawk's debugging tools as MCP tools that can be used by AI agents.
"""

import json
import os
import sys
import time
import logging
from datetime import datetime
from typing import Any, Optional
import asyncio

# Setup logging - write to stderr AND file
LOG_DIR = os.path.dirname(os.path.abspath(__file__))
LOG_FILE = os.path.join(LOG_DIR, "bizhawk_mcp.log")

# Configure logging
logging.basicConfig(
    level=logging.DEBUG,
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[
        logging.FileHandler(LOG_FILE, encoding='utf-8'),
        logging.StreamHandler(sys.stderr)
    ]
)
logger = logging.getLogger("bizhawk_mcp")

logger.info("=" * 50)
logger.info("BizHawk MCP Server starting...")
logger.info(f"Log file: {LOG_FILE}")

# MCP imports
from mcp.server import Server
from mcp.types import Tool, TextContent
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

# Command ID counter
command_id = 0

# Timeout configuration - BizHawk is more reliable, can use shorter timeouts
DEFAULT_TIMEOUT = 10.0  # seconds
FAST_TIMEOUT = 5.0      # for simple operations

# Description suffix for all tools
RUNTIME_DISCLAIMER = " [EMULATOR RUNTIME ONLY: This tool operates only on volatile emulator runtime state and does NOT modify files, server code, or configuration.]"

# Trace log max (matches Lua traceMaxLines)
TRACE_MAX_LINES = 1000

# ═══════════════════════════════════════════════════════════════
# FREEZE FEATURE CONFIGURATION
# Set to True to enable freeze address feature, False to disable (default)
# This prevents AI from automatically relying on freeze functionality
# ═══════════════════════════════════════════════════════════════
FREEZE_FEATURE_ENABLED = False  # Default: Disabled - enable manually when needed


def normalize_address(addr: str) -> int:
    """Convert address string to integer for consistent comparison.
    Handles: 0x075A, $075A, 075A, 1882 (decimal)
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
    """Format address as canonical $XXXX hex string (matches Lua hexAddr)"""
    return f"${addr:04X}"


def get_next_id() -> int:
    """Get next command ID"""
    global command_id
    command_id += 1
    return command_id


def check_bizhawk_alive() -> tuple[bool, str]:
    """Check if BizHawk + Lua script are running by checking state file timestamp"""
    try:
        if not os.path.exists(STATE_FILE):
            return False, "State file not found. Start BizHawk, load ROM, then load bizhawk_debug_api.lua in Tools > Lua Console"
             
        with open(STATE_FILE, 'r') as f:
            state = json.load(f)
            timestamp = state.get('timestamp', 0)
            is_paused = state.get('isPaused', False)
            current_time = time.time()
            age = current_time - timestamp
            
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
SOCKET_TIMEOUT = 2.0  # seconds
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


def send_command_socket(action: str, **params) -> dict:
    """Send command via socket (fast path)"""
    sock = get_socket_connection()
    if sock is None:
        return None  # Signal to use file-based
    
    cmd_id = get_next_id()
    command = {"id": cmd_id, "action": action, **params}
    
    try:
        # Send command as JSON terminated by newline
        message = json.dumps(command) + "\n"
        sock.sendall(message.encode('utf-8'))
        logger.debug(f"[SOCKET] Sent: {action} (id={cmd_id})")
        
        # Receive response
        buffer = b""
        while b"\n" not in buffer:
            chunk = sock.recv(4096)
            if not chunk:
                raise ConnectionError("Connection closed")
            buffer += chunk
        
        response_line = buffer.split(b"\n")[0]
        response = json.loads(response_line.decode('utf-8'))
        logger.debug(f"[SOCKET] Response received for command {cmd_id}")
        return response
        
    except Exception as e:
        logger.warning(f"[SOCKET] Error: {e}, falling back to file-based")
        close_socket_connection()
        return None  # Signal to use file-based


def send_command_file(action: str, timeout: float = DEFAULT_TIMEOUT, **params) -> dict:
    """Send command via file (fallback)"""
    cmd_id = get_next_id()
    command = {"id": cmd_id, "action": action, **params}
    
    logger.debug(f"[FILE] Sending command: {action} (id={cmd_id})")
    
    try:
        with open(COMMAND_FILE, 'w') as f:
            json.dump(command, f)
        logger.debug(f"[FILE] Command written to {COMMAND_FILE}")
    except Exception as e:
        logger.error(f"[FILE] Failed to write command: {e}")
        return {"error": str(e), "commandId": cmd_id}
    
    # Wait for response
    poll_interval = 0.05  # 50ms polling
    max_polls = int(timeout / poll_interval)
    
    for i in range(max_polls):
        time.sleep(poll_interval)
        try:
            if os.path.exists(RESPONSE_FILE):
                with open(RESPONSE_FILE, 'r') as f:
                    response = json.load(f)
                    if response.get('commandId') == cmd_id:
                        logger.debug(f"[FILE] Response received for command {cmd_id}")
                        return response
        except Exception as e:
            if i == max_polls - 1:
                logger.warning(f"[FILE] Error reading response: {e}")
    
    logger.warning(f"[FILE] Timeout waiting for response to command {cmd_id}")
    return {"error": f"Timeout after {timeout}s. Check if BizHawk has Lua Console open and bizhawk_debug_api.lua loaded.", "commandId": cmd_id}


def send_command(action: str, timeout: float = DEFAULT_TIMEOUT, **params) -> dict:
    """Send command to BizHawk - tries socket first, then file
    
    Socket advantages:
    - Near-instant response (< 1ms)
    - No file I/O overhead
    
    Falls back to file-based if socket fails.
    """
    # First check if BizHawk is alive
    alive, message = check_bizhawk_alive()
    if not alive:
        logger.error(f"BizHawk not available: {message}")
        return {"error": message, "bizhawk_not_running": True}
    
    # Try socket first (fast path)
    result = send_command_socket(action, **params)
    if result is not None:
        return result
    
    # Fall back to file-based
    return send_command_file(action, timeout, **params)



def read_state() -> dict:
    """Read current BizHawk state"""
    try:
        with open(STATE_FILE, 'r') as f:
            return json.load(f)
    except Exception as e:
        logger.error(f"Cannot read state file: {e}")
        return {"error": f"Cannot read state file: {e}"}


# Create MCP Server
server = Server("bizhawk-nes")
logger.info("BizHawk MCP Server instance created")


@server.list_tools()
async def list_tools() -> list[Tool]:
    """List all available BizHawk debugging tools"""
    logger.debug("list_tools called")
    return [
        # ═══════════════════════════════════════════════════════════════
        # MEMORY TOOLS (Emulator Runtime Only)
        # ═══════════════════════════════════════════════════════════════
        Tool(
            name="emu_debug_read_memory",
            description="Read a byte from emulator RAM/ROM at the specified address. Address can be hex (0x075A or $075A) or decimal. Optional domain: 'System Bus', 'RAM', 'PRG ROM', etc." + RUNTIME_DISCLAIMER,
            inputSchema={
                "type": "object",
                "properties": {
                    "address": {"type": "string", "description": "Memory address (hex or decimal)"},
                    "domain": {"type": "string", "description": "Memory domain (optional, default='System Bus')"}
                },
                "required": ["address"]
            }
        ),
        Tool(
            name="emu_debug_write_memory",
            description="Write a byte to emulator RAM. Useful for modifying game values like lives, health, score in the running emulator." + RUNTIME_DISCLAIMER,
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
            description="Read a range of bytes from emulator memory. Returns array of values." + RUNTIME_DISCLAIMER,
            inputSchema={
                "type": "object",
                "properties": {
                    "address": {"type": "string", "description": "Start address"},
                    "length": {"type": "integer", "description": "Number of bytes to read"},
                    "domain": {"type": "string", "description": "Memory domain (optional)"}
                },
                "required": ["address", "length"]
            }
        ),
        Tool(
            name="emu_debug_search_memory",
            description="Search emulator RAM for addresses containing a specific value. Returns list of matching addresses." + RUNTIME_DISCLAIMER,
            inputSchema={
                "type": "object",
                "properties": {
                    "value": {"type": "integer", "description": "Value to search for (0-255)"}
                },
                "required": ["value"]
            }
        ),
        Tool(
            name="emu_debug_snapshot_memory",
            description="Take a snapshot of emulator RAM for later comparison. Use before the value changes." + RUNTIME_DISCLAIMER,
            inputSchema={
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Snapshot name (optional, default='default')"}
                }
            }
        ),
        Tool(
            name="emu_debug_compare_memory",
            description="Compare current emulator RAM to a snapshot. Filter: 'changed', 'increased', 'decreased', 'same'." + RUNTIME_DISCLAIMER,
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
            description="Get list of available emulator memory domains (RAM, PRG ROM, CHR ROM, etc.)." + RUNTIME_DISCLAIMER,
            inputSchema={"type": "object", "properties": {}}
        ),
        
        # ═══════════════════════════════════════════════════════════════
        # CPU TOOLS (Emulator Runtime Only)
        # ═══════════════════════════════════════════════════════════════
        Tool(
            name="emu_debug_get_cpu_state",
            description="Get current emulated CPU state including registers (A, X, Y, SP, PC) and flags (N, V, Z, C, etc)." + RUNTIME_DISCLAIMER,
            inputSchema={"type": "object", "properties": {}}
        ),
        Tool(
            name="emu_debug_get_cpu_registers",
            description="Get emulated CPU registers in hex format: A, X, Y, SP, PC, PS." + RUNTIME_DISCLAIMER,
            inputSchema={"type": "object", "properties": {}}
        ),
        
        # ═══════════════════════════════════════════════════════════════
        # EXECUTION CONTROL (Emulator Runtime Only)
        # ═══════════════════════════════════════════════════════════════
        Tool(
            name="emu_debug_pause",
            description="Pause emulator execution. Unlike Mesen, BizHawk pause is FULLY RELIABLE - all commands work perfectly when paused!" + RUNTIME_DISCLAIMER,
            inputSchema={"type": "object", "properties": {}}
        ),
        Tool(
            name="emu_debug_resume",
            description="Resume emulator execution." + RUNTIME_DISCLAIMER,
            inputSchema={"type": "object", "properties": {}}
        ),
        Tool(
            name="emu_debug_step",
            description="Step emulator execution. Types: 'frame' (one frame), 'instruction' (one CPU instruction via emu.yield)." + RUNTIME_DISCLAIMER,
            inputSchema={
                "type": "object",
                "properties": {
                    "count": {"type": "integer", "description": "Number of steps (default=1)"},
                    "stepType": {"type": "string", "description": "Step type: frame (default) or instruction"}
                }
            }
        ),
        Tool(
            name="emu_debug_frame_advance",
            description="Advance emulator by one or more frames. Shortcut for step with type='frame'." + RUNTIME_DISCLAIMER,
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
            description="Add a debug breakpoint in the emulator. Types: 'read' (when address is read), 'write' (written), 'execute' (code runs). Uses BizHawk's event.onmemory* callbacks." + RUNTIME_DISCLAIMER,
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
            description="List all active emulator breakpoints." + RUNTIME_DISCLAIMER,
            inputSchema={"type": "object", "properties": {}}
        ),
        Tool(
            name="emu_debug_remove_breakpoint",
            description="Remove an emulator breakpoint by ID." + RUNTIME_DISCLAIMER,
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
            description="Remove all emulator breakpoints." + RUNTIME_DISCLAIMER,
            inputSchema={"type": "object", "properties": {}}
        ),
        
        # Breakpoint hit tracking (AI-friendly)
        Tool(
            name="emu_debug_set_breakpoint_auto_pause",
            description="Enable/disable auto-pause mode in emulator. When disabled (default), breakpoints log hits but don't pause - perfect for AI automation." + RUNTIME_DISCLAIMER,
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
            description="Get list of all emulator breakpoint hits recorded." + RUNTIME_DISCLAIMER,
            inputSchema={"type": "object", "properties": {}}
        ),
        Tool(
            name="emu_debug_get_last_breakpoint_hit",
            description="Get the most recent emulator breakpoint hit with full CPU state." + RUNTIME_DISCLAIMER,
            inputSchema={"type": "object", "properties": {}}
        ),
        Tool(
            name="emu_debug_clear_breakpoint_hits",
            description="Clear the emulator breakpoint hit queue." + RUNTIME_DISCLAIMER,
            inputSchema={"type": "object", "properties": {}}
        ),
        
        # ═══════════════════════════════════════════════════════════════
        # DISASSEMBLY (Emulator Runtime Only)
        # ═══════════════════════════════════════════════════════════════
        Tool(
            name="emu_debug_disassemble",
            description="Get disassembly (assembly code) at an emulator memory address. Returns instruction bytes, mnemonic, and operand." + RUNTIME_DISCLAIMER,
            inputSchema={
                "type": "object",
                "properties": {
                    "address": {"type": "string", "description": "Start address for disassembly"},
                    "count": {"type": "integer", "description": "Number of instructions (default=20)"}
                },
                "required": ["address"]
            }
        ),
        Tool(
            name="emu_debug_get_current_instruction",
            description="Get the instruction at the current emulated Program Counter (PC)." + RUNTIME_DISCLAIMER,
            inputSchema={"type": "object", "properties": {}}
        ),
        
        # ═══════════════════════════════════════════════════════════════
        # TRACE LOGGER (Emulator Runtime Only)
        # ═══════════════════════════════════════════════════════════════
        Tool(
            name="emu_debug_start_trace",
            description="Start emulator trace logging - records every instruction executed." + RUNTIME_DISCLAIMER,
            inputSchema={"type": "object", "properties": {}}
        ),
        Tool(
            name="emu_debug_stop_trace",
            description="Stop emulator trace logging." + RUNTIME_DISCLAIMER,
            inputSchema={"type": "object", "properties": {}}
        ),
        Tool(
            name="emu_debug_get_trace",
            description="Get emulator trace log entries. Shows PC, instruction, and register values for each step." + RUNTIME_DISCLAIMER,
            inputSchema={
                "type": "object",
                "properties": {
                    "count": {"type": "integer", "description": "Number of entries to get (default=100)"}
                }
            }
        ),
        Tool(
            name="emu_debug_clear_trace",
            description="Clear the emulator trace log." + RUNTIME_DISCLAIMER,
            inputSchema={"type": "object", "properties": {}}
        ),
        
        # ═══════════════════════════════════════════════════════════════
        # WATCH LIST (Emulator Runtime Only)
        # ═══════════════════════════════════════════════════════════════
        Tool(
            name="emu_debug_add_watch",
            description="Add emulator memory address to watch list. Watched values are updated in real-time." + RUNTIME_DISCLAIMER,
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
            description="Get all watched emulator memory values with current readings." + RUNTIME_DISCLAIMER,
            inputSchema={"type": "object", "properties": {}}
        ),
        Tool(
            name="emu_debug_remove_watch",
            description="Remove address from emulator watch list." + RUNTIME_DISCLAIMER,
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
            description="Clear all emulator watches." + RUNTIME_DISCLAIMER,
            inputSchema={"type": "object", "properties": {}}
        ),
        
        # ═══════════════════════════════════════════════════════════════
        # STATE & UTILITIES (Emulator Runtime Only)
        # ═══════════════════════════════════════════════════════════════
        Tool(
            name="emu_debug_get_emulator_state",
            description="Get current emulator state: paused status, frame count, CPU registers, watched values." + RUNTIME_DISCLAIMER,
            inputSchema={"type": "object", "properties": {}}
        ),
        Tool(
            name="emu_debug_save_state",
            description="Save emulator state to a slot (in-memory save states, not disk files)." + RUNTIME_DISCLAIMER,
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
            description="Load emulator state from a slot." + RUNTIME_DISCLAIMER,
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
            description="Set emulator controller input for next frame. Useful for automated testing." + RUNTIME_DISCLAIMER,
            inputSchema={
                "type": "object",
                "properties": {
                    "buttons": {"type": "object", "description": "Button states: {A: true, B: false, Up: true, ...}"},
                    "player": {"type": "integer", "description": "Player number (1-4, default=1)"}
                }
            }
        ),
        Tool(
            name="emu_debug_raw_command",
            description="Send a raw command to the BizHawk emulator debug script." + RUNTIME_DISCLAIMER,
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
            description="Freeze an address to a constant value (writes single byte every frame to System Bus). Use for 'infinite lives', 'max health' cheats. Max 16 concurrent freezes. Note: byte-only (0-255)." + RUNTIME_DISCLAIMER,
            inputSchema={
                "type": "object",
                "properties": {
                    "address": {"type": "string", "description": "Memory address to freeze (hex or decimal)"},
                    "value": {"type": "integer", "description": "Value to maintain (0-255, single byte)"},
                    "label": {"type": "string", "description": "Optional friendly name for this freeze"},
                    "domain": {"type": "string", "description": "Memory domain (optional, default='System Bus')"}
                },
                "required": ["address", "value"]
            }
        ),
        Tool(
            name="emu_cheat_unfreeze_address",
            description="Remove a freeze by ID or address, or clear all freezes." + RUNTIME_DISCLAIMER,
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
            description="List all currently active address freezes." + RUNTIME_DISCLAIMER,
            inputSchema={"type": "object", "properties": {}}
        ),
        
        # ═══════════════════════════════════════════════════════════════
        # HIGH-LEVEL CHEAT TOOLS - Value Discovery (Emulator Runtime Only)
        # ═══════════════════════════════════════════════════════════════
        Tool(
            name="emu_cheat_find_decrementing_value",
            description="Find RAM addresses containing a value that decrements over time (e.g., lives, timer). Snapshots RAM, advances frames, finds decreased values." + RUNTIME_DISCLAIMER,
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
            description="Two-phase value finder: Phase 1 snapshots RAM and returns. Phase 2 (after user triggers event) compares and finds changed values. Use same snapshotName for both calls." + RUNTIME_DISCLAIMER,
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
            description="Narrow a list of candidate addresses using changed/unchanged/increased/decreased filter. Takes snapshot, you trigger change, call again with filter." + RUNTIME_DISCLAIMER,
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
            description="Set write breakpoint on address, run for N frames, summarize what code wrote to it. Auto-removes breakpoint after." + RUNTIME_DISCLAIMER,
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
            description="Trace execution for N frames and summarize hotspots (most frequently executed addresses)." + RUNTIME_DISCLAIMER,
            inputSchema={
                "type": "object",
                "properties": {
                    "frames": {"type": "integer", "description": "Frames to trace (default=60, max=300)"},
                    "topN": {"type": "integer", "description": "Number of hotspots to return (default=20)"}
                }
            }
        )
    ]


@server.call_tool()
async def call_tool(name: str, arguments: dict) -> list[TextContent]:
    """Handle tool calls"""
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
                                 value=arguments.get("value"))
        elif name == "emu_debug_snapshot_memory":
            result = send_command("memory.snapshot", name=arguments.get("name", "default"))
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
            result = send_command("breakpoint.remove", id=arguments.get("id"))
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
        # DISASSEMBLY
        # ═══════════════════════════════════════════════════════════════
        elif name == "emu_debug_disassemble":
            result = send_command("disasm.get",
                                 address=arguments.get("address"),
                                 count=arguments.get("count", 20))
        elif name == "emu_debug_get_current_instruction":
            result = send_command("disasm.current")
        
        # ═══════════════════════════════════════════════════════════════
        # TRACE LOGGER
        # ═══════════════════════════════════════════════════════════════
        elif name == "emu_debug_start_trace":
            result = send_command("trace.start")
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
            result = send_command(arguments.get("action"), **arguments.get("params", {}))
        
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
                                 id=arguments.get("freezeId"),
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
            
            if "error" in snap_result:
                result = snap_result
            else:
                # Step 2: Resume, advance frames, then pause again
                logger.debug(f"[find_decrementing_value] Resuming to advance {frames} frames")
                send_command("execution.resume", timeout=FAST_TIMEOUT)
                step_result = send_command("execution.step", count=frames, stepType="frame")
                logger.debug(f"[find_decrementing_value] Step result: stepped={step_result.get('data', {}).get('stepped', 'N/A')}")
                
                send_command("execution.pause", timeout=FAST_TIMEOUT)
                
                # Step 3: Compare for decreased values
                compare_result = send_command("memory.compare", name="decr_hunt", filter="decreased")
                logger.debug(f"[find_decrementing_value] Compare result: commandId={compare_result.get('commandId', 'N/A')}")
                
                if "error" in compare_result:
                    result = compare_result
                else:
                    candidates = compare_result.get("data", [])
                    # Filter by min decrement (diff is negative for decreased values)
                    filtered = [c for c in candidates if c.get("diff", 0) <= -min_decrement]
                    # Filter by initial value if specified
                    if initial_value is not None:
                        filtered = [c for c in filtered if c.get("old") == initial_value]
                    # Limit results
                    filtered = filtered[:200]
                    result = {
                        "candidates": filtered,
                        "totalFound": len(candidates),
                        "afterFilter": len(filtered),
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
                
                if "error" in compare_result:
                    result = compare_result
                else:
                    candidates = compare_result.get("data", [])[:200]
                    result = {
                        "phase": "comparison_complete",
                        "candidates": candidates,
                        "candidateCount": len(candidates),
                        "filter": filter_type,
                        "message": f"Found {len(candidates)} addresses that {filter_type}. Game is running."
                    }
        
        elif name == "emu_cheat_narrow_candidates":
            addresses = arguments.get("addresses", [])
            action = arguments.get("action", "snapshot")
            snapshot_name = arguments.get("snapshotName", "narrow")
            filter_type = arguments.get("filter", "changed")
            
            if action == "snapshot":
                # Read current values and store snapshot
                logger.debug(f"[narrow_candidates] Snapshot action: {len(addresses)} addresses, name='{snapshot_name}'")
                send_command("execution.pause", timeout=FAST_TIMEOUT)
                snap_result = send_command("memory.snapshot", name=snapshot_name)
                send_command("execution.resume", timeout=FAST_TIMEOUT)  # Resume so user can trigger change
                logger.debug("[narrow_candidates] Snapshot complete, game resumed")
                result = {
                    "action": "snapshot",
                    "addressCount": len(addresses),
                    "snapshotName": snapshot_name,
                    "message": "Snapshot taken. Game is running - trigger change, then call with action='filter' to narrow."
                }
            else:
                # Filter mode: compare and return only matching addresses
                logger.debug(f"[narrow_candidates] Filter action: {len(addresses)} addresses, filter='{filter_type}'")
                send_command("execution.pause", timeout=FAST_TIMEOUT)
                compare_result = send_command("memory.compare", name=snapshot_name, filter=filter_type)
                send_command("execution.resume", timeout=FAST_TIMEOUT)  # Resume after filtering
                logger.debug("[narrow_candidates] Filter complete, game resumed")
                
                if "error" in compare_result:
                    result = compare_result
                else:
                    changed_data = compare_result.get("data", [])
                    # Build set of changed addresses as integers for reliable comparison
                    changed_addr_ints = {c.get("address") for c in changed_data if c.get("address") is not None}
                    
                    # Filter original addresses using numeric comparison
                    narrowed = []
                    narrowed_details = []
                    for addr in addresses:
                        try:
                            addr_int = normalize_address(addr)
                            if addr_int in changed_addr_ints:
                                narrowed.append(format_address(addr_int))
                                # Find the details for this address
                                for c in changed_data:
                                    if c.get("address") == addr_int:
                                        narrowed_details.append(c)
                                        break
                        except (ValueError, TypeError):
                            continue  # Skip invalid addresses
                    
                    result = {
                        "action": "filter",
                        "originalCount": len(addresses),
                        "narrowedCount": len(narrowed),
                        "narrowed": narrowed,
                        "details": narrowed_details[:50],  # Include value changes
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
            if "error" in bp_result:
                result = bp_result
            else:
                bp_id = bp_result.get("data", {}).get("id")
                
                # Step 3: Resume and advance frames
                send_command("execution.resume", timeout=FAST_TIMEOUT)
                send_command("execution.step", count=frames, stepType="frame")
                send_command("execution.pause", timeout=FAST_TIMEOUT)
                
                # Step 4: Get hits
                hits_result = send_command("breakpoint.getHits")
                hits = hits_result.get("data", [])
                
                # Step 5: Remove breakpoint
                if bp_id:
                    send_command("breakpoint.remove", id=bp_id, timeout=FAST_TIMEOUT)
                
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
            frames = min(arguments.get("frames", 60), 300)
            top_n = arguments.get("topN", 20)
            
            # Step 1: Clear and start trace
            send_command("trace.clear", timeout=FAST_TIMEOUT)
            send_command("trace.start", timeout=FAST_TIMEOUT)
            
            # Step 2: Advance frames
            send_command("execution.resume", timeout=FAST_TIMEOUT)
            send_command("execution.step", count=frames, stepType="frame")
            send_command("execution.pause", timeout=FAST_TIMEOUT)
            
            # Step 3: Stop trace and get entries
            send_command("trace.stop", timeout=FAST_TIMEOUT)
            trace_result = send_command("trace.get", count=TRACE_MAX_LINES)  # Match Lua limit
            entries = trace_result.get("data", [])
            
            # Step 4: Aggregate by PC
            pc_counts = {}
            for entry in entries:
                pc = entry.get("pc", "unknown")
                asm = entry.get("asm", "???")
                if pc not in pc_counts:
                    pc_counts[pc] = {"pc": pc, "count": 0, "mnemonic": asm}
                pc_counts[pc]["count"] += 1
            
            sorted_hotspots = sorted(pc_counts.values(), key=lambda x: x["count"], reverse=True)[:top_n]
            
            result = {
                "framesTraced": frames,
                "instructionsLogged": len(entries),
                "uniqueAddresses": len(pc_counts),
                "hotspots": sorted_hotspots
            }
        
        else:
            result = {"error": f"Unknown tool: {name}"}
            logger.warning(f"Unknown tool: {name}")
        
        logger.info(f"Tool {name} completed successfully")
        
    except Exception as e:
        logger.error(f"Error in tool {name}: {e}", exc_info=True)
        result = {"error": str(e)}
    
    return [TextContent(type="text", text=json.dumps(result, indent=2))]


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
    print_err("IMPORTANT: All tools operate ONLY on volatile emulator runtime state.")
    print_err("           They do NOT modify files, server code, or configuration.")
    print_err("")
    print_err("Advantages over Mesen:")
    print_err("   - NO execution time limits!")
    print_err("   - client.pause() works perfectly")
    print_err("   - All commands work when paused")
    print_err("   - Full AI control")
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
