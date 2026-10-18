# 🎮 BizHawk-nds-mcp — Nintendo DS debug bridge

**Python MCP server + Lua bridge for AI-driven Nintendo DS reverse engineering in BizHawk (MelonDS core).**

This fork adapts an [upstream NES debug bridge](https://github.com/q8fft2/BizHawk-mcp)
(MIT licensed) for **Nintendo DS**: it rewires the Lua side for MelonDS memory
domains and ARM9/ARM7 CPU state, and adds touchscreen input, screenshot capture,
batched input sequences, and helper actions for parsing OAM, DISPCNT, POWCNT,
VRAMCNT, and BG tilemaps.

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)

> ⚠️ **Scope**: tools act on the running emulator. A few also touch host files:
> `emu_debug_save_state` writes savestates, `emu_ds_dump_memory` and
> `emu_ds_screenshot`'s `savePath` write files, and `emu_ds_open_rom` loads a ROM
> from disk. Every tool carries MCP `readOnlyHint` / `destructiveHint`
> annotations so clients can tell them apart.

---

## ✨ Features

| Feature | Description |
|---------|-------------|
| 🧠 **Memory Access** | Read/write/search/snapshot/compare 8, 16 or 32-bit values by physical DS address via `ARM9 System Bus` (default), or by offset in MelonDS domains (`Main RAM`, `Shared WRAM`, `Instruction TCM`, `Data TCM`, …). Search/snapshot sweep all 4 MB of main RAM |
| 🔍 **CPU State** | ARM9 / ARM7 registers r0–r15 (BizHawk exposes no CPSR) |
| ⏸️ **Execution Control** | Pause, resume, and frame-advance (works while paused) |
| 🎯 **Breakpoints** | Read / write / execute memory callbacks with hit log and optional auto-pause (MelonDS JIT must be off) |
| 📝 **Trace** | Record executed ARM9 instruction addresses or count hotspots via exec callbacks (CPU-intensive) |
| 💾 **Save States** | Save / load numbered slots or explicit file paths |
| 🕹️ **DS Input** | Buttons, touchscreen taps, and batched navigation sequences as typed tools |
| 📸 **Screen Capture** | `emu_ds_screenshot` returns the screen to the agent as an image (both screens or one, upscaled so pixel fonts stay legible) |
| 🧩 **DS Graphics Helpers** | Decode OAM, DISPCNT, POWCNT, VRAMCNT, and BG tilemaps |
| 🔄 **Patch + Reload** | `emu_ds_close_rom` / `emu_ds_open_rom` for a build-and-reload loop |
| ⚡ **Socket fast path** | Optional: start EmuHawk with `--socket_ip/--socket_port` and commands use BizHawk's built-in socket client (~1 frame per call) instead of 50 ms file polling |

---

## 🚀 Quick Start

