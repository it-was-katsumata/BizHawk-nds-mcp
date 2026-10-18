"""Test harness: runs src/bizhawk_debug_api.lua under Lua 5.4 (via lupa)
against tests/bizhawk_mock.lua, talking to it over the file-IPC path."""

import json
import os
import shutil
import threading
import time

import pytest
from lupa.lua54 import LuaRuntime

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LUA_SCRIPT = os.path.join(ROOT, "src", "bizhawk_debug_api.lua")
MOCK = os.path.join(ROOT, "tests", "bizhawk_mock.lua")


class Bridge:
    """The Lua bridge running inside a simulated EmuHawk host loop."""

    def __init__(self, workdir):
        self.dir = str(workdir)
        self.script = os.path.join(self.dir, "bizhawk_debug_api.lua")
        shutil.copy(LUA_SCRIPT, self.script)
        self.lua = LuaRuntime(unpack_returned_tuples=True)
        self.mock = self.lua.eval(f"dofile({json.dumps(MOCK)})")
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


@pytest.fixture
def server(tmp_path, monkeypatch):
    """The Python MCP server module, wired to a bridge ticking in a thread."""
    import importlib
    import sys

    sys.path.insert(0, os.path.join(ROOT, "src"))
    srv = importlib.import_module("bizhawk_mcp_server")
    b = Bridge(tmp_path)
    monkeypatch.setattr(srv, "COMMAND_FILE", b.path("debug_commands.json"))
    monkeypatch.setattr(srv, "RESPONSE_FILE", b.path("debug_response.json"))
    monkeypatch.setattr(srv, "STATE_FILE", b.path("debug_state.json"))
    monkeypatch.setattr(srv, "SOCKET_PORT", 1)  # nothing listens: file path only
    monkeypatch.setattr(srv, "DEFAULT_TIMEOUT", 5.0)
    monkeypatch.setattr(srv, "FAST_TIMEOUT", 5.0)

    stop = threading.Event()

    def run():
        while not stop.is_set():
            b.tick()
            time.sleep(0.0005)

    t = threading.Thread(target=run, daemon=True)
    t.start()
    deadline = time.time() + 5
    while not os.path.exists(b.path("debug_state.json")) and time.time() < deadline:
        time.sleep(0.01)
    srv.bridge = b
    yield srv
    stop.set()
    t.join(timeout=2)
