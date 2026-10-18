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
import base64
import io
import socket
import struct
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
from mcp.types import Tool, TextContent, ImageContent, ToolAnnotations, CallToolResult, ListToolsResult
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
# Multi-byte values (Lua checkSize / packValue)
SIZE_PROPERTY = {"type": "integer", "enum": [1, 2, 4],
                 "description": "Value width in bytes, little-endian (default 1). DS games mostly use 2 or 4."}
ALIGNED_PROPERTY = {"type": "boolean",
                    "description": "For size 2/4: only addresses that are a multiple of size (default true)"}
SIGNED_PROPERTY = {"type": "boolean",
                   "description": "Treat values as signed when judging increased/decreased (default false)"}

DS_BUTTONS = ["A", "B", "X", "Y", "L", "R", "Up", "Down", "Left", "Right", "Start", "Select"]
ENGINE_PROPERTY = {"type": "string", "enum": ["main", "sub"],
                   "description": "Graphics engine: 'main' (engine A, default) or 'sub' (engine B); emu_ds_read_powcnt says which screen each drives"}
FRAMES_PROPERTY = {"type": "integer", "minimum": 1, "description": "Frames to hold (default 8)"}

# Temporary file emu_ds_screenshot asks BizHawk to write (deleted after reading)
SCREENSHOT_FILE = os.path.join(BASE_PATH, "debug_screenshot.png")

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


def write_json_atomic(path: str, obj: dict) -> None:
    """Write JSON via a temp file + rename so Lua never parses a half-written file."""
    tmp = path + ".tmp"
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(obj, f)
    for i in range(20):
        try:
            os.replace(tmp, path)
            return
        except PermissionError:
            # Windows: Lua has the file open for reading this instant.
            if i == 19:
                raise
            time.sleep(0.01)


def check_bizhawk_alive() -> tuple[bool, str]:
    """Check if BizHawk + Lua script are running by checking state file timestamp"""
    if comm_bridge.connected():
        return True, "OK"  # the bridge is talking to us right now
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
# SOCKET TRANSPORT (BizHawk's built-in comm.socketServer* client)
# ═══════════════════════════════════════════════════════════════
#
# EmuHawk's Lua can only open outbound sockets, so this server listens and
# bizhawk_debug_api.lua connects to it, when EmuHawk was started with
# --socket_ip=127.0.0.1 --socket_port=<SOCKET_PORT>. The SOCKET CONFIGURATION
# block in the Lua script describes the protocol and the BizHawk quirks it
# works around. Without a live connection, commands use the JSON files.

SOCKET_HOST = "127.0.0.1"
# BIZHAWK_MCP_PORT: port to listen on (default 9876, matching --socket_port);
# "off" disables the listener; 0 picks any free port.
_port_setting = os.environ.get("BIZHAWK_MCP_PORT", "9876").strip().lower()
try:
    SOCKET_PORT = None if _port_setting in ("", "off", "none") else int(_port_setting)
except ValueError:
    logger.warning(f"BIZHAWK_MCP_PORT={_port_setting!r} is not a port number; socket listener off")
    SOCKET_PORT = None
SERVER_INFO_FILE = os.path.join(BASE_PATH, "debug_server.json")
HANDOVER_TIMEOUT = 2.0  # seconds for the bridge to pick a command up before using files
LIVE_WINDOW = 2.0       # the connection counts as live if the bridge spoke this recently

# struct linger {on, seconds=0}: close() aborts with RST instead of FIN.
_LINGER_ABORT = struct.pack("HH" if sys.platform == "win32" else "ii", 1, 0)


def _frame(payload: str) -> bytes:
    """BizHawk 2.6.2+ socket framing: "<byte length> <payload>"."""
    data = payload.encode("utf-8")
    return str(len(data)).encode("ascii") + b" " + data


