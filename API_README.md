# BizHawk MCP Debug API Reference (NDS fork)

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

The `address` parameter accepts hex strings (`0x06600000` or `$06600000`) as well
as decimal. Prefer hex strings to avoid manual hex-to-decimal conversion mistakes.

### Memory Domains (MelonDS)

Run `emu_debug_list_memory_domains` to get the exact names your BizHawk build
exposes (they can vary slightly by version). Typical MelonDS domains:

| Domain | Description |
|--------|-------------|
| `System Bus` / `ARM9 System Bus` / `ARM7 System Bus` | Address by physical DS address (default). 0x02000000 main RAM, 0x04000000 I/O, 0x05000000 palette, 0x06xxxxxx VRAM, 0x07000000 OAM |
| `Main RAM` | 4 MB main work RAM (0x02000000) |
| `Shared WRAM` / `ARM7 WRAM` | Shared and ARM7 work RAM |
| `ARM9 ITCM` / `ARM9 DTCM` | ARM9 tightly-coupled memory |
| `Main Palette` / `Sub Palette` | Engine A / Engine B BG+OBJ palettes |
| `OAM` | Sprite attribute memory (both engines) |
| `VRAM` | Video RAM (often a flat block from 0x06000000) |
| `ARM9 BIOS` / `ARM7 BIOS` | BIOS regions |

---

## CPU Tools

Returns the ARM9 / ARM7 registers exposed by the MelonDS core.

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

## Trace

Trace entries record the program counter, the raw opcode byte at the PC, and the
core's register snapshot per logged step.

| Tool | Description | Parameters |
|------|-------------|------------|
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

### Inspect the current scene's sprites
```
1. "Read POWCNT" (powcnt.read — which engine drives which screen)
2. "Parse the sub-engine OAM" (oam.parse engine=sub)
3. "Take a screenshot to dumps/scene.png"
```

### Find a memory address that changes
```
1. "Snapshot main RAM"
2. Trigger the in-game change (spend money, take damage, ...)
3. "Compare main RAM to the snapshot, filter to decreased values"
4. "Add a write breakpoint at the candidate address"
```

### Read by physical DS address
```
1. "Read 64 bytes from 0x02000000" (main RAM via System Bus)
2. "Read 32 bytes from 0x05000000" (BG/OBJ palette)
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
1. "Press A for 8 frames" (input.press)
2. "Touch the bottom screen at (128, 96)" (input.touch — see the touchscreen note in NDS_SETUP.md)
3. "Run a batched navigation sequence" (input.sequence)
```

### Patch and reload a ROM without restarting BizHawk
```
1. "Close the ROM" (system.close_rom — releases the file lock)
2. Run your patcher, overwriting the .nds on disk
3. "Open the patched ROM" (system.open_rom path=...)
```
