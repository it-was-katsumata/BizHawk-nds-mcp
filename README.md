# 🎮 BizHawk Debug Server — Nintendo DS fork

**Python MCP server + Lua bridge for AI-driven Nintendo DS reverse engineering in BizHawk (MelonDS core).**

This fork adapts an [upstream NES debug bridge](https://github.com/q8fft2/BizHawk-mcp)
(MIT licensed) for **Nintendo DS**: it rewires the Lua side for MelonDS memory
domains and ARM9/ARM7 CPU state, and adds touchscreen input, screenshot capture,
batched input sequences, and helper actions for parsing OAM, DISPCNT, POWCNT,
VRAMCNT, and BG tilemaps.

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)

> ⚠️ **IMPORTANT**: All tools operate **ONLY on volatile emulator runtime state**. They do **NOT** modify files, server code, configuration, or the host system.

---

## ✨ Features

| Feature | Description |
|---------|-------------|
| 🧠 **Memory Access** | Read/write/search/snapshot/compare across MelonDS domains (Main RAM, Shared WRAM, VRAM, Main/Sub Palette, OAM, …) or by physical address via System Bus |
| 🔍 **CPU State** | Read the ARM9 / ARM7 registers exposed by MelonDS |
| ⏸️ **Execution Control** | Pause, resume, step, and frame-advance |
| 🎯 **Breakpoints** | Read / write / execute breakpoints with auto-pause |
| 📝 **Trace Logging** | Record instruction execution and find hotspots |
| 💾 **Save States** | Save / load numbered slots or explicit file paths |
| 🕹️ **DS Input** | Buttons, touchscreen taps, and batched navigation sequences |
| 📸 **Screen Capture** | Save either screen to PNG for visual verification |
| 🧩 **DS Graphics Helpers** | Decode OAM, DISPCNT, POWCNT, VRAMCNT, and BG tilemaps |
| 🔄 **Patch + Reload** | `system.close_rom` / `system.open_rom` for a build-and-reload loop |

---

## 🚀 Quick Start

