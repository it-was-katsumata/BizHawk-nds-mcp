"""Behaviour of src/bizhawk_debug_api.lua, driven over file IPC."""

import json


def test_boots_and_writes_heartbeat(bridge):
    bridge.tick(20)
    with open(bridge.path("debug_state.json"), encoding="utf-8") as f:
        state = json.load(f)
    assert state["romLoaded"] is True
    assert state["core"] == "NDS"


def test_memory_domains_include_main_ram(bridge):
    # getmemorydomainlist() is zero-indexed in BizHawk; index 0 is "Main RAM".
    domains = bridge.command("memory.getDomains")["data"]
    assert isinstance(domains, list)
    assert domains[0] == "Main RAM"
    assert "ARM9 System Bus" in domains


def test_address_formats(bridge):
    bridge.mock.poke(0x02000123, 0x5A)
    for addr in ("$02000123", "0x02000123", "0X02000123", 0x02000123, str(0x02000123)):
        r = bridge.command("memory.read", address=addr)
        assert r["data"] == 0x5A, addr


def test_invalid_address_is_an_error(bridge):
    r = bridge.command("memory.read", address="banana")
    assert r["success"] is False
    assert "address" in r["error"].lower()


def test_unknown_domain_is_an_error_not_a_silent_fallback(bridge):
    bridge.mock.poke(0x100, 0x77)  # ARM9 bus address 0x100
    r = bridge.command("memory.write", address="0x100", value=1, domain="No Such Domain")
    assert r["success"] is False
    assert bridge.mock.peek(0x100) == 0x77


def test_named_domain_read(bridge):
    bridge.mock.poke(0x10, 0x42, "Main RAM")
    assert bridge.command("memory.read", address="0x10", domain="Main RAM")["data"] == 0x42
    assert bridge.command("memory.readRange", address="0x0200000F", length=3)["data"] == [0, 0x42, 0]


def test_string_params_survive_json(bridge):
    path = 'C:\\dumps\\scene, take 2 {final} "q"\\翻訳.png'
    r = bridge.command("screenshot.take", path=path)
    assert r["success"] is True
    assert r["data"]["path"] == path
    assert bridge.mock.screenshots[1] == path


def test_input_set_with_nested_buttons(bridge):
    r = bridge.command("input.set", buttons={"A": True, "Up": True, "Touch X": 100, "TouchY": 50})
    assert r["success"] is True, r
    # NDS buttons have no "P1 " prefix.
    assert bridge.mock.joypadSet["A"] is True
    assert bridge.mock.joypadSet["Up"] is True
    assert bridge.mock.analog["Touch X"] == 100
    assert bridge.mock.analog["Touch Y"] == 50


def test_touch_release_clears_analog_autoholds(bridge):
    bridge.command("input.touch", x=10, y=20, frames=2)
    assert bridge.mock.analog["Touch X"] == 10
    bridge.command("input.touch_release")
    assert bridge.mock.joypadSet["Touch"] is False
    assert bridge.mock.analog["Touch X"] is None
    assert bridge.mock.analog["Touch Y"] is None


def test_search_scans_main_ram(bridge):
    bridge.mock.poke(0x02000010, 0xAB)
    hits = bridge.command("memory.search", value=0xAB)["data"]
    assert [h["address"] for h in hits] == [0x02000010]
    assert hits[0]["hex"] == "$02000010"


def test_snapshot_compare_main_ram(bridge):
    bridge.mock.poke(0x02000020, 10)
    bridge.mock.poke(0x02000021, 10)
    bridge.command("memory.snapshot", name="s")
    bridge.mock.poke(0x02000020, 7)
    bridge.mock.poke(0x02000021, 12)
    dec = bridge.command("memory.compare", name="s", filter="decreased")["data"]
    assert [(c["address"], c["old"], c["new"]) for c in dec] == [(0x02000020, 10, 7)]
    changed = bridge.command("memory.compare", name="s", filter="changed")["data"]
    assert {c["address"] for c in changed} == {0x02000020, 0x02000021}


def test_compare_missing_snapshot_reports_failure(bridge):
    r = bridge.command("memory.compare", name="nope")
    assert r["success"] is False
    assert "Snapshot not found" in r["error"]


