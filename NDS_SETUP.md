# NDS Setup

Setup guide for the NDS fork of the BizHawk debug bridge. Paths below use
placeholders like `<venv>` (your Python environment) and `<your-rom>.nds` —
substitute your own. `<repo>` is wherever you cloned this repository.

## Prerequisites

1. **BizHawk 2.10+** for Windows ([releases](https://github.com/TASEmulators/BizHawk/releases)). Verified on **2.11.1**.
2. **Python 3.10+** (a virtual environment is recommended; referred to as `<venv>` below).
3. **NDS ROM + matching `.dsv` save** in a known location (`<your-rom>.nds` + `<your-rom>.dsv`).

## One-time setup

### Step 1 — Python dependencies

```powershell
<venv>\Scripts\pip.exe install -r <repo>\requirements.txt
```

The deps are the `mcp` Python SDK (1.30+ or 2.x; the server supports both) and Pillow (used by `emu_ds_screenshot` to crop and scale). If you see `ModuleNotFoundError: No module named 'mcp'` when the client tries to launch the server, this step was skipped. Confirm with:

```powershell
<venv>\Scripts\python.exe <repo>\src\bizhawk_mcp_server.py
```

— it should print `BizHawk MCP Server starting...` and then wait for stdio. Ctrl+C to exit.

### Step 2 — Verify BizHawk + MelonDS

1. Launch `EmuHawk.exe`.
2. **File → Open ROM** → pick `<your-rom>.nds`.
3. **Nintendo DS** should show in the status bar. The first time you load an NDS ROM, BizHawk may prompt you to point at firmware/BIOS; if so, just dismiss — MelonDS has built-in HLE that works without real firmware.
4. **Place the matching `.dsv`** next to the ROM.
5. Play until **Continue** is selectable. Load it. You should reach the in-game state.

### Step 3 — Capture save-state slots (optional but recommended)

In-game, press `Shift+F1..F10` to save to slots 1..10. Capture a slot for each
scene you expect to revisit (menus, shops, dialogue screens, etc.) so an agent
can jump straight there via `state.load slot=N` instead of replaying the
navigation each time.

State files live in BizHawk's `SaveStates/` folder by default.

### Step 4 — Load the Lua bridge

1. In BizHawk: **Tools → Lua Console**.
2. **Script → Open Script** → pick `<repo>\src\bizhawk_debug_api.lua`.
3. Lua Console should print:
   ```
   ╔═══════════════════════════════════════════╗
   ║  BizHawk Debug API for AI  [FULL CONTROL] ║
   ╠═══════════════════════════════════════════╣
   ...
   BizHawk Debug API Ready!
   System: NDS
   ...
   Available Memory Domains:
     1. Main RAM
     2. Shared WRAM
     3. ARM7 WRAM
     ...
     ARM9 System Bus
     ARM7 System Bus
   ```
4. Confirm `System: NDS` (not `NES`) — that's the signal we're on the right core.
5. For breakpoints and tracing, keep **JIT disabled** in the NDS sync settings:
   MelonDS only supports BizHawk memory callbacks with the interpreter.

Leave the Lua Console open and the script running for the duration of the session.

### Step 5 — Register the MCP server with your client

For Claude Code, register it with the CLI (Claude Code does not read MCP servers
from `settings.json`), substituting your own venv and repo paths:

```powershell
claude mcp add --transport stdio --scope user bizhawk -- <venv>\Scripts\python.exe <repo>\src\bizhawk_mcp_server.py
```

`--scope user` makes it available in every project (stored in `~/.claude.json`).
To share it with a project instead, commit a `.mcp.json` at the project root:

```json
{
  "mcpServers": {
    "bizhawk": {
      "type": "stdio",
      "command": "<venv>\\Scripts\\python.exe",
      "args": ["<repo>\\src\\bizhawk_mcp_server.py"]
    }
  }
}
```

Start a new session (or run `/mcp` to check the connection). Tools prefixed
`mcp__bizhawk__emu_debug_*` and `mcp__bizhawk__emu_ds_*` should be available.

### Step 6 — Socket fast path (optional)

By default commands go through JSON files polled every 50 ms. For about one
frame per command instead, let the bridge use BizHawk's built-in socket client:

1. Start your MCP client first, so the server is listening on `127.0.0.1:9876`
   (change it with the `BIZHAWK_MCP_PORT` environment variable on the server).
2. Then start EmuHawk with the socket flags, e.g. a shortcut or batch file:
   ```powershell
   EmuHawk.exe --socket_ip=127.0.0.1 --socket_port=9876 "<your-rom>.nds"
   ```
3. Load the Lua bridge as usual. The console prints
   `Socket Mode: ENABLED` and then `[SOCKET] Connected to the MCP server`.

EmuHawk opens the connection during startup and **shows a fatal error if nothing
is listening**, so only use the flags while the MCP server runs; otherwise start
EmuHawk normally (file IPC works as before). If the MCP server restarts (for
example a new Claude session), the bridge finds the new one through
`src/debug_server.json` and reconnects within a few seconds, using the files in
the meantime.

## Smoke test

Once BizHawk, the Lua bridge, and the MCP server are all running, from your client:

```
mcp__bizhawk__emu_debug_get_emulator_state()
```

Should return current frame count + system id + CPU registers. If this works, we're connected end-to-end.

Then try the DS tools:

```
emu_ds_screenshot()                 # comes back as an image
emu_ds_read_powcnt()
# → { powcnt1: 0x820F, lcd_swap: true, engine_a_screen: "top", ... }

emu_ds_parse_oam(engine="sub")
# → { sprites: [{idx: 0, y: 8, x: 4, tile: 0, w_tiles: 4, h_tiles: 4, ...}, ...] }
```

## Bridge actions (what the `emu_ds_*` tools wrap)

All of these can also be sent directly with `emu_debug_raw_command(action=..., params={...})`.

| action | params | returns |
|---|---|---|
| `input.set` | `buttons={A=true, Touch=true, "Touch X"=128, "Touch Y"=96}` | digital buttons via `joypad.set` (NDS names are unprefixed: `A`, `Up`, `Touch`), numeric `Touch X`/`Touch Y` routed to `joypad.setanalog`. Accepts both `"Touch X"` (with space, the canonical NDS name) and `"TouchX"` (normalized). Buttons apply to the next frame; touch coords are sticky autoholds until `input.touch_release`. |
| `input.touch` | `x, y, frames=8` | touches at (x, y) for N frames + releases. **Requires the mouse-rebind workaround documented in Troubleshooting** — without it, mouse XOR forces the tap to land at cursor position regardless of (x, y). |
| `input.touch_release` | — | releases Touch and clears the analog autoholds |
| `input.press` | `button="A", frames=8` | press one button for N frames then release. |
| `input.sequence` | `seq="press A; sleep 6; press A; sleep 2.5; press A; sleep 8; press Right; sleep 0.4; press Down; sleep 0.4; press A; sleep 1"` | runs several input steps in ONE command (one round trip for a whole navigation sequence instead of one per button). `seq` is a flat string: steps split by `;`, fields by spaces. Step forms: `press <BTN> [frames]`, `touch <x> <y> [frames]`, `wait <frames>` (emulated frames, default 8), `sleep <seconds>` (advances frames meanwhile: 60 frames per second, or wall-clock time if LuaSocket is installed in BizHawk). Use `sleep` between screen transitions so the next input lands after the game has settled. Prefer `press A`/`press B` over `touch` for "touch to continue" / menu screens — a `touch` lands at fixed coords and can hit the wrong button. Commands time out after 30s (`DEFAULT_TIMEOUT`), so a full ~20s boot-to-screen batch fits in one command. `emu_ds_input_sequence` builds this string from a typed list of steps. The example above is an illustrative boot-to-in-game navigation (skip the boot splash, pick a menu entry, then move the cursor and confirm). |
| `input.debug` | — | returns the full `joypad.get()` / `joypad.get(1)` / `joypad.getimmediate(1)` maps so you can discover which key names the current core exposes. Critical for NDS where touch keys are `Touch X` / `Touch Y` with a literal space, not `TouchX` / `TouchY`. |
| `frame.advance` | `frames=1` | advances N frames (works while paused; stays paused) |
| `screenshot.take` | `path` | saves the display to a PNG file (`emu_ds_screenshot` uses this, then returns the image) |
| `memory.dump_to_file` | `address, length, path, domain?` | writes raw bytes to a file (for large regions such as VRAM or a whole RAM image) |
| `trace.hotspots` | `frames=1, topN=20` | counts executed ARM9 instruction addresses over N frames (≤60; very slow while running, JIT must be off) |
| `system.reset` | — | hard reset / power cycle. Unpauses + clears all button autoholds. |
| `system.close_rom` | — | unloads the current ROM (releases the file handle so a patcher can overwrite). |
| `system.open_rom` | `path` | loads a ROM by absolute path. Pair with `system.close_rom` for a patch-and-reload cycle. |
| `powcnt.read` | — | POWCNT1 + decoded LCD swap state (bit 15 set = engine A on the top screen) and engine A/B enables |
| `dispcnt.decode` | `engine="main"|"sub"` | DISPCNT decoded into BG enables / OBJ / mode / 1D mapping / boundary |
| `vramcnt.decode` | — | VRAMCNT_A..I decoded (enable / mst / offset); H/I are read from 0x04000248/9, past WRAMCNT |
| `bg.read` | `engine, index=0..3` | BG CNT + 24×32 tilemap entries (text BGs). Map/tile addresses include engine A's DISPCNT screen/char base |
| `oam.parse` | `engine` | list of visible sprites with shape/size/tile/palette |

Inherited actions still work: `memory.read`, `memory.write`, `memory.readRange`, `state.save`, `state.load`, `execution.pause/resume/step`, `freeze.add/remove`, etc. `id` is reserved for the command envelope, so `breakpoint.remove` takes `bpId` and `freeze.remove` takes `freezeId`.

## Troubleshooting

**Bridge goes stale ("BizHawk not responding") after closing the ROM, or when started with no ROM**
With no ROM loaded BizHawk runs the "NULL" core, where `emu.frameadvance()`,
`emu.getregisters()` and `emu.framecount()` error or hang. The main loop now
guards against this: it only advances frames while a ROM is loaded (otherwise
it yields), every loop step is `pcall`-wrapped so a single error can't kill the
loop, and the heartbeat (`debug_state.json`) is written every 10 loop iterations even with no
ROM — so the bridge stays alive across `system.close_rom` and resumes on
`system.open_rom`. The state file now includes a `romLoaded` flag. Any edit to
`src/bizhawk_debug_api.lua` only takes effect after you reload the script
(Lua Console → Script → Restart, or relaunch BizHawk).

**Commands time out after restarting the MCP server (without reloading the Lua)**
A restarted server reconnects its socket and resets its command-id counter to a
low value. The bridge handles both now: `checkSocketCommands` accepts a fresh
connection even while holding a stale one (a half-open old socket that never
reports "closed" no longer blocks it), and `checkFileCommands` processes a
command whenever the id *changes* rather than requiring a strictly higher id.
If you still see timeouts right after a server restart, just send one more
command — the bridge re-syncs on the next request.

**Lua Console says "Available Memory Domains:" with NES domains (RAM, PALRAM, etc.)**
You loaded a NES ROM by mistake. Close + load the NDS ROM.

**A memory domain seems to be missing**
`memory.getmemorydomainlist()` is zero-indexed, so a Lua Console loop with
`ipairs` skips the first entry (`Main RAM`). List them with:
```lua
local d = memory.getmemorydomainlist(); for i = 0, #d do console.log(i .. ": " .. d[i]) end
```
or call `emu_debug_list_memory_domains`, which returns the full list.

**Breakpoints or tracing fail with "Could not register memory callback"**
MelonDS only supports memory callbacks with JIT disabled. Turn JIT off in the
NDS sync settings and reload the ROM.

**MCP tools don't appear in Claude Code**
- Run `claude mcp list` and check `bizhawk` is listed and connected (`/mcp` inside a session).
- Start a new session after adding the server.
- Check the Python server runs standalone: `python <repo>\src\bizhawk_mcp_server.py` — should print `BizHawk MCP Server starting...`.

**`screenshot.take` / `emu_ds_screenshot` returns "screenshot failed"**
Check the path is writable. (BizHawk 2.11.1 creates missing folders itself.)

**`emu_ds_screenshot` with `screen="top"`/`"bottom"` says it needs the vertical layout**
Cropping assumes both screens stacked vertically (BizHawk's default NDS screen
layout). With a horizontal, rotated or single-screen layout, use `screen="both"`.

**EmuHawk shows a fatal error at startup after adding `--socket_ip`/`--socket_port`**
Nothing was listening on that port: EmuHawk connects during startup. Start the
MCP client (and so the server) first, or start EmuHawk without the flags.

**Lua Console shows `[SOCKET] … using file IPC until the MCP server is back`**
The connection dropped (usually the MCP server stopped). Commands keep working
through the files; the bridge reconnects by itself once a server is running.

**Touch coords land at the wrong spot (always top-right corner or wherever the mouse cursor is)**

BizHawk 2.10+ has a regression: the touch axes go through an
`XorAdapter` that forces them to neutral when both regular input
(mouse) and sticky input (Lua autohold) are non-neutral. Since the
mouse is almost never at a precisely neutral position, every Lua
`joypad.setanalog` call for `Touch X` / `Touch Y` is XOR'd to the
mouse cursor position. See [issue #4535](https://github.com/TASEmulators/BizHawk/issues/4535)
(still open as of October 2026; a fix for the Lua side was proposed in
[PR #4804](https://github.com/TASEmulators/BizHawk/pull/4804) — check whether
your build includes it). The maintainers' suggested workaround is to unbind the
mouse from all three touch controls (`Touch`, `Touch X`, `Touch Y`) where your
build allows it.

**Workaround (both parts required):**

1. **Rebind `Touch` away from the mouse**. Config → Controllers → NDS,
   find the `Touch` row (defaults to `WMouse L, X1 Touchpad`), and
   replace it with a keyboard key you won't press during testing —
   `F12`, `Pause`, etc. BizHawk requires a binding; you can't leave
   the cell empty.
2. **Park the mouse cursor at the top-left of the BizHawk window**
   (or off the window entirely) before scripted taps. The `Touch X` /
   `Touch Y` rows usually can't be rebound from mouse, so the only
   way to keep their mouse contribution neutral is to physically place
   the cursor at (0, 0). XOR with neutral = passthrough.

After both fixes, `input.touch x=… y=…` lands precisely.

**Touch events still don't register after the XOR fix**

Some games ignore touch on frames when other buttons are also pressed.
Try `input.touch_release` first, then `input.touch` with `frames>=8`.
Verify with `input.debug` that `Touch X` / `Touch Y` show your requested
values (not the default `255 / 0`).

**Key name reference: `Touch X` and `Touch Y` (with a literal space)**

BizHawk's NDS core reports the touch axes with spaces in the names. If
you send `TouchX` or `TouchY` (no space), BizHawk silently drops them.
`input.set` and `input.touch` already normalize the short forms, but if
you script `joypad.setanalog` directly, use `["Touch X"]` and
`["Touch Y"]`. Use the `input.debug` action to verify the exact key
names exposed by your BizHawk + core combination.
