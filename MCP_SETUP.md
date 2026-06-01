# BizHawk Debug Server Setup (NDS fork)

> For the complete NDS walkthrough (BizHawk + MelonDS, save states, registering
> the server with Claude Code, and the touchscreen workaround), see
> **[NDS_SETUP.md](NDS_SETUP.md)**. This page is the short version.

> ⚠️ **IMPORTANT**: All tools operate **ONLY on volatile emulator runtime state**. They do **NOT** modify files, server code, configuration, or the host system.

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

Communication happens via JSON files:

1. **Write command** to `src/debug_commands.json` with unique incrementing `id`
2. **Read response** from `src/debug_response.json`
3. **Verify** `commandId` matches before using result

---

## Available Memory Domains (MelonDS)

Exact names vary by BizHawk version — send `list_memory_domains` (or run
`memory.getmemorydomainlist()` in the Lua Console) to see what your build
exposes. Typical MelonDS domains:

| Domain | Description |
|--------|-------------|
| `System Bus` / `ARM9 System Bus` / `ARM7 System Bus` | Physical DS addressing (default) |
| `Main RAM` | 4 MB main work RAM (0x02000000) |
| `Shared WRAM` / `ARM7 WRAM` | Shared and ARM7 work RAM |
| `ARM9 ITCM` / `ARM9 DTCM` | ARM9 tightly-coupled memory |
| `Main Palette` / `Sub Palette` | Engine A / Engine B palettes |
| `OAM` | Sprite attribute memory |
| `VRAM` | Video RAM (flat block from 0x06000000) |

Physical address map for `System Bus`: `0x02000000` main RAM, `0x04000000` I/O,
`0x05000000` palette, `0x06xxxxxx` VRAM, `0x07000000` OAM.

---

## Verify Connection

Send this command:
```json
{"id": 1, "action": "cpu.getState"}
```

If working, you'll see the ARM9/ARM7 registers and frame count in the response.
(Or, via the MCP server, call `emu_debug_get_emulator_state`.)

---

## Available Commands

See [API_README.md](API_README.md) for complete reference.