class _FrameReader:
    """Buffered reader of framed messages. Not socket.makefile(): its extra
    reference defers close(), and with it the RST that _abort relies on."""

    def __init__(self, conn):
        self._conn = conn
        self._buf = bytearray()

    def _fill(self) -> None:
        chunk = self._conn.recv(65536)
        if not chunk:
            raise ConnectionError("connection closed")
        self._buf += chunk

    def read(self) -> str:
        while (sp := self._buf.find(b" ")) < 0:
            if len(self._buf) > 10:
                raise ConnectionError("bad frame prefix")
            self._fill()
        digits = bytes(self._buf[:sp])
        if not digits.isdigit():
            raise ConnectionError(f"bad frame prefix {digits!r}")
        end = sp + 1 + int(digits)
        while len(self._buf) < end:
            self._fill()
        data = bytes(self._buf[sp + 1:end])
        del self._buf[:end]
        return data.decode("utf-8", errors="replace")


def _abort(conn) -> None:
    """Close with RST, never a graceful FIN: BizHawk's ReceiveString spins
    forever if the peer closes gracefully while it reads a length prefix."""
    try:
        conn.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, _LINGER_ABORT)
    except OSError:
        pass
    try:
        conn.shutdown(socket.SHUT_RD)  # wakes a reader blocked in recv; sends nothing
    except OSError:
        pass
    try:
        conn.close()
    except OSError:
        pass


