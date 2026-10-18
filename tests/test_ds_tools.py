"""Typed emu_ds_* tools and image screenshots, end to end."""

import asyncio
import base64
import io
import json
import os

import pytest
from PIL import Image

from test_mcp_server import call

RED, BLUE = (255, 0, 0), (0, 0, 255)


def png_bytes(width, height, top=RED, bottom=BLUE):
    img = Image.new("RGB", (width, height), top)
    img.paste(bottom, (0, height // 2, width, height))
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


def call_raw(srv, name, **arguments):
    return asyncio.run(srv.call_tool(name, arguments))


def image_of(result):
    block = result.content[0]
    assert block.type == "image"
    mime = getattr(block, "mimeType", None) or getattr(block, "mime_type", None)
    assert mime == "image/png"
    return Image.open(io.BytesIO(base64.b64decode(block.data)))


def test_screenshot_returns_an_image(server):
    server.bridge.mock.screenshotPng = png_bytes(256, 384)
    result = call_raw(server, "emu_ds_screenshot")
    img = image_of(result)
    assert img.size == (512, 768)  # default scale 2
    meta = json.loads(result.content[-1].text)
    assert meta["success"] is True and meta["width"] == 512
    assert not os.path.exists(server.SCREENSHOT_FILE)  # temp file cleaned up


def test_screenshot_crops_one_screen(server):
    server.bridge.mock.screenshotPng = png_bytes(256, 384)
    img = image_of(call_raw(server, "emu_ds_screenshot", screen="bottom", scale=1))
    assert img.size == (256, 192)
    assert img.convert("RGB").getpixel((10, 10)) == BLUE
    img = image_of(call_raw(server, "emu_ds_screenshot", screen="top", scale=3))
    assert img.size == (768, 576)
    assert img.convert("RGB").getpixel((700, 500)) == RED


def test_screenshot_unscaled_is_passed_through(server):
    raw = png_bytes(256, 384)
    server.bridge.mock.screenshotPng = raw
    block = call_raw(server, "emu_ds_screenshot", scale=1).content[0]
    assert base64.b64decode(block.data) == raw


def test_screenshot_save_path_keeps_the_frame(server, tmp_path):
    server.bridge.mock.screenshotPng = png_bytes(256, 384)
    target = str(tmp_path / "keep.png")
    meta = json.loads(call_raw(server, "emu_ds_screenshot", savePath=target).content[-1].text)
    assert meta["savedTo"] == target
    assert Image.open(target).size == (256, 384)  # the uncropped original


def test_screenshot_crop_needs_vertical_layout(server):
    server.bridge.mock.screenshotPng = png_bytes(512, 192)  # horizontal layout
    r = call(server, "emu_ds_screenshot", screen="top")
    assert "vertical" in r["error"]


def test_press_button(server):
    r = call(server, "emu_ds_press_button", button="Start", frames=2)
    assert r["data"] == {"pressed": "Start", "frames": 2}
    assert server.bridge.mock.joypadSet["Start"] is False  # released afterwards


def test_touch_and_release(server):
    r = call(server, "emu_ds_touch", x=40, y=60, frames=2)
    assert r["data"]["touched"] is True
    assert server.bridge.mock.analog["Touch X"] == 40
    call(server, "emu_ds_touch_release")
    assert server.bridge.mock.analog["Touch X"] is None


def test_input_sequence_from_typed_steps(server):
    r = call(server, "emu_ds_input_sequence", steps=[
        {"action": "press", "button": "A", "frames": 2},
        {"action": "wait", "frames": 3},
        {"action": "touch", "x": 128, "y": 96},
        {"action": "sleep", "seconds": 0.05},
    ])
    assert r["data"]["steps"] == ["press A x2", "wait 3", "touch 128,96 x8", "sleep 0.05s"]


@pytest.mark.parametrize("step", [
    {"action": "press", "button": "Q"},
    {"action": "touch", "x": 300, "y": 10},
    {"action": "sleep"},
    {"action": "jump"},
])
def test_input_sequence_rejects_bad_steps(server, step):
    r = call(server, "emu_ds_input_sequence", steps=[step])
    assert "step 0" in r["error"]


def test_graphics_decoders(server):
    m = server.bridge.mock
    with server.bridge.lock:
        m.poke16(0x04000304, 0x820F)
        m.poke(0x04000248, 0x81)
        # OAM entry 0: y=10, x=20, tile 5, 16x16 (shape 0, size 1), palette 2
        m.poke16(0x07000000, 10)
        m.poke16(0x07000002, 20 | (1 << 14))
        m.poke16(0x07000004, 5 | (2 << 12))
    assert call(server, "emu_ds_read_powcnt")["data"]["engine_a_screen"] == "top"
    assert call(server, "emu_ds_decode_vramcnt")["data"]["H"]["value"] == 0x81
    assert call(server, "emu_ds_decode_dispcnt", engine="sub")["data"]["engine"] == "sub"
    sprite = call(server, "emu_ds_parse_oam")["data"]["sprites"][0]
    assert (sprite["x"], sprite["y"], sprite["tile"], sprite["w_tiles"], sprite["palette"]) == (20, 10, 5, 2, 2)
    bg = call(server, "emu_ds_read_bg", index=1)["data"]
    assert bg["index"] == 1 and len(bg["tilemap"]) == 24


def test_dump_memory(server, tmp_path):
    with server.bridge.lock:
        for i, b in enumerate(b"TEXT"):
            server.bridge.mock.poke(0x02000800 + i, b)
    out = tmp_path / "dump.bin"
    r = call(server, "emu_ds_dump_memory", address="0x02000800", length=4, path=str(out))
    assert r["success"] is True
    assert out.read_bytes() == b"TEXT"


def test_rom_tools_and_reset(server):
    assert call(server, "emu_ds_close_rom")["data"]["closed"] is True
    assert server.bridge.mock.system == "NULL"
    assert call(server, "emu_ds_open_rom", path="C:/roms/game.nds")["data"]["opened"] is True
    assert server.bridge.mock.openedRom == "C:/roms/game.nds"
    assert call(server, "emu_ds_reset")["data"]["reset"] is True


def test_input_debug(server):
    assert "digital_nil" in call(server, "emu_ds_input_debug")["data"]