### Prerequisites
- [BizHawk](https://github.com/TASEmulators/BizHawk/releases) **2.10+** with the **MelonDS** core. **2.11.1 recommended**: it fixes 16/32-bit reads on the MelonDS system bus and passes the real value to memory callbacks (2.10 always reports 0)
- **Python 3.10+** (the `mcp` SDK 1.30+ or 2.x; both are supported)
- An **NDS ROM** (plus its matching `.dsv` save, if you want to resume in-game)

### Installation

1. **Clone the repository:**
   ```bash
   git clone https://github.com/it-was-katsumata/BizHawk-nds-mcp.git
   cd BizHawk-nds-mcp
   ```

2. **Install dependencies** (the `mcp` Python SDK, and Pillow for screenshot cropping/scaling):
   ```bash
   pip install -r requirements.txt
   ```

3. **Start BizHawk and load the Lua bridge:**
   - Open BizHawk and load your **NDS** ROM (the status bar should read `Nintendo DS`)
   - **Tools → Lua Console → Open Script** → `src/bizhawk_debug_api.lua`
   - The console prints `BizHawk Debug API Ready!` and `System: NDS`

4. **Run the Python server:**
   ```bash
   python src/bizhawk_mcp_server.py
   ```

> For the full step-by-step (firmware prompt, save states, registering the
> server with Claude Code, and the touchscreen workaround), see
> **[NDS_SETUP.md](NDS_SETUP.md)**.

---

## 📖 How It Works

The MCP server is the bridge between an AI agent and BizHawk:

```
AI agent  ──MCP/stdio──►  bizhawk_mcp_server.py  ◄──TCP 127.0.0.1:9876 (optional)──  bizhawk_debug_api.lua  ──►  BizHawk (MelonDS)
                                                  └──────── JSON files (default) ────────┘
```

The server exposes each capability as an MCP tool (e.g. `emu_debug_read_memory`,
`emu_ds_screenshot`). By default commands travel through a **JSON file**
exchange (`src/debug_commands.json` → `src/debug_response.json`), polled every
50 ms.

**Socket fast path:** BizHawk's Lua can only make *outgoing* connections, so the
MCP server listens on `127.0.0.1:9876` (set `BIZHAWK_MCP_PORT` to change it, or
`off`) and the bridge connects using BizHawk's built-in `comm.socketServer*`
client, which exists only when EmuHawk is started with the socket flags:

```
EmuHawk.exe --socket_ip=127.0.0.1 --socket_port=9876 "C:\path\to\game.nds"
```

EmuHawk connects during startup and shows a fatal error if nothing is listening,
so **start your MCP client (which starts the server) first**, and start EmuHawk
without the flags when the server isn't running. While connected, a command takes
about one frame. If the server restarts, the bridge reconnects on its own (it
finds the new server through `src/debug_server.json`) and uses the files in the
meantime. A command that may already be running is never re-sent.

A heartbeat file (`src/debug_state.json`) lets the server detect a stale or
closed bridge. All of these runtime files are git-ignored.

---

## 📖 Usage Example

A complete "boot the game, reach a scene, inspect it" loop — the everyday
workflow this fork was built for.

**1. Confirm the bridge is alive.**
```
emu_debug_get_emulator_state()
# → { core: "NDS", romLoaded: true, frameCount: 879472, isPaused: false, ... }
```

**2. Drive the game from boot to an in-game scene in one batched command.**
`emu_ds_input_sequence` runs many steps in a single round trip. `sleep` waits
while frames keep advancing (60 frames per second, so real time at normal
speed), for screen transitions:
```
emu_ds_input_sequence(steps=[
  {action: "press", button: "A"}, {action: "sleep", seconds: 6},
  {action: "press", button: "A"}, {action: "sleep", seconds: 2.5},
  {action: "press", button: "A"}, {action: "sleep", seconds: 8},
  {action: "press", button: "Right"}, {action: "sleep", seconds: 0.4},
  {action: "press", button: "Down"}, {action: "sleep", seconds: 0.4},
  {action: "press", button: "A"}, {action: "sleep", seconds: 1}
])
# boots past the splash + intro, picks Continue, then navigates the menu.
```

**3. Look at the screen** (comes back as an image the agent can read, e.g. to
check translated text):
```
emu_ds_screenshot(screen="bottom", scale=3)
```

**4. Inspect the graphics hardware for the current scene.**
```
emu_ds_read_powcnt()
# → { powcnt1: 0x820F, lcd_swap: true, engine_a_screen: "top", ... }   (which engine drives which screen)

emu_ds_parse_oam(engine="sub")
# → { sprites: [ { idx: 0, x: 4, y: 8, tile: 0, w_tiles: 4, h_tiles: 4, palette: 5 }, ... ] }
```

**5. Read memory by physical DS address (System Bus), e.g. main RAM.**
```
emu_debug_read_memory_range(address="0x02000000", length=64)
emu_debug_search_memory(value=1500, size=2)   # a 16-bit value, e.g. money
```

**6. Touch the bottom screen** (see the touchscreen note in
[NDS_SETUP.md](NDS_SETUP.md) — a BizHawk XOR quirk requires rebinding the
`Touch` row and parking the mouse cursor):
```
emu_ds_touch(x=128, y=96, frames=8)
emu_ds_touch_release()
```

**7. Rebuild the ROM and reload it without restarting BizHawk.**
```
emu_ds_close_rom()                        # release the file lock
# ... run your patcher, overwriting the .nds on disk ...
emu_ds_open_rom(path="C:/path/to/patched.nds")
```

---

## 🔧 Available Commands

Full reference in **[API_README.md](API_README.md)**.

**Nintendo DS tools:**

| Category | Tools |
|----------|-------|
| Screen | `emu_ds_screenshot` |
| Input | `emu_ds_press_button`, `emu_ds_touch`, `emu_ds_touch_release`, `emu_ds_input_sequence`, `emu_ds_input_debug` |
| ROM / system | `emu_ds_reset`, `emu_ds_close_rom`, `emu_ds_open_rom`, `emu_ds_dump_memory` |
| Graphics | `emu_ds_read_powcnt`, `emu_ds_decode_dispcnt`, `emu_ds_decode_vramcnt`, `emu_ds_read_bg`, `emu_ds_parse_oam` |

**Core debugging tools:**

| Category | Tools |
|----------|-------|
| Memory | `emu_debug_read_memory`, `emu_debug_write_memory`, `emu_debug_read_memory_range`, `emu_debug_search_memory`, `emu_debug_snapshot_memory`, `emu_debug_compare_memory`, `emu_debug_list_memory_domains` |
| CPU | `emu_debug_get_cpu_state`, `emu_debug_get_cpu_registers` |
| Execution | `emu_debug_pause`, `emu_debug_resume`, `emu_debug_step`, `emu_debug_frame_advance` |
| Breakpoints | `emu_debug_add_breakpoint`, `emu_debug_list_breakpoints`, `emu_debug_remove_breakpoint`, `emu_debug_clear_breakpoints`, `emu_debug_get_breakpoint_hits` |
| Trace | `emu_debug_start_trace`, `emu_debug_stop_trace`, `emu_debug_get_trace`, `emu_debug_trace_and_summarize` |
| Watch | `emu_debug_add_watch`, `emu_debug_list_watches`, `emu_debug_remove_watch`, `emu_debug_clear_watches` |
| State / Input | `emu_debug_get_emulator_state`, `emu_debug_save_state`, `emu_debug_load_state`, `emu_debug_set_input`, `emu_debug_raw_command` |

The DS tools wrap these bridge actions, which `emu_debug_raw_command` can also
send directly (reference in **[NDS_SETUP.md](NDS_SETUP.md)**):

| Group | Actions |
|-------|---------|
| Input | `input.set`, `input.touch`, `input.touch_release`, `input.press`, `input.sequence`, `input.debug` |
| Flow | `frame.advance`, `system.reset`, `system.close_rom`, `system.open_rom` |
| Capture | `screenshot.take`, `memory.dump_to_file` |
| Trace | `trace.hotspots` |
| Graphics | `powcnt.read`, `dispcnt.decode`, `vramcnt.decode`, `bg.read`, `oam.parse` |

---

## 📁 Project Structure

```
├── src/
│   ├── bizhawk_debug_api.lua    # BizHawk Lua bridge (load in Lua Console)
│   └── bizhawk_mcp_server.py    # Python MCP server (socket + file IPC)
├── lua/
│   └── socket.lua               # LuaSocket shim (upstream; optional, only for wall-clock `sleep`)
├── tests/                       # Lua bridge + server tests against a BizHawk mock
├── requirements.txt             # Python dependencies (mcp SDK, Pillow)
├── requirements-dev.txt         # + lupa (Lua 5.4) and pytest for the tests
├── README.md                    # This file
├── API_README.md                # Core MCP tool reference
└── NDS_SETUP.md                 # Full NDS setup + DS action reference + troubleshooting
```

---

## 🧪 Tests

The test suite runs the real `src/bizhawk_debug_api.lua` under Lua 5.4 (the
version BizHawk embeds) against `tests/bizhawk_mock.lua`, a stand-in for the
BizHawk API modelled on BizHawk's source (zero-indexed domain list, MelonDS
register names, frame-advance blocking while paused, …), and drives the MCP
server end to end over file IPC and stdio. No emulator or ROM needed:

```bash
pip install -r requirements-dev.txt
python -m pytest tests
```

It cannot catch everything a real core does (timing, touch/XOR quirks), so
still smoke-test in EmuHawk after changing the bridge.

---

## 📄 License

MIT License — see [LICENSE](LICENSE). This is an NDS-focused fork of an upstream
NES debug bridge; the LICENSE credits both the original author and this fork's
changes.

---

For DS game reverse engineers and AI agents.
