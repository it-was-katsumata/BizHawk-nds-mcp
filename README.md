# 🎮 BizHawk Debug Server
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)

**Python server for AI-powered NES game debugging with BizHawk emulator.**

> ⚠️ **IMPORTANT**: All tools operate **ONLY on volatile emulator runtime state**. They do **NOT** modify files, server code, configuration, or the host system.

## ✨ Features

| Feature | Description |
|---------|-------------|
| 🧠 **Memory Access** | Read/write/search RAM, ROM, PALRAM |
| 🔍 **CPU State** | View 6502 registers and flags |
| ⏸️ **Execution Control** | Pause, resume, step frames |
| 🎯 **Breakpoints** | Set read/write/execute breakpoints |
| 📝 **Trace Logging** | Record instruction execution |
| 💾 **Save States** | Save/load emulator states |
| 🎮 **Input Control** | Automate controller input |
| 🔒 **Freeze/Cheats** | Lock memory values (infinite lives, etc.) |

## 🚀 Quick Start

🎥 **Watch Demo on YouTube:**  
https://www.youtube.com/watch?v=9Y7C9A6L8EI

### Prerequisites
- [BizHawk](https://github.com/TASEmulators/BizHawk/releases) 2.9+
- Python 3.10+

### Installation

1. **Clone the repository:**
   ```bash
   git clone https://github.com/AthbiStudio/bizhawk-debug-server.git
   cd bizhawk-debug-server
   ```

2. **Install dependencies:**
   ```bash
   pip install -r requirements.txt
   ```

3. **Start BizHawk:**
   - copy folder lua and paste to BizHawk root.
   - Open BizHawk and load a ROM
   - **Tools → Lua Console**
   - Load `src/bizhawk_debug_api.lua`
   - You'll see: `BizHawk Debug API Ready!`

4. **Run the Python server:**
   ```bash
   python src/bizhawk_mcp_server.py
   ```

5. **Start debugging!** Send JSON commands to `src/debug_commands.json`

## 📖 How It Works

The server communicates via JSON files:

1. **Write command** to `src/debug_commands.json`:
   ```json
   {"id": 1, "action": "memory.read", "address": 17, "domain": "PALRAM"}
   ```

2. **Read response** from `src/debug_response.json`

3. **Verify** `commandId` matches your request

## 📖 Usage Examples

```

```

## 🔧 Available Commands

See [API_README.md](API_README.md) for full reference.

| Category | Commands |
|----------|----------|
| Memory | `memory.read`, `memory.write`, `memory.readRange`, `memory.search` |
| CPU | `cpu.getState`, `cpu.getRegisters` |
| Execution | `execution.pause`, `execution.resume`, `execution.step` |
| Breakpoints | `breakpoint.add`, `breakpoint.remove`, `breakpoint.list` |
| Trace | `trace.start`, `trace.stop`, `trace.get` |
| Cheats | `freeze.add`, `freeze.remove`, `freeze.list` |

## 📁 Project Structure

```
├── src/
│   ├── bizhawk_debug_api.lua    # BizHawk Lua script (load in Lua Console)
│   └── bizhawk_mcp_server.py    # Python server
├── requirements.txt             # Python dependencies
├── README.md                    # This file
├── API_README.md                # Full API reference
├── MCP_SETUP.md                 # Detailed setup instructions
└── lua/
    └── socket.lua               # BizHawk emulator need it to lua script work.
```

## 📄 License

MIT License - see [LICENSE](LICENSE).

---

Made with ❤️ by [AthbiStudio](https://sites.google.com/view/athbistudio/)

For game reverse engineers and AI enthusiasts