def test_cpu_state_reports_arm_registers(bridge):
    bridge.mock.regs["ARM9 r15"] = 0x02004000
    bridge.mock.regs["ARM9 r0"] = 7
    bridge.mock.regs["ARM7 r15"] = 0x037F8000
    state = bridge.command("cpu.getState")["data"]
    assert state["arm9"]["pc"] == 0x02004000
    assert state["arm9"]["r0"] == 7
    assert state["arm7"]["pc"] == 0x037F8000
    regs = bridge.command("cpu.getRegisters")["data"]
    assert regs["arm9"]["r15"] == "$02004000"


def test_step_while_paused_advances_and_stays_paused(bridge):
    bridge.command("execution.pause")
    start = bridge.mock.frame
    r = bridge.command("execution.step", count=3)
    assert r["success"] is True
    assert bridge.mock.frame - start >= 3
    assert bridge.mock.paused is True


def test_frame_advance_while_paused(bridge):
    bridge.command("execution.pause")
    start = bridge.mock.frame
    bridge.command("frame.advance", frames=2)
    assert bridge.mock.frame - start >= 2
    assert bridge.mock.paused is True


def test_breakpoint_registration_and_hit(bridge):
    r = bridge.command("breakpoint.add", type="write", address="$02000040")
    assert r["success"] is True
    bp_id = r["data"]["id"]
    cb = next(iter(bridge.mock.callbacks.values()))
    assert cb.addr == 0x02000040
    assert cb.scope == "ARM9 System Bus"  # 4th arg is the scope; 3rd is a name
    bridge.mock.access("write", 0x02000040, 9, 0x02001234)
    hit = bridge.command("breakpoint.getLastHit")["data"]
    assert hit["PC"] == "$02001234"
    assert hit["value"] == 9
    assert bridge.command("breakpoint.remove", bpId=bp_id)["data"]["removed"] == bp_id
    assert bridge.mock.callbackCount() == 0


def test_breakpoint_with_bad_address_is_rejected(bridge):
    # A nil address would register a callback on EVERY write.
    r = bridge.command("breakpoint.add", type="write", address="not-an-address")
    assert r["success"] is False
    assert bridge.mock.callbackCount() == 0


def test_freeze_remove_by_freeze_id(bridge):
    fid = bridge.command("freeze.add", address="0x02000050", value=3)["data"]["id"]
    assert bridge.command("freeze.remove", freezeId=fid)["data"]["removed"] == fid


def test_state_load_missing_slot_reports_failure(bridge):
    r = bridge.command("state.load", slot=4)
    assert r["success"] is False
    bridge.command("state.save", slot=4)
    assert bridge.command("state.load", slot=4)["success"] is True


def test_powcnt_display_swap(bridge):
    # GBATEK / libnds lcdMainOnTop(): bit 15 set => engine A on the TOP screen.
    bridge.mock.poke16(0x04000304, 0x820F)
    d = bridge.command("powcnt.read")["data"]
    assert d["engine_a_screen"] == "top"
    assert d["engine_b_screen"] == "bottom"
    assert d["engine_b_enabled"] is True
    bridge.mock.poke16(0x04000304, 0x020F)
    assert bridge.command("powcnt.read")["data"]["engine_a_screen"] == "bottom"


def test_vramcnt_skips_wramcnt(bridge):
    # 0x04000247 is WRAMCNT; VRAMCNT_H/I live at 0x248/0x249.
    bridge.mock.poke(0x04000247, 0x03)
    bridge.mock.poke(0x04000248, 0x81)
    bridge.mock.poke(0x04000249, 0x82)
    banks = bridge.command("vramcnt.decode")["data"]
    assert banks["H"]["value"] == 0x81
    assert banks["I"]["value"] == 0x82


def test_dispcnt_obj_boundary_is_two_bits(bridge):
    bridge.mock.poke32(0x04000000, (1 << 22) | (2 << 20))
    assert bridge.command("dispcnt.decode")["data"]["obj_boundary"] == 2