### Prerequisites
- [BizHawk](https://github.com/TASEmulators/BizHawk/releases) **2.10+** (verified on 2.11.1) with the **MelonDS** core
- **Python 3.10+**
- An **NDS ROM** (plus its matching `.dsv` save, if you want to resume in-game)

### Installation

1. **Clone the repository:**
   ```bash
   git clone https://github.com/it-was-katsumata/BizHawk-mcp.git
   cd BizHawk-mcp
   ```

2. **Install dependencies** (just the `mcp` Python SDK):
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
AI agent  ──MCP/stdio──►  bizhawk_mcp_server.py  ──TCP 127.0.0.1:9876──►  bizhawk_debug_api.lua  ──►  BizHawk (MelonDS)
                                                  └── JSON-file fallback ──┘
```

The server exposes each capability as an MCP tool (e.g. `emu_debug_read_memory`,
`emu_debug_raw_command`). It forwards commands to the Lua bridge over a localhost
**socket** (port `9876`), automatically falling back to a **JSON file** exchange
(`src/debug_commands.json` → `src/debug_response.json`) if the socket is
unavailable. A heartbeat file (`src/debug_state.json`) lets the server detect a
stale or closed bridge. All of these runtime files are git-ignored.

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
`input.sequence` runs many steps in a single round trip. `press` is a button,
`sleep` is a wall-clock wait (frames keep advancing) for screen transitions:
```
emu_debug_raw_command(
  action="input.sequence",
  params={ seq: "press A; sleep 6; press A; sleep 2.5; press A; sleep 8;
                 press Right; sleep 0.4; press Down; sleep 0.4; press A; sleep 1" }
)
# boots past the splash + intro, picks Continue, then navigates the menu.
```

**3. Capture the screen to verify where you are.**
```
emu_debug_raw_command(action="screenshot.take",
                      params={ path: "C:/BizHawk-mcp/dumps/scene.png" })
```

**4. Inspect the graphics hardware for the current scene.**
```
emu_debug_raw_command(action="powcnt.read")
# → { powcnt1: 0x820F, lcd_swap: true, ... }   (which engine drives which screen)

emu_debug_raw_command(action="oam.parse", params={ engine: "sub" })
# → { sprites: [ { idx: 0, x: 4, y: 8, tile: 0, w_tiles: 4, h_tiles: 4, palette: 5 }, ... ] }
```

**5. Read memory by physical DS address (System Bus), e.g. main RAM.**
```
emu_debug_read_memory_range(address="0x02000000", length=64)
```

**6. Touch the bottom screen** (see the touchscreen note in
[NDS_SETUP.md](NDS_SETUP.md) — a BizHawk XOR quirk requires rebinding the
`Touch` row and parking the mouse cursor):
```
emu_debug_raw_command(action="input.touch", params={ x: 128, y: 96, frames: 8 })
emu_debug_raw_command(action="input.touch_release")
```

**7. Rebuild the ROM and reload it without restarting BizHawk.**
```
emu_debug_raw_command(action="system.close_rom")     # release the file lock
# ... run your patcher, overwriting the .nds on disk ...
emu_debug_raw_command(action="system.open_rom",
                      params={ path: "C:/path/to/patched.nds" })
```

---

## 🔧 Available Commands

Two layers of tools. The MCP tools cover the cross-core debugging surface;
the DS-specific helpers are reached through `emu_debug_raw_command`.

**Core MCP tools** — full reference in **[API_README.md](API_README.md)**:

| Category | Tools |
|----------|-------|
| Memory | `emu_debug_read_memory`, `emu_debug_write_memory`, `emu_debug_read_memory_range`, `emu_debug_search_memory`, `emu_debug_snapshot_memory`, `emu_debug_compare_memory`, `emu_debug_list_memory_domains` |
| CPU | `emu_debug_get_cpu_state`, `emu_debug_get_cpu_registers` |
| Execution | `emu_debug_pause`, `emu_debug_resume`, `emu_debug_step`, `emu_debug_frame_advance` |
| Breakpoints | `emu_debug_add_breakpoint`, `emu_debug_list_breakpoints`, `emu_debug_remove_breakpoint`, `emu_debug_clear_breakpoints`, `emu_debug_get_breakpoint_hits` |
| Trace | `emu_debug_start_trace`, `emu_debug_stop_trace`, `emu_debug_get_trace`, `emu_debug_trace_and_summarize` |
| Watch | `emu_debug_add_watch`, `emu_debug_list_watches`, `emu_debug_remove_watch`, `emu_debug_clear_watches` |
| State / Input | `emu_debug_get_emulator_state`, `emu_debug_save_state`, `emu_debug_load_state`, `emu_debug_set_input`, `emu_debug_raw_command` |

**DS-specific `raw_command` actions** — full reference in **[NDS_SETUP.md](NDS_SETUP.md)**:

| Group | Actions |
|-------|---------|
| Input | `input.set`, `input.touch`, `input.touch_release`, `input.press`, `input.sequence`, `input.debug` |
| Flow | `frame.advance`, `system.reset`, `system.close_rom`, `system.open_rom` |
| Capture | `screenshot.take` |
| Graphics | `powcnt.read`, `dispcnt.decode`, `vramcnt.decode`, `bg.read`, `oam.parse` |

---

## 📁 Project Structure

```
├── src/
│   ├── bizhawk_debug_api.lua    # BizHawk Lua bridge (load in Lua Console)
│   └── bizhawk_mcp_server.py    # Python MCP server (socket + file IPC)
├── lua/
│   └── socket.lua               # LuaSocket shim (upstream; optional)
├── requirements.txt             # Python dependencies (mcp SDK)
├── README.md                    # This file
├── API_README.md                # Core MCP tool reference
└── NDS_SETUP.md                 # Full NDS setup + DS action reference + troubleshooting
```

---

## 📄 License

MIT License — see [LICENSE](LICENSE). This is an NDS-focused fork of an upstream
NES debug bridge; the LICENSE credits both the original author and this fork's
changes.

---

For DS game reverse engineers and AI agents.
