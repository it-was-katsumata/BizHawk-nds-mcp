# BizHawk Debug Server Setup (NDS fork)

> For the complete NDS walkthrough (BizHawk + MelonDS, save states, registering
> the server with Claude Code, and the touchscreen workaround), see
> **[NDS_SETUP.md](NDS_SETUP.md)**. This page is the short version.

> ⚠️ **Scope**: tools act on the running emulator; `emu_debug_save_state`,
> `emu_ds_dump_memory`, `emu_ds_screenshot` with `savePath`, and some
> `emu_debug_raw_command` actions also write host files (see the tool annotations).

## Installation

```bash
pip install -r requirements.txt
```

---

## Setup

### 1. Start BizHawk

1. **Open BizHawk** and load an **NDS** ROM (status bar reads `Nintendo DS`)
2. **Tools → Lua Console**
3. **Load** `src/bizhawk_debug_api.lua`
4. Script will show "BizHawk Debug API Ready!" and `System: NDS`

### 2. Run the Python Server

```bash
python src/bizhawk_mcp_server.py
```

---

## How to Send Commands

By default communication happens via JSON files (the MCP server also offers a
socket fast path when EmuHawk is started with `--socket_ip`/`--socket_port`; see
Step 6 of [NDS_SETUP.md](NDS_SETUP.md)):

1. **Write command** to `src/debug_commands.json` with an `id` different from
   the previous command's (the bridge runs a command when the id *changes*).
   Write it atomically (temp file + rename) so the bridge never reads half a
   command.
2. **Read response** from `src/debug_response.json`
3. **Verify** `commandId` matches before using result

`id` belongs to the envelope: name a breakpoint id `bpId` and a freeze id
`freezeId` in command params.

---

## Available Memory Domains (MelonDS)

Send `memory.getDomains` (or call `emu_debug_list_memory_domains`) to see what
your build exposes. BizHawk 2.10 / 2.11.x give:

| Domain | Description |
|--------|-------------|
| `ARM9 System Bus` (default) / `ARM7 System Bus` | Physical DS addressing |
| `Main RAM` | 4 MB main work RAM; offset 0 = bus 0x02000000 |
| `Shared WRAM` / `ARM7 WRAM` | Shared and ARM7 work RAM |
| `Instruction TCM` / `Data TCM` | ARM9 tightly-coupled memory |
| `ARM9 BIOS` / `ARM7 BIOS` / `Firmware` | BIOS and firmware images |
| `SRAM` / `ROM` | Cartridge save memory and ROM image |

Physical address map for `ARM9 System Bus`: `0x02000000` main RAM, `0x04000000` I/O,
`0x05000000` palette, `0x06xxxxxx` VRAM, `0x07000000` OAM. (There are no separate
palette / VRAM / OAM domains.)

---

## Verify Connection

Send this command:
```json
{"id": 1, "action": "cpu.getState"}
```

If working, you'll see the ARM9/ARM7 registers (`arm9.r0`…`arm9.r15`, same for
`arm7`) and frame count in the response.
(Or, via the MCP server, call `emu_debug_get_emulator_state`.)

---

## Available Commands

See [API_README.md](API_README.md) for complete reference.
