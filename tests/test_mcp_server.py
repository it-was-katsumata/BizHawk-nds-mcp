"""End-to-end: MCP tool handlers in src/bizhawk_mcp_server.py -> file IPC ->
the Lua bridge running against the BizHawk mock."""

import asyncio
import json


def call(srv, name, **arguments):
    result = asyncio.run(srv.call_tool(name, arguments))
    content = getattr(result, "content", result)
    return json.loads(content[0].text)


def test_server_builds_on_installed_mcp(server):
    assert server.server is not None
    tools = asyncio.run(server.list_tools())
    names = {t.name for t in tools}
    assert {"emu_debug_read_memory", "emu_debug_raw_command", "emu_debug_trace_and_summarize"} <= names


def test_read_memory_round_trip(server):
    with server.bridge.lock:
        server.bridge.mock.poke(0x02000010, 0x99)
    r = call(server, "emu_debug_read_memory", address="$02000010")
    assert r["success"] is True and r["data"] == 0x99


def test_remove_breakpoint_over_file_ipc(server):
    # The breakpoint id used to overwrite the command envelope's id.
    bp = call(server, "emu_debug_add_breakpoint", type="write", address="0x02000040")
    bp_id = bp["data"]["id"]
    r = call(server, "emu_debug_remove_breakpoint", id=bp_id)
    assert r.get("success") is True, r
    assert r["data"]["removed"] == bp_id


def test_unfreeze_all_over_file_ipc(server, monkeypatch):
    monkeypatch.setattr(server, "FREEZE_FEATURE_ENABLED", True)
    f = call(server, "emu_cheat_freeze_address", address="0x02000050", value=1)
    assert f["success"] is True
    r = call(server, "emu_cheat_unfreeze_address", all=True)
    assert r.get("success") is True, r
    r = call(server, "emu_cheat_unfreeze_address", freezeId=12345)
    assert r["success"] is False


def test_error_responses_are_flagged(server):
    result = asyncio.run(server.call_tool("emu_debug_compare_memory", {"name": "missing"}))
    # mcp 2.x names the field is_error (isError is its alias); 1.x uses isError
    assert getattr(result, "is_error", getattr(result, "isError", None)) is True


def test_cpu_state_reports_arm9_pc(server):
    with server.bridge.lock:
        server.bridge.mock.regs["ARM9 r15"] = 0x02001000
    r = call(server, "emu_debug_get_cpu_state")
    assert r["data"]["arm9"]["pc"] == 0x02001000


def test_step_while_paused(server):
    call(server, "emu_debug_pause")
    r = call(server, "emu_debug_frame_advance", count=2)
    assert r["success"] is True, r
    assert server.bridge.mock.paused is True


def test_find_decrementing_value(server):
    addr = 0x02001234
    with server.bridge.lock:
        server.bridge.mock.poke(addr, 200)
        server.bridge.mock.onFrame = lambda n: server.bridge.mock.poke(addr, max(0, 200 - n))
    r = call(server, "emu_cheat_find_decrementing_value", frames=5, minDecrement=2)
    assert addr in [c["address"] for c in r["candidates"]], r


def test_narrow_candidates(server):
    a, b = 0x02000400, 0x02000401
    call(server, "emu_cheat_narrow_candidates", addresses=["$02000400", "$02000401"], action="snapshot")
    with server.bridge.lock:
        server.bridge.mock.poke(a, 5)
    r = call(server, "emu_cheat_narrow_candidates", addresses=["$02000400", "0x02000401"],
             action="filter", filter="changed")
    assert r["narrowed"] == ["$02000400"], r
    assert b not in [d["address"] for d in r["details"]]


def test_capture_write_source(server):
    def frame(_n):
        server.bridge.mock.access("write", 0x02000500, 1, 0x02003000)
    with server.bridge.lock:
        server.bridge.mock.onFrame = frame
    r = call(server, "emu_debug_capture_write_source", address="0x02000500", frames=3)
    assert r["hits"][0]["pc"] == "$02003000"
    assert r["totalHits"] >= 3


def test_trace_and_summarize(server):
    def frame(_n):
        server.bridge.mock.access("execute", 0x02000600)
    with server.bridge.lock:
        server.bridge.mock.onFrame = frame
    r = call(server, "emu_debug_trace_and_summarize", frames=2)
    assert r["data"]["hotspots"][0]["pc"] == "$02000600", r


def test_set_input_presses_nds_button(server):
    r = call(server, "emu_debug_set_input", buttons={"A": True})
    assert r["success"] is True, r
    assert server.bridge.mock.joypadSet["A"] is True


def test_tool_calls_do_not_block_the_event_loop(server):
    async def scenario():
        ticks = 0
        stop = asyncio.Event()

        async def heartbeat():
            nonlocal ticks
            while not stop.is_set():
                ticks += 1
                await asyncio.sleep(0.005)

        hb = asyncio.create_task(heartbeat())
        await server.call_tool("emu_debug_raw_command",
                               {"action": "input.sequence", "params": {"seq": "wait 300"}})
        stop.set()
        await hb
        return ticks

    assert asyncio.run(scenario()) > 5


def test_narrow_candidates_rejects_huge_span(server):
    r = call(server, "emu_cheat_narrow_candidates", addresses=["$02000000", "$06000000"], action="snapshot")
    assert "4 MB" in r["error"]
