English | [العربية](API_README.ar.md)

# BizHawk MCP Debug API Reference

> ⚠️ **IMPORTANT**: All tools operate **ONLY on volatile emulator runtime state**. They do **NOT** modify files, server code, configuration, or the host system.

---

## Memory Tools

| Tool | Description | Parameters |
|------|-------------|------------|
| `emu_debug_read_memory` | Read byte from address | `address`, `domain?` |
| `emu_debug_write_memory` | Write byte to address | `address`, `value`, `domain?` |
| `emu_debug_read_memory_range` | Read byte range | `address`, `length`, `domain?` |
| `emu_debug_search_memory` | Search for value in RAM | `value` |
| `emu_debug_snapshot_memory` | Take RAM snapshot | `name?` |
| `emu_debug_compare_memory` | Compare RAM to snapshot | `name?`, `filter?` |
| `emu_debug_list_memory_domains` | List available memory domains | — |

### Memory Domains

| Domain | Size | Description |
|--------|------|-------------|
| `System Bus` | 64KB | Full address space (default) |
| `RAM` | 2KB | Work RAM |
| `PALRAM` | 32B | PPU color palette |
| `OAM` | 256B | Sprite attribute memory |
| `PRG ROM` | varies | Program ROM |
| `CHR` / `CHR VROM` | varies | Graphics ROM |

---

## CPU Tools

| Tool | Description | Parameters |
|------|-------------|------------|
| `emu_debug_get_cpu_state` | Get full CPU state with flags | — |
| `emu_debug_get_cpu_registers` | Get registers in hex format | — |

---

## Execution Control

| Tool | Description | Parameters |
|------|-------------|------------|
| `emu_debug_pause` | Pause emulator | — |
| `emu_debug_resume` | Resume emulator | — |
| `emu_debug_step` | Step frame or instruction | `count?`, `stepType?` |
| `emu_debug_frame_advance` | Advance frames | `count?` |

---

## Breakpoints

| Tool | Description | Parameters |
|------|-------------|------------|
| `emu_debug_add_breakpoint` | Add breakpoint | `type`, `address` |
| `emu_debug_list_breakpoints` | List all breakpoints | — |
| `emu_debug_remove_breakpoint` | Remove breakpoint by ID | `id` |
| `emu_debug_clear_breakpoints` | Remove all breakpoints | — |
| `emu_debug_get_breakpoint_hits` | Get all breakpoint hits | — |
| `emu_debug_get_last_breakpoint_hit` | Get most recent hit | — |
| `emu_debug_clear_breakpoint_hits` | Clear hit queue | — |
| `emu_debug_set_breakpoint_auto_pause` | Enable/disable auto-pause | `enabled` |

---

## Disassembly & Trace

| Tool | Description | Parameters |
|------|-------------|------------|
| `emu_debug_disassemble` | Disassemble at address | `address`, `count?` |
| `emu_debug_get_current_instruction` | Get instruction at PC | — |
| `emu_debug_start_trace` | Start trace logging | — |
| `emu_debug_stop_trace` | Stop trace logging | — |
| `emu_debug_get_trace` | Get trace entries | `count?` |
| `emu_debug_clear_trace` | Clear trace log | — |

---

## Watch List

| Tool | Description | Parameters |
|------|-------------|------------|
| `emu_debug_add_watch` | Add address to watch | `address`, `name?` |
| `emu_debug_list_watches` | Get all watched values | — |
| `emu_debug_remove_watch` | Remove watch | `address` |
| `emu_debug_clear_watches` | Clear all watches | — |

---

## State & Input

| Tool | Description | Parameters |
|------|-------------|------------|
| `emu_debug_get_emulator_state` | Get emulator state | — |
| `emu_debug_save_state` | Save emulator state | `slot?`, `path?` |
| `emu_debug_load_state` | Load emulator state | `slot?`, `path?` |
| `emu_debug_set_input` | Set controller input | `buttons?`, `player?` |
| `emu_debug_raw_command` | Send raw command | `action`, `params?` |

---

## Cheat Tools (Freeze/Lock)

| Tool | Description | Parameters |
|------|-------------|------------|
| `emu_cheat_freeze_address` | Freeze address to constant value | `address`, `value`, `label?`, `domain?` |
| `emu_cheat_unfreeze_address` | Remove freeze | `freezeId?`, `address?`, `all?` |
| `emu_cheat_list_freezes` | List active freezes | — |

---

## High-Level Cheat Tools

| Tool | Description | Parameters |
|------|-------------|------------|
| `emu_cheat_find_decrementing_value` | Find values that decrease over time | `initialValue?`, `frames?` |
| `emu_cheat_find_value_on_event` | Two-phase value finder | `phase`, `snapshotName?`, `filter?` |
| `emu_cheat_narrow_candidates` | Narrow candidate addresses | `addresses`, `action`, `filter?` |

---

## Debug Workflow Tools

| Tool | Description | Parameters |
|------|-------------|------------|
| `emu_debug_capture_write_source` | Monitor what writes to an address | `address`, `frames?` |
| `emu_debug_trace_and_summarize` | Trace and find execution hotspots | `frames?`, `topN?` |

---

## Usage Examples

### Change Palette Colors
```
1. "Read PALRAM from address 0 to 31"
2. "Write value 42 (green) to PALRAM address 17"
```

### Find a Memory Address
```
1. "Search memory for value 3" (initial lives)
2. Lose a life, then "Search memory for value 2"
3. "Add a write breakpoint at $075A"
```

### Infinite Lives Cheat
```
1. "Freeze address $075A to value 9"
```

### Trace Execution
```
1. "Pause the emulator"
2. "Start trace logging"
3. "Advance 1 frame"
4. "Show me the trace log"
```

### Automate Input
```
1. "Set input to press A button"
2. "Advance 10 frames"
3. "Clear input"
```