class CommBridge:
    """Listener and message pump for the Lua bridge's socket client.

    The bridge sends READY or "RESULT <json>" once per loop iteration and
    expects an immediate answer: NONE or the next command. Every message
    gets exactly one answer; after a missed answer the bridge sends another
    READY, so answers may run one behind, which is harmless because a
    command is only ever handed over once. send() parks a command for the
    reader thread to hand over, then waits for its RESULT.
    """

    def __init__(self):
        self._cond = threading.Condition()
        self._stop = threading.Event()
        self._listener = None
        self._conn = None
        self._gen = 0             # connection generation; readers of old ones exit
        self._last_heard = 0.0    # time.monotonic() of the bridge's last message
        self._outbox = None       # command waiting to be handed over
        self._handed = None       # id of the command the bridge is running
        self._results = {}
        self.port = None

    def start(self, host: str = SOCKET_HOST, port: int | None = None) -> bool:
        port = SOCKET_PORT if port is None else port
        if port is None:
            return False
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        if os.name != "nt":
            # POSIX: allow rebinding over TIME_WAIT (on Windows this flag
            # would instead let another process share the port).
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            listener.bind((host, port))
            listener.listen(1)
        except OSError as e:
            listener.close()
            logger.warning(f"[SOCKET] Cannot listen on {host}:{port} ({e}); using file IPC only")
            return False
        self._listener = listener
        self.port = listener.getsockname()[1]
        threading.Thread(target=self._accept_loop, daemon=True, name="bizhawk-accept").start()
        threading.Thread(target=self._announce_loop, daemon=True, name="bizhawk-announce").start()
        logger.info(f"[SOCKET] Listening on {host}:{self.port} for EmuHawk (--socket_port={self.port})")
        return True

    def stop(self) -> None:
        self._stop.set()
        if self._listener:
            self._listener.close()
        with self._cond:
            conn, self._conn = self._conn, None
            self._gen += 1
            self._fail_handed("The MCP server stopped")
            self._cond.notify_all()
        if conn:
            _abort(conn)
        try:
            with open(SERVER_INFO_FILE, encoding="utf-8") as f:
                mine = json.load(f).get("pid") == os.getpid()
            if mine:
                os.remove(SERVER_INFO_FILE)
        except (OSError, ValueError):
            pass

    def connected(self) -> bool:
        with self._cond:
            return self._conn is not None and time.monotonic() - self._last_heard < LIVE_WINDOW

    def send(self, command: dict, timeout: float) -> dict | None:
        """Run a command over the socket. None means it never reached the
        bridge, so the caller may safely use file IPC instead. Once handed
        over it is never re-sent: the bridge may be running it."""
        cid = command["id"]
        with self._cond:
            if self._conn is None or time.monotonic() - self._last_heard >= LIVE_WINDOW:
                return None
            self._outbox = command
            handover_deadline = time.monotonic() + HANDOVER_TIMEOUT
            while self._outbox is command:
                remaining = handover_deadline - time.monotonic()
                if self._conn is None or remaining <= 0:
                    self._outbox = None
                    return None
                self._cond.wait(remaining)
            deadline = time.monotonic() + timeout
            while cid not in self._results:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    if self._handed == cid:
                        self._handed = None  # a late RESULT is then ignored
                    return {"error": f"Timeout after {timeout}s waiting for BizHawk over the socket. "
                                     "The command may still be running; not re-sending.", "commandId": cid}
                self._cond.wait(remaining)
            return self._results.pop(cid)

    def _fail_handed(self, reason: str) -> None:
        """Caller holds the lock."""
        if self._handed is not None:
            self._results[self._handed] = {
                "error": f"{reason} while BizHawk had the command; not re-sending (it may have run).",
                "commandId": self._handed}
            self._handed = None

    def _accept_loop(self) -> None:
        while not self._stop.is_set():
            try:
                conn, _ = self._listener.accept()
            except OSError:
                return  # listener closed
            # Set from the start so that even this process dying aborts with RST.
            conn.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, _LINGER_ABORT)
            conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            with self._cond:
                old, self._conn = self._conn, conn
                self._gen += 1
                gen = self._gen
                self._last_heard = 0.0
                # The bridge only reconnects from its main loop, so a command
                # handed over on the old connection lost its result.
                self._fail_handed("BizHawk reconnected")
                self._cond.notify_all()
            if old:
                _abort(old)
            logger.info("[SOCKET] EmuHawk connected")
            threading.Thread(target=self._reader, args=(conn, gen), daemon=True,
                             name="bizhawk-reader").start()

    def _reader(self, conn, gen: int) -> None:
        frames = _FrameReader(conn)
        try:
            while True:
                msg = frames.read()
                with self._cond:
                    if gen != self._gen:
                        return
                    self._last_heard = time.monotonic()
                    if msg.startswith("RESULT "):
                        try:
                            resp = json.loads(msg[len("RESULT "):])
                        except ValueError:
                            resp = {"error": "Unreadable result from the bridge",
                                    "commandId": self._handed}
                        if self._handed is not None and resp.get("commandId") == self._handed:
                            self._results[self._handed] = resp
                            self._handed = None
                    reply = "NONE"
                    if self._outbox is not None and self._handed is None:
                        reply = json.dumps(self._outbox)
                        self._handed = self._outbox["id"]
                        self._outbox = None
                    self._cond.notify_all()
                    # Answer at once, under the lock, so a hand-over and its
                    # send can't interleave with another connection's.
                    conn.sendall(_frame(reply))
        except (OSError, ConnectionError) as e:
            logger.info(f"[SOCKET] EmuHawk connection ended ({e})")
        finally:
            with self._cond:
                if gen == self._gen:
                    self._conn = None
                    self._fail_handed("Lost the connection to BizHawk")
                    self._cond.notify_all()
            _abort(conn)

    def _announce_loop(self) -> None:
        """Refresh debug_server.json every second. The bridge only dials a
        server that announced itself recently, because connecting to a port
        nobody listens on can stall EmuHawk for seconds on Windows."""
        while not self._stop.is_set():
            try:
                write_json_atomic(SERVER_INFO_FILE, {"port": self.port, "pid": os.getpid(),
                                                     "timestamp": int(time.time())})
            except OSError as e:
                logger.debug(f"[SOCKET] Cannot write {SERVER_INFO_FILE}: {e}")
            self._stop.wait(1.0)


comm_bridge = CommBridge()


def build_command(action: str, cmd_id: int, params: dict) -> dict:
    """The envelope's id/action always win over same-named params (a param
    called "id" used to replace the command id and the reply was never matched)."""
    return {**params, "id": cmd_id, "action": action}


