English | [العربية](MCP_SETUP.ar.md)

# BizHawk Debug Server Setup

> ⚠️ **IMPORTANT**: All tools operate **ONLY on volatile emulator runtime state**. They do **NOT** modify files, server code, configuration, or the host system.

## Installation

```bash
pip install -r requirements.txt
```

---

## Setup

### 1. Start BizHawk

1. **Open BizHawk** and load a ROM
2. **Tools → Lua Console**
3. **Load** `src/bizhawk_debug_api.lua`
4. Script will show "BizHawk Debug API Ready!"

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

## Command Examples

### List Memory Domains

---

## Available Memory Domains

| Domain | Size | Description |
|--------|------|-------------|
| `System Bus` | 64KB | Full address space (default) |
| `RAM` | 2KB | Work RAM |
| `PALRAM` | 32B | PPU color palette |
| `OAM` | 256B | Sprite attribute memory |
| `PRG ROM` | varies | Program ROM |
| `CHR` / `CHR VROM` | varies | Graphics ROM |

---

## NES Palette Reference



---

## Verify Connection

Send this command:
```json
{"id": 1, "action": "cpu.getState"}
```

If working, you'll see CPU registers and frame count in the response.

---

## Available Commands

See [API_README.md](API_README.md) for complete reference.
