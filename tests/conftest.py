"""Test harness: runs src/bizhawk_debug_api.lua under Lua 5.4 (via lupa)
against tests/bizhawk_mock.lua, talking to it over the file-IPC path."""

import json
import os
import shutil
import socket
import threading
import time

import pytest
from lupa.lua54 import LuaRuntime

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LUA_SCRIPT = os.path.join(ROOT, "src", "bizhawk_debug_api.lua")
MOCK = os.path.join(ROOT, "tests", "bizhawk_mock.lua")


class FakeBizHawkSocketClient:
    """Python port of BizHawk 2.11.1's SocketServer (the client behind Lua's
    comm.socketServer*), installed as the bridge's `comm` global.

    Faithful where it matters: it connects on construction and on every
    port/IP change, each connect makes a socket with an infinite receive
    timeout, frames are "<byte length> <payload>", receive errors return "",
    and IsConnected just reports that a connect once succeeded. Where
    BizHawk would spin forever (the peer closing gracefully during a length
    prefix) it records `hang_hazard` instead.
    """

    def __init__(self, ip, port):
        self.ip, self.port = ip, port
        self.sock = None
        self.connected = False
        self.hang_hazard = False
        self._connect()

    def _connect(self):
        s = socket.create_connection((self.ip, self.port))  # raises, like _soc.Connect
        s.settimeout(None)
        self.sock = s
        self.connected = True

    def socketServerGetInfo(self):
        return f"{self.ip}:{self.port}"

    def socketServerIsConnected(self):
        return self.connected

    def socketServerSetTimeout(self, ms):
        self.sock.settimeout(ms / 1000 if ms else None)

    def socketServerSetPort(self, port):
        self.port = int(port)
        self._connect()

    def socketServerSetIp(self, ip):
        self.ip = ip
        self._connect()

    def socketServerSend(self, msg):
        data = msg.encode("utf-8") if isinstance(msg, str) else bytes(msg)
        try:
            self.sock.sendall(str(len(data)).encode() + b" " + data)
            return len(data) + len(str(len(data))) + 1
        except OSError:
            return -1

    def socketServerResponse(self):
        try:
            digits = b""
            while True:
                b = self.sock.recv(1)
                if not b:
                    self.hang_hazard = True  # BizHawk: infinite loop here
                    return ""
                if b == b" ":
                    break
                digits += b
            n, buf = int(digits), b""
            while len(buf) < n:
                chunk = self.sock.recv(n - len(buf))
                if not chunk:
                    return ""
                buf += chunk
            return buf.decode("utf-8")
        except (OSError, ValueError):
            return ""


class Bridge:
    """The Lua bridge running inside a simulated EmuHawk host loop."""

    def __init__(self, workdir, comm=None):
        self.dir = str(workdir)
        self.script = os.path.join(self.dir, "bizhawk_debug_api.lua")
        shutil.copy(LUA_SCRIPT, self.script)
        self.lua = LuaRuntime(unpack_returned_tuples=True)
        self.mock = self.lua.eval(f"dofile({json.dumps(MOCK)})")
        if comm is not None:
            self.lua.globals().comm = comm  # EmuHawk started with --socket_ip/--socket_port
        self.mock.Host.start(self.script)
        self.next_id = 1000
        self.lock = threading.Lock()
        self.tick()

    def path(self, name):
        return os.path.join(self.dir, name)

    def tick(self, n=1):
        with self.lock:
            for _ in range(n):
                self.mock.Host.tick()

    @property
    def stuck(self):
        return self.mock.Host.stuck

    def send(self, payload, max_ticks=5000):
        """Write a raw command dict, tick until the matching response shows up."""
        with open(self.path("debug_commands.json"), "w", encoding="utf-8") as f:
            json.dump(payload, f)
        stuck_before = self.stuck
        for _ in range(max_ticks):
            self.tick()
            if self.stuck > stuck_before:
                raise TimeoutError(f"bridge blocked in frameadvance while paused ({payload['action']})")
            try:
                with open(self.path("debug_response.json"), encoding="utf-8") as f:
                    resp = json.load(f)
            except (OSError, ValueError):
                continue
            if resp.get("commandId") == payload["id"]:
                return resp
        raise TimeoutError(f"no response to {payload}")

    def command(self, action, **params):
        self.next_id += 1
        return self.send({**params, "id": self.next_id, "action": action})

    def logs(self):
        return [self.mock.logs[i] for i in range(1, len(self.mock.logs) + 1)]


@pytest.fixture
def bridge(tmp_path):
    return Bridge(tmp_path)


def wait_for(predicate, timeout=10.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return False


def _server_module():
    import importlib
    import sys

    sys.path.insert(0, os.path.join(ROOT, "src"))
    return importlib.import_module("bizhawk_mcp_server")


@pytest.fixture
def server_factory(tmp_path, monkeypatch):
    """Build the Python MCP server module wired to a bridge ticking in a
    thread; with socket=True EmuHawk runs with the socket flags."""
    srv = _server_module()
    stops = []

    def make(socket_mode=False):
        monkeypatch.setattr(srv, "COMMAND_FILE", str(tmp_path / "debug_commands.json"))
        monkeypatch.setattr(srv, "RESPONSE_FILE", str(tmp_path / "debug_response.json"))
        monkeypatch.setattr(srv, "STATE_FILE", str(tmp_path / "debug_state.json"))
        monkeypatch.setattr(srv, "SERVER_INFO_FILE", str(tmp_path / "debug_server.json"))
        monkeypatch.setattr(srv, "DEFAULT_TIMEOUT", 5.0)
        monkeypatch.setattr(srv, "FAST_TIMEOUT", 5.0)
        comm_bridge = srv.CommBridge()
        monkeypatch.setattr(srv, "comm_bridge", comm_bridge)
        fake = None
        if socket_mode:
            # The MCP server listens before EmuHawk starts (EmuHawk connects
            # at startup and fails fatally if nobody is listening).
            assert comm_bridge.start(port=0)
            fake = FakeBizHawkSocketClient("127.0.0.1", comm_bridge.port)
            stops.append(comm_bridge.stop)
        b = Bridge(tmp_path, comm=fake)

        stop = threading.Event()

        def run():
            while not stop.is_set():
                b.tick()
                time.sleep(0.0005)

        t = threading.Thread(target=run, daemon=True)
        t.start()
        stops.append(lambda: (stop.set(), t.join(timeout=2)))
        wait_for(lambda: os.path.exists(b.path("debug_state.json")), 5)
        srv.bridge = b
        srv.fake_comm = fake
        return srv

    yield make
    for fn in reversed(stops):
        fn()


@pytest.fixture
def server(server_factory):
    """Server + bridge over file IPC only (EmuHawk without socket flags)."""
    return server_factory()


@pytest.fixture
def socket_server(server_factory):
    """Server + bridge with EmuHawk started with --socket_ip/--socket_port."""
    srv = server_factory(socket_mode=True)
    assert wait_for(srv.comm_bridge.connected, 5), "bridge never spoke over the socket"
    return srv
