"""The comm.socketServer* fast path: Python listens, the Lua bridge (via a
port of BizHawk's SocketServer client) connects and polls READY/answer."""

import json
import socket
import threading
import time

from conftest import wait_for
from test_mcp_server import call


def command_file(srv):
    with open(srv.COMMAND_FILE, encoding="utf-8") as f:
        return json.load(f)


def test_commands_go_over_the_socket(socket_server):
    srv = socket_server
    with srv.bridge.lock:
        srv.bridge.mock.poke(0x02000010, 0x77)
    r = call(srv, "emu_debug_read_memory", address="$02000010")
    assert r["success"] is True and r["data"] == 0x77
    r = call(srv, "emu_debug_raw_command", action="input.sequence", params={"seq": "wait 30"})
    assert r["data"]["steps"] == ["wait 30"]
    # Only the bridge's startup placeholder: nothing went through the file.
    assert command_file(srv) == {"id": 0}
    assert srv.fake_comm.hang_hazard is False


def test_socket_is_faster_than_file_polling(socket_server):
    srv = socket_server
    t = time.perf_counter()
    for _ in range(20):
        assert call(srv, "emu_debug_read_memory", address="0x02000000")["success"] is True
    # File IPC polls every 50 ms, so 20 calls would take >= 1 s.
    assert time.perf_counter() - t < 1.0


def test_bridge_reconnects_to_a_restarted_server(socket_server, monkeypatch):
    srv = socket_server
    srv.comm_bridge.stop()  # e.g. the Claude session ended; aborts with RST
    new = srv.CommBridge()
    assert new.start(port=0)
    monkeypatch.setattr(srv, "comm_bridge", new)
    try:
        # Meanwhile commands still work over the files.
        assert call(srv, "emu_debug_read_memory", address="0x02000000")["success"] is True
        # The bridge reads the new port from debug_server.json and redials.
        assert wait_for(new.connected, 10), "bridge did not reconnect"
        assert srv.fake_comm.port == new.port
        before = command_file(srv)
        assert call(srv, "emu_debug_read_memory", address="0x02000000")["success"] is True
        assert command_file(srv) == before  # this one went over the socket
        assert srv.fake_comm.hang_hazard is False
    finally:
        new.stop()


class RawBridgeClient:
    """Speaks the bridge side of the protocol by hand."""

    def __init__(self, port):
        self.sock = socket.create_connection(("127.0.0.1", port))
        self.sock.settimeout(5)
        self.rfile = self.sock.makefile("rb")

    def send(self, msg):
        data = msg.encode()
        self.sock.sendall(str(len(data)).encode() + b" " + data)

    def recv(self):
        digits = b""
        while (b := self.rfile.read(1)) != b" ":
            assert b, "server closed the connection"
            digits += b
        return self.rfile.read(int(digits)).decode()

    def close(self):
        self.rfile.close()  # makefile holds a reference; close both
        self.sock.close()


def started_bridge(srv):
    bridge = srv.CommBridge()
    assert bridge.start(port=0)
    return bridge


def test_handed_over_command_is_not_resent_when_the_connection_dies(server):
    srv = server
    bridge = started_bridge(srv)
    try:
        client = RawBridgeClient(bridge.port)
        client.send("READY")
        assert client.recv() == "NONE"
        result = {}
        t = threading.Thread(target=lambda: result.update(
            r=bridge.send({"id": 7, "action": "input.sequence"}, timeout=5)))
        t.start()
        client.send("READY")
        assert json.loads(client.recv())["id"] == 7  # handed over...
        client.close()                               # ...then BizHawk goes away
        t.join(timeout=5)
        assert "not re-sending" in result["r"]["error"]
    finally:
        bridge.stop()


def test_command_never_picked_up_falls_back_to_file(server, monkeypatch):
    srv = server
    monkeypatch.setattr(srv, "HANDOVER_TIMEOUT", 0.3)
    bridge = started_bridge(srv)
    try:
        client = RawBridgeClient(bridge.port)
        client.send("READY")
        assert client.recv() == "NONE"
        # The bridge stops polling (e.g. busy with a file command): send()
        # gives up before handing over, so the caller may use the file.
        assert bridge.send({"id": 8, "action": "memory.read"}, timeout=5) is None
        client.close()
    finally:
        bridge.stop()


def test_server_aborts_connections_with_rst(server):
    # BizHawk's ReceiveString spins forever on a graceful close (FIN) during
    # a length prefix, so the server must always reset (RST) instead.
    srv = server
    bridge = started_bridge(srv)
    client = RawBridgeClient(bridge.port)
    client.send("READY")
    assert client.recv() == "NONE"
    bridge.stop()
    try:
        data = client.sock.recv(1)
    except ConnectionResetError:
        data = None
    assert data is None, "server closed gracefully (FIN); EmuHawk would hang"


def test_announcement_file_tracks_the_listener(server):
    srv = server
    bridge = started_bridge(srv)
    try:
        assert wait_for(lambda: _read(srv.SERVER_INFO_FILE).get("port") == bridge.port, 5)
    finally:
        bridge.stop()
    assert _read(srv.SERVER_INFO_FILE) == {}  # removed on stop


def _read(path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}