def send_command_file(command: dict, timeout: float) -> dict:
    """Send command via file (fallback)"""
    action, cmd_id = command["action"], command["id"]
    logger.debug(f"[FILE] Sending command: {action} (id={cmd_id})")

    try:
        write_json_atomic(COMMAND_FILE, command)
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

    command = build_command(action, get_next_id(), params)

    # Try socket first (fast path)
    result = comm_bridge.send(command, timeout)
    if result is not None:
        return result

    # Fall back to file-based
    return send_command_file(command, timeout)


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
            description="Read a 1, 2 or 4-byte little-endian value from emulator memory. Address can be hex (0x02000000 or $02000000) or decimal. The default domain 'ARM9 System Bus' takes physical DS addresses (0x02000000 main RAM, 0x04000000 I/O, 0x05000000 palette, 0x06000000 VRAM, 0x07000000 OAM); other domains such as 'Main RAM' take offsets.",
            inputSchema={
                "type": "object",
                "properties": {
                    "address": {"type": "string", "description": "Memory address (hex or decimal)"},
                    "domain": {"type": "string", "description": "Memory domain (optional, default='ARM9 System Bus')"},
                    "size": SIZE_PROPERTY
                },
                "required": ["address"]
            }
        ),
        Tool(
            name="emu_debug_write_memory",
            description="Write a 1, 2 or 4-byte little-endian value to emulator memory. Useful for modifying game values like lives, health, score in the running emulator.",
            inputSchema={
                "type": "object",
                "properties": {
                    "address": {"type": "string", "description": "Memory address (hex or decimal)"},
                    "value": {"type": "integer", "description": "Value to write; must fit in size bytes (negative values are written as two's complement)"},
                    "domain": {"type": "string", "description": "Memory domain (optional)"},
                    "size": SIZE_PROPERTY
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
            description="Search memory for a 1, 2 or 4-byte little-endian value. Defaults to all of main RAM (0x02000000-0x023FFFFF on ARM9 System Bus). Returns up to 1000 matching addresses; 'total' gives the full count.",
            inputSchema={
                "type": "object",
                "properties": {
                    "value": {"type": "integer", "description": "Value to search for; must fit in size bytes (negative values match their two's complement)"},
                    "size": SIZE_PROPERTY,
                    "aligned": ALIGNED_PROPERTY,
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
            description="Compare current memory to a snapshot, as 1, 2 or 4-byte values (any width works with any snapshot). Filter: 'changed', 'increased', 'decreased', 'same'. Returns up to 1000 entries; 'total' gives the full count.",
            inputSchema={
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Snapshot name to compare to"},
                    "filter": {"type": "string", "description": "Filter type: changed/increased/decreased/same"},
                    "size": SIZE_PROPERTY,
                    "signed": SIGNED_PROPERTY,
                    "aligned": ALIGNED_PROPERTY
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
                    "minDecrement": {"type": "integer", "description": "Minimum decrease amount (default=1)"},
                    "size": SIZE_PROPERTY
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
                    "filter": {"type": "string", "description": "For phase 2: changed/increased/decreased (default='changed')"},
                    "size": SIZE_PROPERTY
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
                    "snapshotName": {"type": "string", "description": "Snapshot name (default='narrow')"},
                    "size": SIZE_PROPERTY
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
        ),

        # ═══════════════════════════════════════════════════════════════
        # NINTENDO DS TOOLS (typed wrappers around the bridge's DS actions)
        # ═══════════════════════════════════════════════════════════════
        Tool(
            name="emu_ds_screenshot",
            description="Capture the emulator display and return it as an image you can look at (e.g. to check on-screen text). 'top'/'bottom' crop to one DS screen (needs BizHawk's vertical screen layout); scale upsizes with nearest-neighbour so pixel fonts stay sharp.",
            inputSchema={
                "type": "object",
                "properties": {
                    "screen": {"type": "string", "enum": ["both", "top", "bottom"], "description": "Which screen(s) to return (default 'both')"},
                    "scale": {"type": "integer", "minimum": 1, "maximum": 4, "description": "Integer upscale factor (default 2)"},
                    "savePath": {"type": "string", "description": "Optional .png path on the host to also keep the full-resolution, uncropped frame"}
                }
            }
        ),
        Tool(
            name="emu_ds_press_button",
            description="Hold one DS button for N frames, then release it (emulation runs meanwhile).",
            inputSchema={
                "type": "object",
                "properties": {
                    "button": {"type": "string", "enum": DS_BUTTONS},
                    "frames": FRAMES_PROPERTY
                },
                "required": ["button"]
            }
        ),
        Tool(
            name="emu_ds_touch",
            description="Touch the bottom screen at (x, y) for N frames, then release. Needs the mouse-unbinding workaround in NDS_SETUP.md, or taps land at the mouse cursor.",
            inputSchema={
                "type": "object",
                "properties": {
                    "x": {"type": "integer", "minimum": 0, "maximum": 255},
                    "y": {"type": "integer", "minimum": 0, "maximum": 191},
                    "frames": FRAMES_PROPERTY
                },
                "required": ["x", "y"]
            }
        ),
        Tool(
            name="emu_ds_touch_release",
            description="Release the touchscreen and clear the sticky Touch X / Touch Y holds.",
            inputSchema={"type": "object", "properties": {}}
        ),
        Tool(
            name="emu_ds_input_sequence",
            description="Run several input steps in one call (one round trip for a whole menu navigation). Use 'sleep' between screen transitions so the next input lands after the game settles; prefer button presses over touches for 'touch to continue' screens.",
            inputSchema={
                "type": "object",
                "properties": {
                    "steps": {
                        "type": "array",
                        "minItems": 1,
                        "items": {
                            "type": "object",
                            "properties": {
                                "action": {"type": "string", "enum": ["press", "touch", "wait", "sleep"],
                                           "description": "press a button, touch (x, y), wait N emulated frames, or sleep N seconds while emulation runs (60 frames per second; wall-clock only if LuaSocket is installed in BizHawk)"},
                                "button": {"type": "string", "enum": DS_BUTTONS, "description": "For press"},
                                "x": {"type": "integer", "minimum": 0, "maximum": 255, "description": "For touch"},
                                "y": {"type": "integer", "minimum": 0, "maximum": 191, "description": "For touch"},
                                "frames": {"type": "integer", "minimum": 1, "description": "For press/touch/wait (default 8)"},
                                "seconds": {"type": "number", "exclusiveMinimum": 0, "description": "For sleep"}
                            },
                            "required": ["action"]
                        }
                    }
                },
                "required": ["steps"]
            }
        ),
        Tool(
            name="emu_ds_reset",
            description="Hard-reset (power-cycle) the emulated DS and release all buttons. Unsaved game progress is lost.",
            inputSchema={"type": "object", "properties": {}}
        ),
        Tool(
            name="emu_ds_close_rom",
            description="Close the loaded ROM, releasing the file so a patcher can overwrite it. Unsaved progress is lost.",
            inputSchema={"type": "object", "properties": {}}
        ),
        Tool(
            name="emu_ds_open_rom",
            description="Load a ROM by absolute path; pair with emu_ds_close_rom for a patch-and-reload cycle.",
            inputSchema={
                "type": "object",
                "properties": {"path": {"type": "string", "description": "Absolute path to the .nds file"}},
                "required": ["path"]
            }
        ),
        Tool(
            name="emu_ds_dump_memory",
            description="Write raw memory bytes to a file on the host (for large regions such as VRAM or a whole RAM image); overwrites the file.",
            inputSchema={
                "type": "object",
                "properties": {
                    "address": {"type": "string", "description": "Start address (hex or decimal)"},
                    "length": {"type": "integer", "minimum": 1, "description": "Number of bytes"},
                    "path": {"type": "string", "description": "Output file path on the host"},
                    "domain": {"type": "string", "description": "Memory domain (optional, default='ARM9 System Bus')"}
                },
                "required": ["address", "length", "path"]
            }
        ),
        Tool(
            name="emu_ds_read_powcnt",
            description="Read POWCNT1: which graphics engine drives which screen (LCD swap) and which engines are enabled.",
            inputSchema={"type": "object", "properties": {}}
        ),
        Tool(
            name="emu_ds_decode_dispcnt",
            description="Decode an engine's DISPCNT: BG mode, BG0-3 and OBJ enables, OBJ tile mapping and boundary, display mode.",
            inputSchema={"type": "object", "properties": {"engine": ENGINE_PROPERTY}}
        ),
        Tool(
            name="emu_ds_decode_vramcnt",
            description="Decode VRAMCNT_A..I: which VRAM banks are enabled and how each is mapped (MST / offset).",
            inputSchema={"type": "object", "properties": {}}
        ),
        Tool(
            name="emu_ds_read_bg",
            description="Read one text BG layer: its BGxCNT fields, map and tile base addresses, and the 32x24 tilemap entries (useful for locating text tiles).",
            inputSchema={
                "type": "object",
                "properties": {
                    "engine": ENGINE_PROPERTY,
                    "index": {"type": "integer", "minimum": 0, "maximum": 3, "description": "BG layer 0-3 (default 0)"}
                }
            }
        ),
        Tool(
            name="emu_ds_parse_oam",
            description="List the visible sprites in an engine's OAM: position, tile, size in tiles, palette, priority, rotation flag.",
            inputSchema={"type": "object", "properties": {"engine": ENGINE_PROPERTY}}
        ),
        Tool(
            name="emu_ds_input_debug",
            description="Dump the joypad key names and current values the core exposes (to check names such as 'Touch X').",
            inputSchema={"type": "object", "properties": {}}
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
    "emu_ds_read_powcnt", "emu_ds_decode_dispcnt", "emu_ds_decode_vramcnt", "emu_ds_read_bg",
    "emu_ds_parse_oam", "emu_ds_input_debug",
}
DESTRUCTIVE_TOOLS = {
    "emu_debug_save_state", "emu_debug_load_state", "emu_debug_raw_command",
    "emu_ds_reset", "emu_ds_close_rom", "emu_ds_open_rom", "emu_ds_dump_memory",
}


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
                                 domain=arguments.get("domain"),
                                 size=arguments.get("size"))
        elif name == "emu_debug_write_memory":
            result = send_command("memory.write",
                                 address=arguments.get("address"),
                                 value=arguments.get("value"),
                                 domain=arguments.get("domain"),
                                 size=arguments.get("size"))
        elif name == "emu_debug_read_memory_range":
            result = send_command("memory.readRange", 
                                 address=arguments.get("address"),
                                 length=arguments.get("length"),
                                 domain=arguments.get("domain"))
        elif name == "emu_debug_search_memory":
            result = send_command("memory.search",
                                 value=arguments.get("value"),
                                 **pick(arguments, "domain", "start", "length", "size", "aligned"))
        elif name == "emu_debug_snapshot_memory":
            result = send_command("memory.snapshot", name=arguments.get("name", "default"),
                                  **pick(arguments, "domain", "start", "length"))
        elif name == "emu_debug_compare_memory":
            result = send_command("memory.compare", 
                                 name=arguments.get("name", "default"),
                                 filter=arguments.get("filter", "changed"),
                                 **pick(arguments, "size", "signed", "aligned"))
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
                                              minDelta=min_decrement, oldValue=initial_value,
                                              size=arguments.get("size"))
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
                compare_result = send_command("memory.compare", name=snapshot_name, filter=filter_type,
                                              size=arguments.get("size"))
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

            size = arguments.get("size") or 1
            if not addr_ints:
                result = {"error": "No valid candidate addresses given"}
            elif max(addr_ints) - min(addr_ints) + size > 0x400000:
                result = {"error": "Candidate addresses span more than 4 MB; narrow them in batches"}
            elif action == "snapshot":
                start = min(addr_ints)
                span = max(addr_ints) - start + size
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
                                              addresses=addr_ints, size=size)
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
        
        # ═══════════════════════════════════════════════════════════════
        # NINTENDO DS TOOLS
        # ═══════════════════════════════════════════════════════════════
        elif name == "emu_ds_screenshot":
            result = take_screenshot(arguments)
        elif name == "emu_ds_press_button":
            result = send_command("input.press", button=arguments.get("button"),
                                  frames=arguments.get("frames", 8))
        elif name == "emu_ds_touch":
            result = send_command("input.touch", x=arguments.get("x"), y=arguments.get("y"),
                                  frames=arguments.get("frames", 8))
        elif name == "emu_ds_touch_release":
            result = send_command("input.touch_release")
        elif name == "emu_ds_input_sequence":
            result = send_command("input.sequence", seq=build_sequence(arguments.get("steps") or []))
        elif name == "emu_ds_reset":
            result = send_command("system.reset")
        elif name == "emu_ds_close_rom":
            result = send_command("system.close_rom")
        elif name == "emu_ds_open_rom":
            result = send_command("system.open_rom", path=arguments.get("path"))
        elif name == "emu_ds_dump_memory":
            result = send_command("memory.dump_to_file", address=arguments.get("address"),
                                  length=arguments.get("length"), path=arguments.get("path"),
                                  domain=arguments.get("domain"))
        elif name == "emu_ds_read_powcnt":
            result = send_command("powcnt.read")
        elif name == "emu_ds_decode_dispcnt":
            result = send_command("dispcnt.decode", engine=arguments.get("engine", "main"))
        elif name == "emu_ds_decode_vramcnt":
            result = send_command("vramcnt.decode")
        elif name == "emu_ds_read_bg":
            result = send_command("bg.read", engine=arguments.get("engine", "main"),
                                  index=arguments.get("index", 0))
        elif name == "emu_ds_parse_oam":
            result = send_command("oam.parse", engine=arguments.get("engine", "main"))
        elif name == "emu_ds_input_debug":
            result = send_command("input.debug")

        else:
            result = {"error": f"Unknown tool: {name}"}
            logger.warning(f"Unknown tool: {name}")
        
        logger.info(f"Tool {name} completed successfully")
        
    except Exception as e:
        logger.error(f"Error in tool {name}: {e}", exc_info=True)
        result = {"error": str(e)}

    return result


def pick(arguments: dict, *keys: str) -> dict:
    """The given optional arguments that were actually set"""
    return {k: arguments[k] for k in keys if arguments.get(k) is not None}


class ToolOutput:
    """A tool result plus PNG images to return as MCP image content"""

    def __init__(self, result: dict, images: list[bytes]):
        self.result = result
        self.images = images


def process_screenshot(png: bytes, screen: str, scale: int) -> tuple[bytes, int, int]:
    """Crop a BizHawk NDS frame to one screen and/or upscale it (nearest
    neighbour, so pixel fonts stay sharp). Returns (png, width, height)."""
    if png[:8] != b"\x89PNG\r\n\x1a\n":
        raise ValueError("BizHawk did not write a PNG")
    width, height = struct.unpack(">II", png[16:24])  # IHDR
    if screen == "both" and scale == 1:
        return png, width, height
    try:
        from PIL import Image
    except ImportError:
        raise RuntimeError("Cropping or scaling needs Pillow (pip install -r requirements.txt); "
                           "screen='both' with scale=1 works without it") from None
    img = Image.open(io.BytesIO(png))
    if screen != "both":
        # Vertical layout: the two screens stacked (maybe with a gap), each
        # 256x192 times the core's resolution multiplier.
        k = width // 256
        screen_h = 192 * k
        if k < 1 or width % 256 or height < 2 * screen_h:
            raise ValueError(f"screen='{screen}' needs BizHawk's vertical NDS screen layout "
                             f"(screens stacked); this frame is {width}x{height}. Use screen='both'.")
        top = 0 if screen == "top" else height - screen_h
        img = img.crop((0, top, width, top + screen_h))
    if scale > 1:
        img = img.resize((img.width * scale, img.height * scale), Image.Resampling.NEAREST)
    out = io.BytesIO()
    img.save(out, "PNG")
    return out.getvalue(), img.width, img.height


def take_screenshot(arguments: dict):
    screen = arguments.get("screen", "both")
    scale = arguments.get("scale", 2)
    save_path = arguments.get("savePath")
    if screen not in ("both", "top", "bottom"):
        return {"error": "screen must be 'both', 'top' or 'bottom'"}
    if scale not in (1, 2, 3, 4):
        return {"error": "scale must be an integer 1-4"}
    if save_path and not save_path.lower().endswith(".png"):
        return {"error": "savePath must end in .png"}
    path = os.path.abspath(save_path) if save_path else SCREENSHOT_FILE

    def remove_temp():
        if not save_path:
            try:
                os.remove(path)
            except FileNotFoundError:
                pass

    remove_temp()  # never hand back a stale frame
    response = send_command("screenshot.take", path=path)
    data = data_of(response)
    if data is None:
        return response
    try:
        with open(path, "rb") as f:
            png = f.read()
    finally:
        remove_temp()
    png, width, height = process_screenshot(png, screen, scale)
    result = {"success": True, "screen": screen, "scale": scale, "width": width, "height": height,
              "frameCount": data.get("frameCount")}
    if save_path:
        result["savedTo"] = path
    return ToolOutput(result, [png])


def build_sequence(steps: list) -> str:
    """Typed input steps -> the bridge's flat input.sequence string"""
    parts = []
    for i, step in enumerate(steps):
        kind = step.get("action")
        frames = int(step.get("frames") or 8)
        if kind == "press":
            if step.get("button") not in DS_BUTTONS:
                raise ValueError(f"step {i}: press needs a button, one of {', '.join(DS_BUTTONS)}")
            parts.append(f"press {step['button']} {frames}")
        elif kind == "touch":
            x, y = step.get("x"), step.get("y")
            if not (isinstance(x, int) and 0 <= x <= 255 and isinstance(y, int) and 0 <= y <= 191):
                raise ValueError(f"step {i}: touch needs integer x 0-255 and y 0-191")
            parts.append(f"touch {x} {y} {frames}")
        elif kind == "wait":
            parts.append(f"wait {frames}")
        elif kind == "sleep":
            seconds = step.get("seconds")
            if not isinstance(seconds, (int, float)) or seconds <= 0:
                raise ValueError(f"step {i}: sleep needs seconds > 0")
            parts.append(f"sleep {seconds:g}")
        else:
            raise ValueError(f"step {i}: unknown action {kind!r}")
    return "; ".join(parts)


async def call_tool(name: str, arguments: dict) -> CallToolResult:
    """Handle tool calls off the event loop (bridge calls block for up to
    DEFAULT_TIMEOUT), one at a time, flagging failures with isError."""
    def locked():
        with _bridge_lock:
            return run_tool(name, arguments or {})

    result = await asyncio.to_thread(locked)
    images = []
    if isinstance(result, ToolOutput):
        result, images = result.result, result.images
    is_error = isinstance(result, dict) and ("error" in result or result.get("success") is False)
    content = [ImageContent(type="image", data=base64.b64encode(png).decode("ascii"), mimeType="image/png")
               for png in images]
    content.append(TextContent(type="text", text=json.dumps(result, indent=2)))
    return CallToolResult(content=content, isError=is_error)


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
                raise ToolCallError(result.content[-1].text)  # reported with isError=True
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
    comm_bridge.start()
    try:
        async with stdio_server() as (read_stream, write_stream):
            logger.info("stdio_server started, running server...")
            await server.run(read_stream, write_stream, server.create_initialization_options())
    except Exception as e:
        logger.error(f"Server error: {e}", exc_info=True)
        raise
    finally:
        comm_bridge.stop()


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
    print_err("   1. Open BizHawk and load a ROM (optionally start EmuHawk with")
    print_err(f"      --socket_ip=127.0.0.1 --socket_port={SOCKET_PORT} for the socket fast path)")
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
