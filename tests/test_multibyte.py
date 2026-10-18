"""16/32-bit values: search, compare, read and write (little-endian)."""

import struct


def poke_bytes(bridge, addr, data):
    for i, b in enumerate(data):
        bridge.mock.poke(addr + i, b)


def test_search_u16_aligned_by_default(bridge):
    poke_bytes(bridge, 0x02000100, struct.pack("<H", 0x1234))  # aligned
    poke_bytes(bridge, 0x02000201, struct.pack("<H", 0x1234))  # odd address
    r = bridge.command("memory.search", value=0x1234, size=2)
    assert [h["address"] for h in r["data"]] == [0x02000100]
    assert r["data"][0]["value"] == 0x1234
    r = bridge.command("memory.search", value=0x1234, size=2, aligned=False)
    assert [h["address"] for h in r["data"]] == [0x02000100, 0x02000201]


def test_search_u32_and_negative_values(bridge):
    poke_bytes(bridge, 0x02000300, struct.pack("<I", 100000))
    poke_bytes(bridge, 0x02000310, struct.pack("<i", -2))
    assert [h["address"] for h in bridge.command("memory.search", value=100000, size=4)["data"]] == [0x02000300]
    hits = bridge.command("memory.search", value=-2, size=4)["data"]
    assert [h["address"] for h in hits] == [0x02000310]
    assert hits[0]["value"] == 0xFFFFFFFE


def test_search_rejects_out_of_range_and_bad_size(bridge):
    assert bridge.command("memory.search", value=70000, size=2)["success"] is False
    assert bridge.command("memory.search", value=1, size=3)["success"] is False
    assert bridge.command("memory.search", value=256)["success"] is False


def test_read_write_multibyte_round_trip(bridge):
    assert bridge.command("memory.write", address="0x02000400", value=0xCAFEBABE, size=4)["success"]
    assert bridge.command("memory.read", address="0x02000400", size=4)["data"] == 0xCAFEBABE
    assert bridge.command("memory.read", address="0x02000400", size=2)["data"] == 0xBABE
    assert bridge.mock.peek(0x02000400) == 0xBE  # little-endian
    bridge.command("memory.write", address="0x02000410", value=-1, size=2)
    assert bridge.command("memory.read", address="0x02000410", size=2)["data"] == 0xFFFF
    assert bridge.mock.peek(0x02000412) == 0  # wrote exactly 2 bytes


def test_compare_u16_increased_and_decreased(bridge):
    poke_bytes(bridge, 0x02000500, struct.pack("<H", 1000))  # HP 1000 -> 990
    poke_bytes(bridge, 0x02000502, struct.pack("<H", 255))   # 255 -> 256: low byte drops, value rises
    bridge.command("memory.snapshot", name="hp")
    poke_bytes(bridge, 0x02000500, struct.pack("<H", 990))
    poke_bytes(bridge, 0x02000502, struct.pack("<H", 256))
    dec = bridge.command("memory.compare", name="hp", filter="decreased", size=2)["data"]
    assert [(c["address"], c["old"], c["new"]) for c in dec] == [(0x02000500, 1000, 990)]
    inc = bridge.command("memory.compare", name="hp", filter="increased", size=2)["data"]
    assert [(c["address"], c["old"], c["new"]) for c in inc] == [(0x02000502, 255, 256)]
    # The same snapshot byte-wise sees the 255 -> 0 low byte as a decrease.
    dec8 = bridge.command("memory.compare", name="hp", filter="decreased")["data"]
    assert 0x02000502 in [c["address"] for c in dec8]


def test_compare_signed(bridge):
    poke_bytes(bridge, 0x02000600, struct.pack("<h", 1))
    bridge.command("memory.snapshot", name="s")
    poke_bytes(bridge, 0x02000600, struct.pack("<h", -1))
    unsigned = bridge.command("memory.compare", name="s", filter="increased", size=2)["data"]
    assert [c["address"] for c in unsigned] == [0x02000600]  # 1 -> 0xFFFF
    signed = bridge.command("memory.compare", name="s", filter="decreased", size=2, signed=True)["data"]
    assert [(c["old"], c["new"]) for c in signed] == [(1, -1)]


def test_compare_u32_straddling_a_page_boundary(bridge):
    # A value whose bytes sit in two 4 KB pages, with only the second page
    # changing, must still be found (unaligned compare).
    addr = 0x02000000 + 4096 - 2
    poke_bytes(bridge, addr, struct.pack("<I", 0x11112222))
    bridge.command("memory.snapshot", name="p")
    poke_bytes(bridge, addr, struct.pack("<I", 0x33332222))  # only the high half changes
    r = bridge.command("memory.compare", name="p", filter="changed", size=4, aligned=False)
    assert addr in [c["address"] for c in r["data"]]


def test_compare_addresses_with_size(bridge):
    poke_bytes(bridge, 0x02000700, struct.pack("<I", 5))
    poke_bytes(bridge, 0x02000704, struct.pack("<I", 5))
    bridge.command("memory.snapshot", name="a")
    poke_bytes(bridge, 0x02000704, struct.pack("<I", 9))
    r = bridge.command("memory.compare", name="a", filter="changed", size=4,
                       addresses=["$02000700", "$02000704"])
    assert [(c["address"], c["old"], c["new"]) for c in r["data"]] == [(0x02000704, 5, 9)]


def test_finder_tools_take_size(server):
    from test_mcp_server import call

    addr = 0x02001200
    timer = [5000]

    def tick(_n):  # a 16-bit timer counting down once per frame
        timer[0] -= 1
        poke_bytes(server.bridge, addr, struct.pack("<H", timer[0]))

    with server.bridge.lock:
        server.bridge.mock.onFrame = tick
    r = call(server, "emu_cheat_find_decrementing_value", frames=3, size=2)
    assert [c["address"] for c in r["candidates"]] == [addr]
    assert r["candidates"][0]["old"] - r["candidates"][0]["new"] >= 3