def test_bg_read_applies_engine_a_dispcnt_bases(bridge):
    # DISPCNT char base 1 (bits 24-26), screen base 2 (bits 27-29); BG0CNT
    # char base 3, screen base 4.
    bridge.mock.poke32(0x04000000, (1 << 24) | (2 << 27))
    bridge.mock.poke16(0x04000008, (3 << 2) | (4 << 8))
    d = bridge.command("bg.read", engine="main", index=0)["data"]
    assert d["map_addr"] == "0x%08X" % (0x06000000 + 2 * 0x10000 + 4 * 0x800)
    assert d["tile_addr"] == "0x%08X" % (0x06000000 + 1 * 0x10000 + 3 * 0x4000)
    # Engine B ignores the DISPCNT bases.
    bridge.mock.poke32(0x04001000, (1 << 24) | (2 << 27))
    bridge.mock.poke16(0x04001008, (3 << 2) | (4 << 8))
    d = bridge.command("bg.read", engine="sub", index=0)["data"]
    assert d["map_addr"] == "0x%08X" % (0x06200000 + 4 * 0x800)


def test_command_error_does_not_kill_bridge(bridge):
    r = bridge.command("memory.readRange", address="0x02000000", length="lots")
    assert r["success"] is False
    assert bridge.command("memory.read", address="0x02000000")["success"] is True


def test_trace_records_executed_addresses_and_stops_at_limit(bridge):
    r = bridge.command("trace.start", limit=3)
    assert r["success"] is True
    for pc in (0x02000000, 0x02000004, 0x02000008, 0x0200000C):
        bridge.mock.access("execute", pc)
    assert bridge.mock.callbackCount() == 0  # unregistered itself at the limit
    entries = bridge.command("trace.get", count=10)["data"]
    assert [e["pc"] for e in entries] == ["$02000000", "$02000004", "$02000008"]


def test_trace_hotspots_counts_instructions_over_frames(bridge):
    def frame(_n):
        for _ in range(3):
            bridge.mock.access("execute", 0x02000100)
        bridge.mock.access("execute", 0x02000200)

    bridge.mock.onFrame = frame
    d = bridge.command("trace.hotspots", frames=2, topN=5)["data"]
    assert d["hotspots"][0] == {"pc": "$02000100", "count": 6}
    assert d["hotspots"][1] == {"pc": "$02000200", "count": 2}
    assert d["instructionsExecuted"] == 8
    assert bridge.mock.callbackCount() == 0


def test_search_caps_results_and_reports_total(bridge):
    for i in range(1500):
        bridge.mock.poke(0x02100000 + i, 0xEE)
    r = bridge.command("memory.search", value=0xEE)
    assert len(r["data"]) == 1000
    assert r["total"] == 1500


def test_compare_filters_old_value_and_delta(bridge):
    for a, v in ((0x02000300, 5), (0x02000301, 5), (0x02000302, 9)):
        bridge.mock.poke(a, v)
    bridge.command("memory.snapshot", name="d")
    bridge.mock.poke(0x02000300, 4)   # -1
    bridge.mock.poke(0x02000301, 2)   # -3
    bridge.mock.poke(0x02000302, 6)   # -3, old 9
    r = bridge.command("memory.compare", name="d", filter="decreased", oldValue=5, minDelta=2)
    assert [c["address"] for c in r["data"]] == [0x02000301]
    r = bridge.command("memory.compare", name="d", filter="changed",
                       addresses=["$02000300", "0x02000302", "$03000000"])
    assert [c["address"] for c in r["data"]] == [0x02000300, 0x02000302]


def test_input_press_and_sequence_still_work(bridge):
    r = bridge.command("input.press", button="A", frames=2)
    assert r["data"] == {"pressed": "A", "frames": 2}
    assert bridge.mock.joypadSet["A"] is False  # released afterwards
    r = bridge.command("input.sequence", seq="press B 2; wait 3; touch 10 20 2")
    assert r["data"]["steps"] == ["press B x2", "wait 3", "touch 10,20 x2"]


def test_full_main_ram_sweep_is_fast(bridge):
    import time
    t = time.perf_counter()
    bridge.command("memory.snapshot", name="big")
    bridge.mock.poke(0x023FFFFF, 1)
    r = bridge.command("memory.compare", name="big")
    assert [c["address"] for c in r["data"]] == [0x023FFFFF]
    # Plain Lua + mock per-byte reads; BizHawk's host read is bulk.
    assert time.perf_counter() - t < 30
