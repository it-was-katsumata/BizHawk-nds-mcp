# BizHawk MCP Debug API Reference (NDS fork)

> ⚠️ **Scope**: tools act on the running emulator; `emu_debug_save_state` and some
> `emu_debug_raw_command` actions also write host files. Each tool carries MCP
> `readOnlyHint` / `destructiveHint` annotations.

Failed calls come back with `success: false`, a top-level `error`, and the MCP
`isError` flag set.

---

## Memory Tools

| Tool | Description | Parameters |
|------|-------------|------------|
| `emu_debug_read_memory` | Read byte from address | `address`, `domain?` |
| `emu_debug_write_memory` | Write byte to address | `address`, `value`, `domain?` |
| `emu_debug_read_memory_range` | Read byte range (max 65536) | `address`, `length`, `domain?` |
| `emu_debug_search_memory` | Search for a byte value (default: all of main RAM) | `value`, `domain?`, `start?`, `length?` |
| `emu_debug_snapshot_memory` | Take a snapshot (default: all of main RAM) | `name?`, `domain?`, `start?`, `length?` |
| `emu_debug_compare_memory` | Compare memory to snapshot | `name?`, `filter?` |
| `emu_debug_list_memory_domains` | List available memory domains | — |

The `address` parameter accepts hex strings (`0x06600000` or `$06600000`) as well
as decimal. Prefer hex strings to avoid manual hex-to-decimal conversion mistakes.
Search and compare return at most 1000 entries; the response's `total` gives the
full count. An unknown `domain` is an error (BizHawk itself would silently use
the current domain instead).

### Memory Domains (MelonDS)

What BizHawk 2.10 / 2.11.x expose for NDS (run `emu_debug_list_memory_domains`
to confirm on your build):

| Domain | Description |
|--------|-------------|
| `ARM9 System Bus` (default) / `ARM7 System Bus` | Address by physical DS address: 0x02000000 main RAM, 0x04000000 I/O, 0x05000000 palette, 0x06xxxxxx VRAM, 0x07000000 OAM |
| `Main RAM` | 4 MB main work RAM; offset 0 = bus 0x02000000 |
| `Shared WRAM` / `ARM7 WRAM` | Shared and ARM7 work RAM |
| `Instruction TCM` / `Data TCM` | ARM9 tightly-coupled memory |
| `ARM9 BIOS` / `ARM7 BIOS` / `Firmware` | BIOS and firmware images |
| `SRAM` / `ROM` | Cartridge save memory and ROM image |

There are no separate palette, VRAM or OAM domains: read those through
`ARM9 System Bus`.

---

## CPU Tools

Returns ARM9 and ARM7 registers r0–r15 (`sp`=r13, `lr`=r14, `pc`=r15) from the
MelonDS core. BizHawk exposes no CPSR, so there are no flags or Thumb bit, and
r15 is the core's pipelined PC (ahead of the executing instruction).

| Tool | Description | Parameters |
|------|-------------|------------|
| `emu_debug_get_cpu_state` | Registers as numbers, per CPU | — |
| `emu_debug_get_cpu_registers` | Registers as hex strings, per CPU | — |

---

## Execution Control

| Tool | Description | Parameters |
|------|-------------|------------|
| `emu_debug_pause` | Pause emulator | — |
| `emu_debug_resume` | Resume emulator | — |
| `emu_debug_step` | Advance frames (`stepType` must be `frame`; MelonDS cannot single-step instructions) | `count?`, `stepType?` |
| `emu_debug_frame_advance` | Advance frames | `count?` |

Both frame tools work while paused and leave the emulator paused.

---

## Breakpoints

Breakpoints are BizHawk memory callbacks on `ARM9 System Bus`. MelonDS only
supports them with JIT disabled (NDS sync settings). Auto-pause takes effect at
the end of the frame, not at the instruction. Execute breakpoints report the
exact instruction address as `PC`; read/write hits report the pipelined r15.

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

Tracing registers an exec callback that fires before every ARM9 instruction, so
emulation slows down heavily while it runs (JIT must be off). `start_trace`
records the addresses of the next `limit` instructions and stops by itself;
`emu_debug_trace_and_summarize` counts executed addresses over a few frames.

| Tool | Description | Parameters |
|------|-------------|------------|
| `emu_debug_start_trace` | Record the next N executed instruction addresses | `limit?` (≤1000) |
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
| `emu_debug_save_state` | Save state to slot 1-10 or a file (writes to disk) | `slot?`, `path?` |
| `emu_debug_load_state` | Load state (fails if the slot/file is missing) | `slot?`, `path?` |
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
| `emu_cheat_find_decrementing_value` | Find main RAM bytes that decrease over time | `initialValue?`, `frames?`, `minDecrement?` |
| `emu_cheat_find_value_on_event` | Two-phase value finder | `phase`, `snapshotName?`, `filter?` |
| `emu_cheat_narrow_candidates` | Narrow candidate addresses (anywhere on the ARM9 bus, within a 4 MB span) | `addresses`, `action`, `filter?` |

---

## Debug Workflow Tools

| Tool | Description | Parameters |
|------|-------------|------------|
| `emu_debug_capture_write_source` | Monitor what writes to an address | `address`, `frames?` |
| `emu_debug_trace_and_summarize` | Count executed-instruction hotspots (slow; default 1 frame, max 60) | `frames?`, `topN?` |

---

## Raw command parameter names

When calling the bridge directly (`emu_debug_raw_command` or the file protocol),
`id` is reserved for the command envelope. Use `bpId` for `breakpoint.remove`
and `freezeId` for `freeze.remove`.

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
2. "Start trace logging" (records the next 1000 instruction addresses)
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
