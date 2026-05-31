--[[
  BizHawk/EmuHawk Debug API for AI — NDS adaptation
  Comprehensive debugging interface for AI agents.

  Originally written for NES; this fork adapts it for Nintendo DS via
  the MelonDS core in BizHawk 2.10+.  See translate-r00 project notes
  in C:\ds_translation_patch for context (ROM hacking workflow).

  Memory domains exposed by MelonDS in BizHawk (verified via
  memory.getmemorydomainlist()):
    System Bus / ARM9 System Bus / ARM7 System Bus
    Main RAM         (0x02000000, 4 MB)
    Main RAM (ARM7)  (0x02000000 mirror for ARM7)
    Shared WRAM, ARM7 WRAM, ARM9 ITCM, ARM9 DTCM
    Main Palette, Sub Palette, OAM, ARM9 BIOS, ARM7 BIOS
    VRAM (often presented as a flat block 0x06000000 onward)
  Use System Bus to address by physical DS address (0x02000000 main
  RAM, 0x04000000 I/O, 0x05000000 palette, 0x06xxxxxx VRAM,
  0x07000000 OAM).

  NDS-specific actions added by this adaptation (callable via the
  Python MCP server's emu_debug_raw_command tool):
    screenshot.take {path=...}
    input.touch     {x=..., y=..., frames=N}
    input.touch_release {}
    frame.advance   {frames=N}
    oam.parse       {engine="main"|"sub"}
    dispcnt.decode  {engine="main"|"sub"}
    powcnt.read     {}
    vramcnt.decode  {}
    bg.read         {engine="main"|"sub", index=0..3}

  Features (inherited from upstream):
  - Memory read/write/search/snapshot/compare
  - CPU state access (ARM9 / ARM7 registers exposed by MelonDS)
  - Execution control (pause, step, resume) - FULLY WORKING
  - Memory breakpoints with callbacks
  - Trace logging
  - Watch list

  Usage:
  1. Open BizHawk 2.10+ and load an NDS ROM
  2. Tools > Lua Console
  3. Load this script (Open Script or drag & drop)
  4. Script runs continuously with full control
]]

-- ═══════════════════════════════════════════════════════════════
-- CONFIGURATION
-- ═══════════════════════════════════════════════════════════════

-- Auto-detect BASE_PATH from script location
-- This makes the project portable - works in any directory!
local function getScriptPath()
    local info = debug.getinfo(1, "S")
    local script_path = info.source:match("^@(.*)$") or info.source
    -- Normalize path separators and extract directory
    script_path = script_path:gsub("\\", "/")
    local dir = script_path:match("(.*/)") 
    return dir or "./"
end

local BASE_PATH = getScriptPath()
local STATE_FILE = BASE_PATH .. "debug_state.json"
local COMMAND_FILE = BASE_PATH .. "debug_commands.json"
local RESPONSE_FILE = BASE_PATH .. "debug_response.json"
local TRACE_FILE = BASE_PATH .. "debug_trace.json"

-- NDS main RAM is 4 MB (0x02000000..0x023FFFFF).  Memory.search and
-- Memory.snapshot iterate this on the "Main RAM" domain; cap defaults
-- to a smaller range when called interactively so a full sweep doesn't
-- freeze the emulator for many seconds.
local RAM_SIZE = 0x400000          -- 4 MB Main RAM (full)
local RAM_SEARCH_DEFAULT = 0x40000 -- 256 KB default sweep
-- ARM9 System Bus is the unified domain; Main RAM lives at 0x02000000.
-- Use opts.start = 0x02000000 in Memory.search/snapshot to sweep it.
local DEFAULT_RAM_DOMAIN = "ARM9 System Bus"
local MAIN_RAM_BASE = 0x02000000

-- BizHawk's MelonDS core (verified 2.11.1) exposes these NDS domains:
--   Shared WRAM, ARM7 WRAM, SRAM, ROM, Instruction TCM, Data TCM,
--   ARM9 BIOS, ARM7 BIOS, Firmware,
--   ARM9 System Bus, ARM7 System Bus, Waterbox PageData
-- No separate Main RAM / VRAM / OAM domains — everything is reached
-- through ARM9 System Bus (which gives the ARM9's full memory map:
-- 0x02000000 main RAM, 0x04000000 I/O, 0x05000000 palette,
-- 0x06xxxxxx VRAM, 0x07000000 OAM).
local DEFAULT_SYSTEM_DOMAIN = "ARM9 System Bus"

-- ═══════════════════════════════════════════════════════════════
-- SOCKET CONFIGURATION (LuaSocket - Fast Communication)
-- ═══════════════════════════════════════════════════════════════

local SOCKET_PORT = 9876
local SOCKET_ENABLED = true  -- Try socket first, fallback to file
local socketServer = nil
local socketClient = nil
local socketInitialized = false

-- Try to load LuaSocket
local socket = nil
local socketOk, socketErr = pcall(function()
    socket = require("socket")
end)

if socketOk and socket then
    console.log("[SOCKET] LuaSocket loaded successfully — using TCP")
else
    -- File-IPC fallback is the supported path for this NDS fork.  The
    -- upstream q8fft2 repo ships a lua/socket.lua but BizHawk's Lua
    -- engine doesn't pick it up reliably across versions, and file IPC
    -- works fine for our use case (sub-second latency, no port to
    -- configure).  Silencing the long error trace to reduce noise.
    console.log("[SOCKET] LuaSocket not available — using file IPC (fine, no action needed)")
    SOCKET_ENABLED = false
end


-- Initialize TCP server using LuaSocket
local function initSocketServer()
    if not SOCKET_ENABLED or not socket then return false end
    
    local ok, err = pcall(function()
        socketServer = socket.tcp()
        socketServer:setoption("reuseaddr", true)
        local bindOk, bindErr = socketServer:bind("127.0.0.1", SOCKET_PORT)
        if not bindOk then
            error("Bind failed: " .. tostring(bindErr))
        end
        socketServer:listen(1)
        socketServer:settimeout(0)  -- Non-blocking!
        socketInitialized = true
    end)
    
    if ok then
        console.log("[SOCKET] TCP Server listening on port " .. SOCKET_PORT)
        return true
    else
        console.log("[SOCKET] Server init failed: " .. tostring(err))
        SOCKET_ENABLED = false
        return false
    end
end




-- ═══════════════════════════════════════════════════════════════
-- STATE
-- ═══════════════════════════════════════════════════════════════

local frameCounter = 0
local lastCommandId = 0
local isPausedState = false


-- State constants
local STATE_RUNNING = "RUNNING"
local STATE_PAUSED = "PAUSED"

local currentState = STATE_RUNNING

-- Snapshots for RAM comparison
local snapshots = {}

-- Watched addresses
local watchedAddresses = {}

-- Active breakpoints (memory hooks)
local breakpoints = {}
local nextBreakpointId = 1

-- Trace logging
local traceEnabled = false
local traceLog = {}
local traceMaxLines = 1000

-- Breakpoint hit tracking
local breakpointHitQueue = {}
local maxQueueSize = 100
local autoPauseMode = false  -- If true, pause on breakpoint hit

-- Freeze/Lock infrastructure (for cheats)
local frozenAddresses = {}  -- { [id] = { address, value, label } }
local nextFreezeId = 1
local MAX_FREEZES = 16  -- Safety limit

-- ═══════════════════════════════════════════════════════════════
-- UTILITIES
-- ═══════════════════════════════════════════════════════════════

local function writeFile(path, content)
    local file = io.open(path, "w")
    if file then
        file:write(content)
        file:close()
        return true
    end
    return false
end

local function readFile(path)
    local file = io.open(path, "r")
    if file then
        local content = file:read("*all")
        file:close()
        return content
    end
    return nil
end

-- JSON serializer
local function toJson(tbl)
    if type(tbl) ~= "table" then
        if type(tbl) == "string" then
            return '"' .. tbl:gsub('\\', '\\\\'):gsub('"', '\\"'):gsub('\n', '\\n'):gsub('\r', ''):gsub('[\x00-\x1f]', '') .. '"'
        elseif type(tbl) == "boolean" then
            return tbl and "true" or "false"
        elseif tbl == nil then
            return "null"
        elseif type(tbl) == "function" then
            return "null"
        else
            return tostring(tbl)
        end
    end
    
    local isArray = true
    local count = 0
    for k, _ in pairs(tbl) do
        count = count + 1
        if type(k) ~= "number" or k ~= count then
            isArray = false
            break
        end
    end
    
    local result = isArray and "[" or "{"
    local first = true
    
    if isArray then
        for _, v in ipairs(tbl) do
            if not first then result = result .. "," end
            result = result .. toJson(v)
            first = false
        end
    else
        for k, v in pairs(tbl) do
            if type(v) ~= "function" then
                if not first then result = result .. "," end
                result = result .. '"' .. tostring(k) .. '":' .. toJson(v)
                first = false
            end
        end
    end
    
    return result .. (isArray and "]" or "}")
end

-- JSON parser (simple)
local function parseJson(str)
    if not str or str == "" then return nil end
    str = str:gsub("^%s+", ""):gsub("%s+$", "")
    
    if str:sub(1,1) == "{" then
        local result = {}
        for k, v in str:gmatch('"([^"]+)"%s*:%s*([^,}]+)') do
            v = v:gsub('^%s+', ''):gsub('%s+$', ''):gsub('^"', ''):gsub('"$', '')
            if tonumber(v) then v = tonumber(v)
            elseif v == "true" then v = true
            elseif v == "false" then v = false
            elseif v == "null" then v = nil  -- Handle JSON null
            end
            result[k] = v
        end
        return result
    end
    return nil
end

-- Parse hex or decimal address
local function parseAddress(addr)
    if type(addr) == "number" then return addr end
    if type(addr) == "string" then
        if addr:sub(1,2) == "0x" or addr:sub(1,1) == "$" then
            return tonumber(addr:gsub("[$0x]", ""), 16)
        end
        return tonumber(addr)
    end
    return 0
end

-- Format address as hex
local function hexAddr(addr)
    return string.format("$%04X", addr)
end

local function hexByte(val)
    return string.format("$%02X", val or 0)
end

local function log(msg)
    console.log("[DEBUG API] " .. msg)
end

-- ═══════════════════════════════════════════════════════════════
-- MEMORY API (BizHawk-specific)
-- ═══════════════════════════════════════════════════════════════

local Memory = {}

-- Set memory domain (default = ARM9 System Bus for NDS)
local function setMemoryDomain(domain)
    -- Handle nil, empty string, or "null" (from JSON)
    if domain == nil or domain == "" or domain == "null" then
        domain = DEFAULT_SYSTEM_DOMAIN
    end
    local ok = pcall(function()
        memory.usememorydomain(domain)
    end)
    return ok
end

-- Initialize with the NDS unified address domain
setMemoryDomain(DEFAULT_SYSTEM_DOMAIN)

function Memory.read(addr, domain)
    addr = parseAddress(addr)
    if addr == nil then
        return { error = "Invalid address" }
    end
    
    -- Handle nil, empty string, or "null" domain
    if domain and domain ~= "" and domain ~= "null" then
        local oldDomain = memory.getcurrentmemorydomain()
        setMemoryDomain(domain)
        local val = memory.readbyte(addr)
        setMemoryDomain(oldDomain)
        return val
    end
    
    return memory.readbyte(addr)
end

function Memory.write(addr, value, domain)
    addr = parseAddress(addr)
    if addr == nil then
        return { error = "Invalid address" }
    end
    
    -- Handle nil, empty string, or "null" domain
    if domain and domain ~= "" and domain ~= "null" then
        local oldDomain = memory.getcurrentmemorydomain()
        setMemoryDomain(domain)
        memory.writebyte(addr, value)
        setMemoryDomain(oldDomain)
        return
    end
    
    memory.writebyte(addr, value)
end

function Memory.readRange(startAddr, length, domain)
    startAddr = parseAddress(startAddr)
    
    -- Validate startAddr
    if startAddr == nil then
        return { error = "Invalid address: address parameter is nil or invalid" }
    end
    
    -- Validate length
    length = length or 16  -- Default to 16 bytes
    if type(length) ~= "number" or length < 1 then
        return { error = "Invalid length: must be a positive number" }
    end
    
    local data = {}
    
    -- Handle nil, empty string, or "null" domain
    if domain and domain ~= "" and domain ~= "null" then
        local oldDomain = memory.getcurrentmemorydomain()
        setMemoryDomain(domain)
        for i = 0, length - 1 do
            table.insert(data, memory.readbyte(startAddr + i))
        end
        setMemoryDomain(oldDomain)
        return data
    end
    
    for i = 0, length - 1 do
        table.insert(data, memory.readbyte(startAddr + i))
    end
    return data
end

function Memory.search(value, opts)
    opts = opts or {}
    local domain = opts.domain or DEFAULT_RAM_DOMAIN
    local startAddr = opts.start or 0
    local endAddr = opts.endAddr or (startAddr + RAM_SEARCH_DEFAULT)
    local results = {}
    setMemoryDomain(domain)
    for addr = startAddr, endAddr - 1 do
        if memory.readbyte(addr) == value then
            table.insert(results, {
                address = addr,
                hex = hexAddr(addr),
                value = value
            })
        end
    end
    setMemoryDomain(DEFAULT_SYSTEM_DOMAIN)
    return results
end

function Memory.searchRange(minVal, maxVal, opts)
    opts = opts or {}
    local domain = opts.domain or DEFAULT_RAM_DOMAIN
    local startAddr = opts.start or 0
    local endAddr = opts.endAddr or (startAddr + RAM_SEARCH_DEFAULT)
    local results = {}
    setMemoryDomain(domain)
    for addr = startAddr, endAddr - 1 do
        local val = memory.readbyte(addr)
        if val >= minVal and val <= maxVal then
            table.insert(results, {
                address = addr,
                hex = hexAddr(addr),
                value = val
            })
        end
    end
    setMemoryDomain(DEFAULT_SYSTEM_DOMAIN)
    return results
end

function Memory.snapshot(name, opts)
    name = name or "default"
    opts = opts or {}
    local domain = opts.domain or DEFAULT_RAM_DOMAIN
    local startAddr = opts.start or 0
    local endAddr = opts.endAddr or (startAddr + RAM_SEARCH_DEFAULT)
    local data = {}
    setMemoryDomain(domain)
    for addr = startAddr, endAddr - 1 do
        data[addr] = memory.readbyte(addr)
    end
    setMemoryDomain(DEFAULT_SYSTEM_DOMAIN)
    snapshots[name] = { data = data, start = startAddr, endAddr = endAddr, domain = domain }
    return { name = name, size = endAddr - startAddr, domain = domain }
end

function Memory.compare(name, filter)
    name = name or "default"
    filter = filter or "changed"

    local snapshot = snapshots[name]
    if not snapshot then return { error = "Snapshot not found" } end

    local changes = {}
    setMemoryDomain(snapshot.domain or DEFAULT_RAM_DOMAIN)
    for addr = snapshot.start, snapshot.endAddr - 1 do
        local current = memory.readbyte(addr)
        local old = snapshot.data[addr]
        local diff = current - old

        local include = false
        if filter == "changed" then include = (diff ~= 0)
        elseif filter == "increased" then include = (diff > 0)
        elseif filter == "decreased" then include = (diff < 0)
        elseif filter == "same" then include = (diff == 0)
        end

        if include then
            table.insert(changes, {
                address = addr,
                hex = hexAddr(addr),
                old = old,
                new = current,
                diff = diff
            })
        end
    end
    setMemoryDomain(DEFAULT_SYSTEM_DOMAIN)
    return changes
end

function Memory.getDomains()
    return memory.getmemorydomainlist()
end

-- ═══════════════════════════════════════════════════════════════
-- CPU STATE API (BizHawk-specific)
-- ═══════════════════════════════════════════════════════════════

local CPU = {}

function CPU.getState()
    -- BizHawk uses emu.getregisters() for NES
    local regs = emu.getregisters()
    if not regs then
        return { error = "Cannot get CPU state" }
    end
    
    local a = regs.A or regs.a or 0
    local x = regs.X or regs.x or 0
    local y = regs.Y or regs.y or 0
    local sp = regs.S or regs.SP or regs.sp or 0
    local pc = regs.PC or regs.pc or 0
    local ps = regs.P or regs.PS or regs.ps or 0
    
    return {
        -- Registers
        A = a,
        X = x,
        Y = y,
        SP = sp,
        PC = pc,
        
        -- Hex formatted
        A_hex = hexByte(a),
        X_hex = hexByte(x),
        Y_hex = hexByte(y),
        SP_hex = hexByte(sp),
        PC_hex = hexAddr(pc),
        
        -- Flags
        flags = {
            N = (ps & 0x80) ~= 0, -- Negative
            V = (ps & 0x40) ~= 0, -- Overflow
            B = (ps & 0x10) ~= 0, -- Break
            D = (ps & 0x08) ~= 0, -- Decimal
            I = (ps & 0x04) ~= 0, -- Interrupt
            Z = (ps & 0x02) ~= 0, -- Zero
            C = (ps & 0x01) ~= 0  -- Carry
        },
        
        -- Status register
        PS = ps,
        PS_hex = hexByte(ps),
        
        -- Frame count
        frameCount = emu.framecount()
    }
end

function CPU.getRegisters()
    local state = CPU.getState()
    if state.error then
        return { A = "$00", X = "$00", Y = "$00", SP = "$00", PC = "$0000", PS = "$00" }
    end
    return {
        A = state.A_hex,
        X = state.X_hex,
        Y = state.Y_hex,
        SP = state.SP_hex,
        PC = state.PC_hex,
        PS = state.PS_hex
    }
end

function CPU.getPC()
    local regs = emu.getregisters()
    if not regs then return 0 end
    return regs.PC or regs.pc or 0
end

-- ═══════════════════════════════════════════════════════════════
-- EXECUTION CONTROL API (BizHawk-specific)
-- BizHawk has FULL control - no limitations like Mesen!
-- ═══════════════════════════════════════════════════════════════

local Execution = {}

function Execution.getState()
    return {
        state = currentState,
        isPaused = isPausedState,
        frameCount = emu.framecount()
    }
end

-- PAUSE - BizHawk fully pauses with client.pause()
function Execution.pause()
    client.pause()
    isPausedState = true
    currentState = STATE_PAUSED
    
    log("Paused - Full control available!")
    
    return {
        success = true,
        paused = true,
        note = "BizHawk paused. Full AI control available. Use resume to continue."
    }
end

-- RESUME
function Execution.resume()
    client.unpause()
    isPausedState = false
    currentState = STATE_RUNNING
    
    log("Resumed")
    
    return {
        success = true,
        paused = false
    }
end

-- STEP - Frame advance
function Execution.step(count, stepType)
    count = count or 1
    stepType = stepType or "frame"
    
    local pcBefore = CPU.getPC()
    
    if stepType == "frame" then
        for i = 1, count do
            emu.frameadvance()
        end
    elseif stepType == "instruction" then
        -- BizHawk doesn't have single instruction step in all cores
        -- Use emu.yield() or short frame advances
        for i = 1, count do
            emu.yield()
        end
    end
    
    return {
        stepped = count,
        type = stepType,
        pcBefore = hexAddr(pcBefore),
        newPC = hexAddr(CPU.getPC())
    }
end

-- ═══════════════════════════════════════════════════════════════
-- BREAKPOINTS API (BizHawk-specific)
-- Uses event.onmemoryread/onmemorywrite
-- ═══════════════════════════════════════════════════════════════

local Breakpoints = {}

function Breakpoints.add(bpType, addr, options)
    addr = parseAddress(addr)
    options = options or {}
    
    local id = nextBreakpointId
    nextBreakpointId = nextBreakpointId + 1
    
    -- Create callback function
    local callback = function(address, value, flags)
        local pc = CPU.getPC()
        local regs = CPU.getRegisters()
        
        local info = {
            breakpointId = id,
            type = bpType,
            address = hexAddr(address),
            value = value,
            PC = hexAddr(pc),
            registers = regs,
            timestamp = os.time(),
            frameCount = emu.framecount()
        }
        
        -- Add to hit queue
        table.insert(breakpointHitQueue, info)
        if #breakpointHitQueue > maxQueueSize then
            table.remove(breakpointHitQueue, 1)
        end
        
        -- Update hit count
        if breakpoints[id] then
            breakpoints[id].hitCount = (breakpoints[id].hitCount or 0) + 1
        end
        
        -- Write break info to file
        writeFile(BASE_PATH .. "debug_break.json", toJson(info))
        log("Breakpoint Hit at " .. hexAddr(pc) .. " address=" .. hexAddr(address) .. " value=" .. tostring(value))
        
        -- Pause if autoPauseMode is enabled
        if autoPauseMode then
            client.pause()
            isPausedState = true
        end
    end
    
    -- Register the appropriate event
    local eventGuid = nil
    if bpType == "read" then
        eventGuid = event.onmemoryread(callback, addr, DEFAULT_SYSTEM_DOMAIN)
    elseif bpType == "write" then
        eventGuid = event.onmemorywrite(callback, addr, DEFAULT_SYSTEM_DOMAIN)
    elseif bpType == "execute" then
        eventGuid = event.onmemoryexecute(callback, addr, DEFAULT_SYSTEM_DOMAIN)
    else
        return { error = "Invalid breakpoint type: " .. tostring(bpType) }
    end
    
    -- Store breakpoint info
    breakpoints[id] = {
        id = id,
        type = bpType,
        address = addr,
        addressHex = hexAddr(addr),
        enabled = true,
        eventGuid = eventGuid,
        hitCount = 0
    }
    
    log("Breakpoint added: " .. bpType .. " at " .. hexAddr(addr) .. " (id=" .. id .. ")")
    
    return breakpoints[id]
end

function Breakpoints.remove(id)
    local bp = breakpoints[id]
    if not bp then
        return { error = "Breakpoint not found: " .. tostring(id) }
    end
    
    -- Remove event callback
    if bp.eventGuid then
        event.unregisterbyid(bp.eventGuid)
    end
    
    breakpoints[id] = nil
    log("Breakpoint removed: id=" .. id)
    return { removed = id }
end

function Breakpoints.list()
    local list = {}
    for id, bp in pairs(breakpoints) do
        table.insert(list, bp)
    end
    return list
end

function Breakpoints.clear()
    for id, bp in pairs(breakpoints) do
        if bp.eventGuid then
            event.unregisterbyid(bp.eventGuid)
        end
    end
    breakpoints = {}
    log("All breakpoints cleared")
    return { cleared = true }
end

function Breakpoints.getHits()
    return breakpointHitQueue
end

function Breakpoints.getLastHit()
    if #breakpointHitQueue > 0 then
        return breakpointHitQueue[#breakpointHitQueue]
    end
    return { noHits = true, message = "No breakpoint hits recorded yet" }
end

function Breakpoints.clearHits()
    breakpointHitQueue = {}
    return { cleared = true }
end

function Breakpoints.setAutoMode(enabled)
    autoPauseMode = enabled or false
    return { 
        autoPauseMode = autoPauseMode, 
        message = autoPauseMode and "Auto-pause mode enabled" or "Non-blocking mode" 
    }
end

-- ═══════════════════════════════════════════════════════════════
-- DISASSEMBLY API
-- ═══════════════════════════════════════════════════════════════

local Disasm = {}

-- NES 6502 instruction set
local opcodeNames = {
    [0x00] = "BRK", [0x01] = "ORA", [0x05] = "ORA", [0x06] = "ASL",
    [0x08] = "PHP", [0x09] = "ORA", [0x0A] = "ASL", [0x0D] = "ORA",
    [0x0E] = "ASL", [0x10] = "BPL", [0x11] = "ORA", [0x15] = "ORA",
    [0x16] = "ASL", [0x18] = "CLC", [0x19] = "ORA", [0x1D] = "ORA",
    [0x1E] = "ASL", [0x20] = "JSR", [0x21] = "AND", [0x24] = "BIT",
    [0x25] = "AND", [0x26] = "ROL", [0x28] = "PLP", [0x29] = "AND",
    [0x2A] = "ROL", [0x2C] = "BIT", [0x2D] = "AND", [0x2E] = "ROL",
    [0x30] = "BMI", [0x31] = "AND", [0x35] = "AND", [0x36] = "ROL",
    [0x38] = "SEC", [0x39] = "AND", [0x3D] = "AND", [0x3E] = "ROL",
    [0x40] = "RTI", [0x41] = "EOR", [0x45] = "EOR", [0x46] = "LSR",
    [0x48] = "PHA", [0x49] = "EOR", [0x4A] = "LSR", [0x4C] = "JMP",
    [0x4D] = "EOR", [0x4E] = "LSR", [0x50] = "BVC", [0x51] = "EOR",
    [0x55] = "EOR", [0x56] = "LSR", [0x58] = "CLI", [0x59] = "EOR",
    [0x5D] = "EOR", [0x5E] = "LSR", [0x60] = "RTS", [0x61] = "ADC",
    [0x65] = "ADC", [0x66] = "ROR", [0x68] = "PLA", [0x69] = "ADC",
    [0x6A] = "ROR", [0x6C] = "JMP", [0x6D] = "ADC", [0x6E] = "ROR",
    [0x70] = "BVS", [0x71] = "ADC", [0x75] = "ADC", [0x76] = "ROR",
    [0x78] = "SEI", [0x79] = "ADC", [0x7D] = "ADC", [0x7E] = "ROR",
    [0x81] = "STA", [0x84] = "STY", [0x85] = "STA", [0x86] = "STX",
    [0x88] = "DEY", [0x8A] = "TXA", [0x8C] = "STY", [0x8D] = "STA",
    [0x8E] = "STX", [0x90] = "BCC", [0x91] = "STA", [0x94] = "STY",
    [0x95] = "STA", [0x96] = "STX", [0x98] = "TYA", [0x99] = "STA",
    [0x9A] = "TXS", [0x9D] = "STA", [0xA0] = "LDY", [0xA1] = "LDA",
    [0xA2] = "LDX", [0xA4] = "LDY", [0xA5] = "LDA", [0xA6] = "LDX",
    [0xA8] = "TAY", [0xA9] = "LDA", [0xAA] = "TAX", [0xAC] = "LDY",
    [0xAD] = "LDA", [0xAE] = "LDX", [0xB0] = "BCS", [0xB1] = "LDA",
    [0xB4] = "LDY", [0xB5] = "LDA", [0xB6] = "LDX", [0xB8] = "CLV",
    [0xB9] = "LDA", [0xBA] = "TSX", [0xBC] = "LDY", [0xBD] = "LDA",
    [0xBE] = "LDX", [0xC0] = "CPY", [0xC1] = "CMP", [0xC4] = "CPY",
    [0xC5] = "CMP", [0xC6] = "DEC", [0xC8] = "INY", [0xC9] = "CMP",
    [0xCA] = "DEX", [0xCC] = "CPY", [0xCD] = "CMP", [0xCE] = "DEC",
    [0xD0] = "BNE", [0xD1] = "CMP", [0xD5] = "CMP", [0xD6] = "DEC",
    [0xD8] = "CLD", [0xD9] = "CMP", [0xDD] = "CMP", [0xDE] = "DEC",
    [0xE0] = "CPX", [0xE1] = "SBC", [0xE4] = "CPX", [0xE5] = "SBC",
    [0xE6] = "INC", [0xE8] = "INX", [0xE9] = "SBC", [0xEA] = "NOP",
    [0xEC] = "CPX", [0xED] = "SBC", [0xEE] = "INC", [0xF0] = "BEQ",
    [0xF1] = "SBC", [0xF5] = "SBC", [0xF6] = "INC", [0xF8] = "SED",
    [0xF9] = "SBC", [0xFD] = "SBC", [0xFE] = "INC"
}

-- Instruction sizes by addressing mode
local opcodeSizes = {
    [0x00] = 1, [0x01] = 2, [0x05] = 2, [0x06] = 2, [0x08] = 1, [0x09] = 2,
    [0x0A] = 1, [0x0D] = 3, [0x0E] = 3, [0x10] = 2, [0x11] = 2, [0x15] = 2,
    [0x16] = 2, [0x18] = 1, [0x19] = 3, [0x1D] = 3, [0x1E] = 3, [0x20] = 3,
    [0x21] = 2, [0x24] = 2, [0x25] = 2, [0x26] = 2, [0x28] = 1, [0x29] = 2,
    [0x2A] = 1, [0x2C] = 3, [0x2D] = 3, [0x2E] = 3, [0x30] = 2, [0x31] = 2,
    [0x35] = 2, [0x36] = 2, [0x38] = 1, [0x39] = 3, [0x3D] = 3, [0x3E] = 3,
    [0x40] = 1, [0x41] = 2, [0x45] = 2, [0x46] = 2, [0x48] = 1, [0x49] = 2,
    [0x4A] = 1, [0x4C] = 3, [0x4D] = 3, [0x4E] = 3, [0x50] = 2, [0x51] = 2,
    [0x55] = 2, [0x56] = 2, [0x58] = 1, [0x59] = 3, [0x5D] = 3, [0x5E] = 3,
    [0x60] = 1, [0x61] = 2, [0x65] = 2, [0x66] = 2, [0x68] = 1, [0x69] = 2,
    [0x6A] = 1, [0x6C] = 3, [0x6D] = 3, [0x6E] = 3, [0x70] = 2, [0x71] = 2,
    [0x75] = 2, [0x76] = 2, [0x78] = 1, [0x79] = 3, [0x7D] = 3, [0x7E] = 3,
    [0x81] = 2, [0x84] = 2, [0x85] = 2, [0x86] = 2, [0x88] = 1, [0x8A] = 1,
    [0x8C] = 3, [0x8D] = 3, [0x8E] = 3, [0x90] = 2, [0x91] = 2, [0x94] = 2,
    [0x95] = 2, [0x96] = 2, [0x98] = 1, [0x99] = 3, [0x9A] = 1, [0x9D] = 3,
    [0xA0] = 2, [0xA1] = 2, [0xA2] = 2, [0xA4] = 2, [0xA5] = 2, [0xA6] = 2,
    [0xA8] = 1, [0xA9] = 2, [0xAA] = 1, [0xAC] = 3, [0xAD] = 3, [0xAE] = 3,
    [0xB0] = 2, [0xB1] = 2, [0xB4] = 2, [0xB5] = 2, [0xB6] = 2, [0xB8] = 1,
    [0xB9] = 3, [0xBA] = 1, [0xBC] = 3, [0xBD] = 3, [0xBE] = 3, [0xC0] = 2,
    [0xC1] = 2, [0xC4] = 2, [0xC5] = 2, [0xC6] = 2, [0xC8] = 1, [0xC9] = 2,
    [0xCA] = 1, [0xCC] = 3, [0xCD] = 3, [0xCE] = 3, [0xD0] = 2, [0xD1] = 2,
    [0xD5] = 2, [0xD6] = 2, [0xD8] = 1, [0xD9] = 3, [0xDD] = 3, [0xDE] = 3,
    [0xE0] = 2, [0xE1] = 2, [0xE4] = 2, [0xE5] = 2, [0xE6] = 2, [0xE8] = 1,
    [0xE9] = 2, [0xEA] = 1, [0xEC] = 3, [0xED] = 3, [0xEE] = 3, [0xF0] = 2,
    [0xF1] = 2, [0xF5] = 2, [0xF6] = 2, [0xF8] = 1, [0xF9] = 3, [0xFD] = 3,
    [0xFE] = 3
}

function Disasm.getInstruction(addr)
    addr = parseAddress(addr)
    local opcode = Memory.read(addr)
    local size = opcodeSizes[opcode] or 1
    local name = opcodeNames[opcode] or "???"
    
    local bytes = string.format("%02X", opcode)
    local operand = ""
    
    if size == 2 then
        local b1 = Memory.read(addr + 1)
        bytes = bytes .. string.format(" %02X", b1)
        operand = string.format("#$%02X", b1)
    elseif size == 3 then
        local b1 = Memory.read(addr + 1)
        local b2 = Memory.read(addr + 2)
        bytes = bytes .. string.format(" %02X %02X", b1, b2)
        operand = string.format("$%04X", b1 + b2 * 256)
    end
    
    return {
        address = addr,
        addressHex = hexAddr(addr),
        opcode = opcode,
        bytes = bytes,
        mnemonic = name,
        operand = operand,
        asm = name .. (operand ~= "" and " " .. operand or ""),
        size = size
    }
end

function Disasm.disassemble(startAddr, count)
    startAddr = parseAddress(startAddr)
    count = count or 20
    
    local instructions = {}
    local addr = startAddr
    
    for i = 1, count do
        local inst = Disasm.getInstruction(addr)
        table.insert(instructions, inst)
        addr = addr + inst.size
    end
    
    return instructions
end

function Disasm.getCurrentInstruction()
    return Disasm.getInstruction(CPU.getPC())
end

-- ═══════════════════════════════════════════════════════════════
-- TRACE LOGGER API
-- ═══════════════════════════════════════════════════════════════

local Trace = {}

function Trace.start()
    traceEnabled = true
    traceLog = {}
    return { tracing = true }
end

function Trace.stop()
    traceEnabled = false
    return { tracing = false, lineCount = #traceLog }
end

function Trace.clear()
    traceLog = {}
    return { cleared = true }
end

function Trace.get(count)
    count = count or 100
    local result = {}
    local start = math.max(1, #traceLog - count + 1)
    for i = start, #traceLog do
        table.insert(result, traceLog[i])
    end
    return result
end

function Trace.logInstruction()
    if not traceEnabled then return end
    if #traceLog >= traceMaxLines then
        table.remove(traceLog, 1)
    end
    
    local cpu = CPU.getState()
    local inst = Disasm.getCurrentInstruction()
    
    table.insert(traceLog, {
        pc = inst.addressHex,
        asm = inst.asm,
        a = cpu.A_hex,
        x = cpu.X_hex,
        y = cpu.Y_hex,
        sp = cpu.SP_hex,
        ps = cpu.PS_hex
    })
end

-- ═══════════════════════════════════════════════════════════════
-- WATCH API
-- ═══════════════════════════════════════════════════════════════

local Watch = {}

function Watch.add(addr, name)
    addr = parseAddress(addr)
    watchedAddresses[addr] = {
        address = addr,
        hex = hexAddr(addr),
        name = name or hexAddr(addr),
        value = Memory.read(addr)
    }
    return watchedAddresses[addr]
end

function Watch.remove(addr)
    addr = parseAddress(addr)
    watchedAddresses[addr] = nil
    return { removed = addr }
end

function Watch.list()
    local result = {}
    for addr, info in pairs(watchedAddresses) do
        info.value = Memory.read(addr)
        info.valueHex = hexByte(info.value)
        table.insert(result, info)
    end
    return result
end

function Watch.clear()
    watchedAddresses = {}
    return { cleared = true }
end

-- ═══════════════════════════════════════════════════════════════
-- FREEZE/LOCK API (for cheats)
-- ═══════════════════════════════════════════════════════════════

local Freeze = {}

function Freeze.add(addr, value, label, domain)
    addr = parseAddress(addr)
    
    -- Normalize domain (nil, empty, or "null" becomes default = ARM9 System Bus on NDS)
    if domain == nil or domain == "" or domain == "null" then
        domain = DEFAULT_SYSTEM_DOMAIN
    end
    
    -- Check freeze limit
    local count = 0
    for _ in pairs(frozenAddresses) do count = count + 1 end
    if count >= MAX_FREEZES then
        return { error = "Maximum freeze limit reached (" .. MAX_FREEZES .. ")" }
    end
    
    -- Check if already frozen
    for id, freeze in pairs(frozenAddresses) do
        if freeze.address == addr then
            return { error = "Address already frozen", existingId = id }
        end
    end
    
    local id = nextFreezeId
    nextFreezeId = nextFreezeId + 1
    
    frozenAddresses[id] = {
        id = id,
        address = addr,
        addressHex = hexAddr(addr),
        value = value,
        valueHex = hexByte(value),
        label = label or hexAddr(addr),
        domain = domain
    }
    
    log("Freeze added: " .. hexAddr(addr) .. " = " .. hexByte(value) .. " domain=" .. domain .. " (id=" .. id .. ")")
    
    return frozenAddresses[id]
end

function Freeze.remove(id, addr, removeAll)
    if removeAll then
        frozenAddresses = {}
        nextFreezeId = 1
        log("All freezes removed")
        return { cleared = true }
    end
    
    if id then
        if frozenAddresses[id] then
            local removed = frozenAddresses[id]
            frozenAddresses[id] = nil
            log("Freeze removed: id=" .. id)
            return { removed = id, address = removed.addressHex }
        else
            return { error = "Freeze not found: " .. tostring(id) }
        end
    end
    
    if addr then
        addr = parseAddress(addr)
        for fid, freeze in pairs(frozenAddresses) do
            if freeze.address == addr then
                frozenAddresses[fid] = nil
                log("Freeze removed by address: " .. hexAddr(addr))
                return { removed = fid, address = hexAddr(addr) }
            end
        end
        return { error = "No freeze found at address: " .. hexAddr(addr) }
    end
    
    return { error = "No id, address, or removeAll specified" }
end

function Freeze.list()
    local result = {}
    for id, freeze in pairs(frozenAddresses) do
        table.insert(result, freeze)
    end
    return result
end

function Freeze.apply()
    -- Called every frame to maintain frozen values
    for id, freeze in pairs(frozenAddresses) do
        -- Use domain-aware write
        Memory.write(freeze.address, freeze.value, freeze.domain)
    end
end

-- ═══════════════════════════════════════════════════════════════
-- COMMAND PROCESSOR
-- ═══════════════════════════════════════════════════════════════

local function processCommand(cmd)
    if not cmd or not cmd.action then
        return { error = "Invalid command" }
    end
    
    local action = cmd.action
    local response = { success = true, action = action }
    
    -- Memory commands
    if action == "memory.read" then
        response.data = Memory.read(cmd.address, cmd.domain)
        
    elseif action == "memory.write" then
        Memory.write(cmd.address, cmd.value, cmd.domain)
        response.data = { written = true }
        
    elseif action == "memory.readRange" then
        response.data = Memory.readRange(cmd.address, cmd.length, cmd.domain)
        
    elseif action == "memory.search" then
        response.data = Memory.search(cmd.value)
        
    elseif action == "memory.snapshot" then
        response.data = Memory.snapshot(cmd.name)
        
    elseif action == "memory.compare" then
        response.data = Memory.compare(cmd.name, cmd.filter)
        
    elseif action == "memory.getDomains" then
        response.data = Memory.getDomains()
        
    -- CPU commands
    elseif action == "cpu.getState" then
        response.data = CPU.getState()
        
    elseif action == "cpu.getRegisters" then
        response.data = CPU.getRegisters()
        
    -- Execution commands
    elseif action == "execution.pause" or action == "pause" then
        response.data = Execution.pause()
        
    elseif action == "execution.resume" or action == "resume" then
        response.data = Execution.resume()
        
    elseif action == "execution.step" or action == "step" then
        response.data = Execution.step(cmd.count or cmd.frames, cmd.stepType)
        
    elseif action == "execution.getState" then
        response.data = Execution.getState()
        
    -- Breakpoint commands
    elseif action == "breakpoint.add" then
        response.data = Breakpoints.add(cmd.type, cmd.address, cmd.options)
        
    elseif action == "breakpoint.remove" then
        response.data = Breakpoints.remove(cmd.id)
        
    elseif action == "breakpoint.list" then
        response.data = Breakpoints.list()
        
    elseif action == "breakpoint.clear" then
        response.data = Breakpoints.clear()
        
    elseif action == "breakpoint.setAutoMode" then
        response.data = Breakpoints.setAutoMode(cmd.enabled)
        
    elseif action == "breakpoint.getHits" then
        response.data = Breakpoints.getHits()
        
    elseif action == "breakpoint.getLastHit" then
        response.data = Breakpoints.getLastHit()
        
    elseif action == "breakpoint.clearHits" then
        response.data = Breakpoints.clearHits()
        
    -- Disassembly commands
    elseif action == "disasm.get" then
        response.data = Disasm.disassemble(cmd.address, cmd.count)
        
    elseif action == "disasm.current" then
        response.data = Disasm.getCurrentInstruction()
        
    -- Trace commands
    elseif action == "trace.start" then
        response.data = Trace.start()
        
    elseif action == "trace.stop" then
        response.data = Trace.stop()
        
    elseif action == "trace.get" then
        response.data = Trace.get(cmd.count)
        
    elseif action == "trace.clear" then
        response.data = Trace.clear()
        
    -- Watch commands
    elseif action == "watch.add" then
        response.data = Watch.add(cmd.address, cmd.name)
        
    elseif action == "watch.remove" then
        response.data = Watch.remove(cmd.address)
        
    elseif action == "watch.list" then
        response.data = Watch.list()
        
    elseif action == "watch.clear" then
        response.data = Watch.clear()
        
    -- Freeze/Lock commands (for cheats)
    elseif action == "freeze.add" then
        response.data = Freeze.add(cmd.address, cmd.value, cmd.label, cmd.domain)
        
    elseif action == "freeze.remove" then
        response.data = Freeze.remove(cmd.id, cmd.address, cmd.removeAll)
        
    elseif action == "freeze.list" then
        response.data = Freeze.list()
        
    -- Save state commands
    elseif action == "state.save" then
        if cmd.slot then
            savestate.saveslot(cmd.slot)
            response.data = { saved = true, slot = cmd.slot }
        elseif cmd.path then
            savestate.save(cmd.path)
            response.data = { saved = true, path = cmd.path }
        else
            response.data = { error = "No slot or path specified" }
        end
        
    elseif action == "state.load" then
        if cmd.slot then
            savestate.loadslot(cmd.slot)
            response.data = { loaded = true, slot = cmd.slot }
        elseif cmd.path then
            savestate.load(cmd.path)
            response.data = { loaded = true, path = cmd.path }
        else
            response.data = { error = "No slot or path specified" }
        end
        
    -- Input control
    elseif action == "input.set" then
        local buttons = cmd.buttons
        local player = cmd.player or 1
        local joypad_input = {}

        -- Validate buttons is a table
        if buttons == nil then
            buttons = {}
        elseif type(buttons) ~= "table" then
            response.data = { error = "buttons must be a table/object, got: " .. type(buttons) }
            return response
        end

        -- BizHawk NDS reports touch keys as "Touch", "Touch X", "Touch Y"
        -- (with literal spaces) — not "TouchX"/"TouchY". Touch is a
        -- digital boolean (joypad.set). Touch X / Touch Y are NUMERIC
        -- and only respond to joypad.setanalog. Wrong API or wrong name
        -- gets silently dropped, which caused the long-standing
        -- "Touch=true fires but coords ignored" symptom.
        local analog_input = {}
        for btn, val in pairs(buttons) do
            if btn == "Touch" then
                joypad_input["Touch"] = val
            elseif btn == "TouchX" or btn == "Touch X" then
                analog_input["Touch X"] = val
            elseif btn == "TouchY" or btn == "Touch Y" then
                analog_input["Touch Y"] = val
            elseif val == true then
                joypad_input["P" .. player .. " " .. btn] = true
            elseif type(val) == "number" then
                analog_input["P" .. player .. " " .. btn] = val
            end
        end

        joypad.set(joypad_input)
        if next(analog_input) ~= nil then
            joypad.setanalog(analog_input)
        end
        response.data = { input = true, buttons = buttons }

    -- ───────────────────────────── NDS-specific actions ─────────────────────────────
    elseif action == "input.touch" then
        -- Touch the bottom screen at (x, y) for `frames` frames, then release.
        -- KEY FACTS verified via input.debug on MelonDS NDS core:
        --   - Touch keys live in joypad.get() (no player arg), not get(1).
        --   - "Touch" is a digital boolean — settable via joypad.set.
        --   - "Touch X" / "Touch Y" (with spaces) are NUMERIC controls
        --     settable via joypad.setanalog. joypad.set ignores numeric
        --     values for them, leaving the coords stuck at the last
        --     value (often 255/0 — top-right corner) so the tap fires
        --     wherever was last set.
        --   - Pass no player arg (or omit explicitly) so it matches the
        --     player-nil schema where the NDS controls live.
        local x = cmd.x or 0
        local y = cmd.y or 0
        local frames = cmd.frames or 8
        isPausedState = false
        currentState = STATE_RUNNING
        -- joypad.set with full-state override: get the current input
        -- table, modify Touch / Touch X / Touch Y, then call joypad.set
        -- with the FULL table. This bypasses the XorAdapter that breaks
        -- joypad.setanalog whenever a mouse binding still exists on the
        -- touch axes (BizHawk XORs sticky autoholds against regular
        -- inputs, forcing the axis to neutral if both are non-neutral).
        -- joypad.set is a per-frame OVERRIDE, not a sticky autohold, so
        -- the XOR doesn't apply.
        local post_x, post_y, post_touch
        for _ = 1, frames do
            if client.unpause then client.unpause() end
            local ds = joypad.get() or {}
            ds["Touch"] = true
            ds["Touch X"] = x
            ds["Touch Y"] = y
            joypad.set(ds)
            -- Also push through setanalog explicitly — some BizHawk
            -- builds only honor numeric touch values via this API even
            -- when joypad.set has them in the table.
            joypad.setanalog({ ["Touch X"] = x, ["Touch Y"] = y })
            local check = joypad.get() or {}
            post_x = check["Touch X"]
            post_y = check["Touch Y"]
            post_touch = check["Touch"]
            emu.frameadvance()
        end
        if client.unpause then client.unpause() end
        local ds = joypad.get() or {}
        ds["Touch"] = false
        joypad.set(ds)
        emu.frameadvance()
        response.data = {
            touched = true, x = x, y = y, frames = frames,
            post_x = post_x, post_y = post_y, post_touch = post_touch,
        }

    elseif action == "input.touch_release" then
        local ds = joypad.get() or {}
        ds["Touch"] = false
        joypad.set(ds)
        response.data = { released = true }

    elseif action == "input.debug" then
        -- Dump joypad immutable + mutable state so we can see the actual
        -- button/analog names BizHawk exposes for the current core.
        local digital_p1 = joypad.get(1) or {}
        local digital_nil = joypad.get() or {}
        local analog_p1 = joypad.getimmediate(1) or {}
        -- Build sortable string lists from these maps.
        local function keyset(t)
            local keys = {}
            for k, v in pairs(t) do
                table.insert(keys, tostring(k) .. "=" .. tostring(v))
            end
            table.sort(keys)
            return keys
        end
        response.data = {
            digital_p1 = keyset(digital_p1),
            digital_nil = keyset(digital_nil),
            analog_p1 = keyset(analog_p1),
            note = "if any TouchX/TouchY-related key appears, that is the canonical name to use",
        }

    elseif action == "input.press" then
        -- Press a single button for N frames then release. Avoids the
        -- nested-object JSON parsing limitation of input.set.
        -- Params: button (string, e.g. "A", "B", "Down"), frames (int, default 8)
        -- Sets BOTH "P1 BTN" and "BTN" key formats since NDS uses no
        -- prefix while other systems use P1.
        local btn = cmd.button or "A"
        local frames = cmd.frames or 8
        local player = cmd.player or 1
        local key1 = "P" .. player .. " " .. btn
        local key2 = btn
        isPausedState = false
        currentState = STATE_RUNNING
        for _ = 1, frames do
            -- Unpause EVERY frame. See input.touch comment for why —
            -- mid-loop pauses cause the button to be held while BizHawk
            -- catches up on unpause, producing stuck-input symptoms.
            if client.unpause then client.unpause() end
            joypad.set({ [key1] = true, [key2] = true })
            emu.frameadvance()
        end
        if client.unpause then client.unpause() end
        joypad.set({ [key1] = false, [key2] = false })
        emu.frameadvance()
        response.data = { pressed = btn, frames = frames }

    elseif action == "system.reset" then
        -- Hard reset / power cycle the emulator core.
        -- client.reboot_core() can leave the emulator paused; explicitly
        -- unpause and sync our isPausedState so the main loop resumes
        -- frame advancing.  Without this, the script's main loop would
        -- yield indefinitely (the cause of the "stale bridge" symptom).
        client.reboot_core()
        if client.unpause then client.unpause() end
        isPausedState = false
        currentState = STATE_RUNNING
        -- Explicitly release every known NDS button. joypad.set({}) is
        -- a NO-OP in BizHawk, so we have to list every button to clear
        -- any leftover Lua-side input state from prior commands.
        joypad.set({
            ["P1 A"] = false, ["P1 B"] = false,
            ["P1 X"] = false, ["P1 Y"] = false,
            ["P1 L"] = false, ["P1 R"] = false,
            ["P1 Up"] = false, ["P1 Down"] = false,
            ["P1 Left"] = false, ["P1 Right"] = false,
            ["P1 Start"] = false, ["P1 Select"] = false,
            ["A"] = false, ["B"] = false,
            ["X"] = false, ["Y"] = false,
            ["L"] = false, ["R"] = false,
            ["Up"] = false, ["Down"] = false,
            ["Left"] = false, ["Right"] = false,
            ["Start"] = false, ["Select"] = false,
            ["Touch"] = false,
        })
        -- Clear analog Touch X/Y too (different API).
        joypad.setanalog({ ["Touch X"] = "", ["Touch Y"] = "" })
        emu.frameadvance()
        response.data = { reset = true }

    elseif action == "system.close_rom" then
        -- Close the currently loaded ROM. After this, no ROM is loaded;
        -- emulator state is whatever BizHawk shows when no ROM is open.
        -- Useful before swapping ROM files (so the file handle isn't
        -- held by BizHawk and the patcher can overwrite it).
        local ok, err = pcall(client.closerom)
        if ok then
            isPausedState = false
            currentState = STATE_RUNNING
            response.data = { closed = true }
        else
            response.success = false
            response.data = { error = tostring(err) }
        end

    elseif action == "system.open_rom" then
        -- Load a ROM file. `path` is the absolute path to the ROM.
        -- After loading, BizHawk may pause briefly; we unpause and
        -- clear joypad state so navigation works immediately.
        local rompath = cmd.path
        if not rompath then
            response.success = false
            response.data = { error = "missing 'path' param" }
        else
            local ok, err = pcall(function() client.openrom(rompath) end)
            if ok then
                if client.unpause then client.unpause() end
                isPausedState = false
                currentState = STATE_RUNNING
                joypad.set({
                    ["P1 A"] = false, ["P1 B"] = false,
                    ["P1 X"] = false, ["P1 Y"] = false,
                    ["P1 L"] = false, ["P1 R"] = false,
                    ["P1 Up"] = false, ["P1 Down"] = false,
                    ["P1 Left"] = false, ["P1 Right"] = false,
                    ["P1 Start"] = false, ["P1 Select"] = false,
                    ["A"] = false, ["B"] = false,
                    ["X"] = false, ["Y"] = false,
                    ["L"] = false, ["R"] = false,
                    ["Up"] = false, ["Down"] = false,
                    ["Left"] = false, ["Right"] = false,
                    ["Start"] = false, ["Select"] = false,
                    ["Touch"] = false,
                })
                emu.frameadvance()
                response.data = { opened = true, path = rompath }
            else
                response.success = false
                response.data = { error = tostring(err) }
            end
        end

    elseif action == "frame.advance" then
        local n = cmd.frames or 1
        for _ = 1, n do
            emu.frameadvance()
        end
        response.data = { advanced = n, frameCount = emu.framecount() }

    elseif action == "memory.dump_to_file" then
        -- Read `length` bytes from `address` in `domain` and write the raw
        -- bytes to `path`.  Avoids transferring large memory regions through
        -- the JSON/MCP layer.
        local addr = parseAddress(cmd.address or 0)
        local length = cmd.length or 0
        local path = cmd.path
        local domain = cmd.domain or DEFAULT_SYSTEM_DOMAIN
        if not path or length <= 0 then
            response.data = { error = "memory.dump_to_file requires {address, length, path}" }
            return response
        end
        local prev = memory.getcurrentmemorydomain()
        setMemoryDomain(domain)
        local f = io.open(path, "wb")
        if not f then
            setMemoryDomain(prev)
            response.data = { error = "cannot open " .. tostring(path) }
            return response
        end
        for i = 0, length - 1 do
            f:write(string.char(memory.readbyte(addr + i)))
        end
        f:close()
        setMemoryDomain(prev)
        response.data = { saved = true, path = path, bytes = length,
                          address = string.format("0x%08X", addr) }

    elseif action == "screenshot.take" then
        local path = cmd.path
        if not path or path == "" then
            response.data = { error = "screenshot.take requires `path`" }
            return response
        end
        local ok, err = pcall(function() client.screenshot(path) end)
        response.data = ok and { saved = true, path = path }
                            or  { error = "screenshot failed: " .. tostring(err) }

    elseif action == "powcnt.read" then
        local prev = memory.getcurrentmemorydomain()
        setMemoryDomain(DEFAULT_SYSTEM_DOMAIN)
        local v = memory.read_u16_le(0x04000304)
        setMemoryDomain(prev)
        local swap = (v >= 0x8000)
        response.data = {
            powcnt1 = v,
            hex = string.format("0x%04X", v),
            lcd_swap = swap,
            engine_a_screen = swap and "bottom" or "top",
            engine_b_screen = swap and "top" or "bottom",
            engine_a_enabled = (v % 4) >= 2,
        }

    elseif action == "dispcnt.decode" then
        local prev = memory.getcurrentmemorydomain()
        setMemoryDomain(DEFAULT_SYSTEM_DOMAIN)
        local engine = cmd.engine or "main"
        local base = (engine == "sub") and 0x04001000 or 0x04000000
        local dc = memory.read_u32_le(base)
        setMemoryDomain(prev)
        local bg_on = {}
        for i = 0, 3 do
            bg_on[i] = (math.floor(dc / 2^(8 + i)) % 2) == 1
        end
        response.data = {
            engine = engine,
            dispcnt = dc,
            hex = string.format("0x%08X", dc),
            mode = dc % 8,
            obj_1d_mapping = ((math.floor(dc / 16) % 2) == 1),
            obj_boundary = (math.floor(dc / 2^20) % 8),
            obj_enabled = ((math.floor(dc / 4096) % 2) == 1),
            bg0_enabled = bg_on[0], bg1_enabled = bg_on[1],
            bg2_enabled = bg_on[2], bg3_enabled = bg_on[3],
            display_mode = (math.floor(dc / 2^16) % 4),
        }

    elseif action == "vramcnt.decode" then
        local prev = memory.getcurrentmemorydomain()
        setMemoryDomain(DEFAULT_SYSTEM_DOMAIN)
        local banks = {}
        for i = 0, 8 do
            local v = memory.readbyte(0x04000240 + i)
            banks[string.char(65 + i)] = {
                value = v, hex = string.format("0x%02X", v),
                enable = (v >= 0x80),
                mst = (v % 8),
                offset = (math.floor(v / 8) % 4),
            }
        end
        setMemoryDomain(prev)
        response.data = banks

    elseif action == "bg.read" then
        -- Read one BG layer's CNT register + 24x32 tilemap entries.
        local engine = cmd.engine or "main"
        local idx = cmd.index or 0
        local cnt_base = (engine == "sub") and 0x04001008 or 0x04000008
        local vram_base = (engine == "sub") and 0x06200000 or 0x06000000
        local prev = memory.getcurrentmemorydomain()
        setMemoryDomain(DEFAULT_SYSTEM_DOMAIN)
        local cnt = memory.read_u16_le(cnt_base + idx * 2)
        local screen_base = math.floor(cnt / 256) % 32
        local char_base = math.floor(cnt / 4) % 16
        local map_addr = vram_base + screen_base * 0x800
        local tile_addr = vram_base + char_base * 0x4000
        local rows = {}
        for r = 0, 23 do
            local row = {}
            for c = 0, 31 do
                row[c + 1] = memory.read_u16_le(map_addr + (r * 32 + c) * 2)
            end
            rows[r + 1] = row
        end
        setMemoryDomain(prev)
        response.data = {
            engine = engine, index = idx,
            cnt = cnt, hex = string.format("0x%04X", cnt),
            priority = (cnt % 4),
            colors_256 = ((math.floor(cnt / 128) % 2) == 1),
            char_base = char_base,
            screen_base = screen_base,
            map_addr = string.format("0x%08X", map_addr),
            tile_addr = string.format("0x%08X", tile_addr),
            tilemap = rows,
        }

    elseif action == "oam.parse" then
        -- Decode visible sprites from MAIN or SUB OAM into a list.
        local engine = cmd.engine or "main"
        local base = (engine == "sub") and 0x07000400 or 0x07000000
        local SIZE = {
            [0] = { [0]={1,1},[1]={2,2},[2]={4,4},[3]={8,8} },
            [1] = { [0]={2,1},[1]={4,1},[2]={4,2},[3]={8,4} },
            [2] = { [0]={1,2},[1]={1,4},[2]={2,4},[3]={4,8} },
        }
        local prev = memory.getcurrentmemorydomain()
        setMemoryDomain(DEFAULT_SYSTEM_DOMAIN)
        local sprites = {}
        for i = 0, 127 do
            local a0 = memory.read_u16_le(base + i * 8)
            local a1 = memory.read_u16_le(base + i * 8 + 2)
            local a2 = memory.read_u16_le(base + i * 8 + 4)
            local y = a0 % 256
            local rot = (math.floor(a0 / 256) % 2) == 1
            local dis = (math.floor(a0 / 512) % 2) == 1
            local shape = math.floor(a0 / 16384) % 4
            local x = a1 % 512
            local size = math.floor(a1 / 16384) % 4
            local tile = a2 % 1024
            local pri = math.floor(a2 / 1024) % 4
            local pal = math.floor(a2 / 4096) % 16
            if (not (not rot and dis)) and SIZE[shape] and SIZE[shape][size] then
                local wh = SIZE[shape][size]
                table.insert(sprites, {
                    idx = i, y = y, x = x, tile = tile,
                    w_tiles = wh[1], h_tiles = wh[2],
                    priority = pri, palette = pal,
                    rotated = rot,
                })
            end
        end
        setMemoryDomain(prev)
        response.data = { engine = engine, sprites = sprites }
        
    -- Legacy commands
    elseif action == "search" then
        response.data = Memory.search(cmd.value)
        
    elseif action == "snapshot" then
        response.data = Memory.snapshot(cmd.name)
        
    elseif action == "compare" then
        response.data = Memory.compare(cmd.name, "changed")
        
    elseif action == "write" then
        Memory.write(cmd.address, cmd.value)
        response.data = { written = true }
        
    elseif action == "addWatch" then
        response.data = Watch.add(cmd.address)
        
    elseif action == "removeWatch" then
        response.data = Watch.remove(cmd.address)
        
    else
        response.success = false
        response.error = "Unknown action: " .. tostring(action)
    end
    
    return response
end

-- ═══════════════════════════════════════════════════════════════
-- STATE UPDATE
-- ═══════════════════════════════════════════════════════════════

-- True only when a real core/ROM is loaded. With no ROM, BizHawk runs the
-- "NULL" core and most emu.* calls (frameadvance, getregisters, framecount)
-- error or hang, so callers must guard on this.
local function romLoaded()
    local ok, sid = pcall(emu.getsystemid)
    return ok and sid ~= nil and sid ~= "NULL"
end

-- Write the heartbeat/state file. Every field is read defensively so this
-- ALWAYS reaches writeFile() — even with no ROM loaded. If it could throw,
-- the timestamp would stop advancing and MCP would treat the bridge as stale
-- even though the script is still alive.
local function updateState()
    local function safe(fn, default)
        local ok, v = pcall(fn)
        if ok and v ~= nil then return v end
        return default
    end
    local loaded = romLoaded()
    local state = {
        -- State
        state = currentState,
        isPaused = isPausedState,
        romLoaded = loaded,
        -- Game info (only read core-dependent values when a ROM is loaded)
        frameCount = loaded and safe(emu.framecount, 0) or 0,
        cpu = loaded and safe(CPU.getRegisters, {}) or {},
        watched = safe(Watch.list, {}),
        breakpoints = safe(function() return #Breakpoints.list() end, 0),
        tracing = traceEnabled,
        timestamp = os.time(),
        -- BizHawk specific
        emulator = "BizHawk",
        core = safe(emu.getsystemid, "NULL")
    }
    pcall(function() writeFile(STATE_FILE, toJson(state)) end)
end

-- ═══════════════════════════════════════════════════════════════
-- COMMAND CHECK (Socket + File-based)
-- ═══════════════════════════════════════════════════════════════

-- Check for socket commands using LuaSocket (fast path)
local function checkSocketCommands()
    if not SOCKET_ENABLED or not socketInitialized then return false end

    -- Accept new connection if no current client
    if not socketClient then
        local client, err = socketServer:accept()
        if client then
            client:settimeout(0)  -- Non-blocking
            socketClient = client
            console.log("[SOCKET] Client connected!")
        end
        return false
    end

    -- Try to receive data from client
    local data, err = socketClient:receive("*l")  -- Read line
    if data then
        local cmd = parseJson(data)
        if cmd and cmd.id then
            lastCommandId = cmd.id
            log("[SOCKET] Command: " .. tostring(cmd.action) .. " (id=" .. tostring(cmd.id) .. ")")

            -- pcall so a single command's runtime error doesn't kill the
            -- bridge (and the main loop) and leave BizHawk stale.
            local ok, response = pcall(processCommand, cmd)
            if not ok then
                console.log("[SOCKET] Command error: " .. tostring(response))
                response = { success = false, error = tostring(response) }
            end
            response.commandId = cmd.id
            
            -- Send response back via socket
            local jsonResponse = toJson(response) .. "\n"
            socketClient:send(jsonResponse)
            log("[SOCKET] Response sent for command " .. tostring(cmd.id))
            return true
        end
    elseif err == "closed" then
        console.log("[SOCKET] Client disconnected")
        socketClient:close()
        socketClient = nil
    end
    -- "timeout" is normal for non-blocking, just means no data
    
    return false
end



-- Check for file-based commands (fallback)
local function checkFileCommands()
    local content = readFile(COMMAND_FILE)
    if not content or content == "" then return end

    local cmd = parseJson(content)
    if not cmd or not cmd.id or cmd.id <= lastCommandId then return end

    lastCommandId = cmd.id
    log("[FILE] Processing command: " .. tostring(cmd.action) .. " (id=" .. tostring(cmd.id) .. ")")

    -- pcall so a single command's runtime error doesn't kill the bridge.
    local ok, response = pcall(processCommand, cmd)
    if not ok then
        console.log("[FILE] Command error: " .. tostring(response))
        response = { success = false, error = tostring(response) }
    end
    response.commandId = cmd.id

    writeFile(RESPONSE_FILE, toJson(response))
    log("[FILE] Response written for command " .. tostring(cmd.id))
end

-- Combined command check - tries socket first, then file
local function checkCommands()
    if not checkSocketCommands() then
        checkFileCommands()
    end
end


-- ═══════════════════════════════════════════════════════════════
-- INITIALIZATION
-- ═══════════════════════════════════════════════════════════════

console.log("╔═══════════════════════════════════════════╗")
console.log("║  BizHawk Debug API for AI  [FULL CONTROL] ║")
console.log("╠═══════════════════════════════════════════╣")
console.log("║  APIs: Memory, CPU, Execution,            ║")
console.log("║        Breakpoints, Disasm, Trace         ║")
console.log("║  + NO EXECUTION TIME LIMITS               ║")
console.log("║  + FULL AI CONTROL (pause/resume/step)    ║")
console.log("║  + SOCKET COMMUNICATION (FAST!)           ║")
console.log("╚═══════════════════════════════════════════╝")
console.log("")
console.log("Files:")
console.log("  State: " .. STATE_FILE)
console.log("  Commands: " .. COMMAND_FILE)
console.log("")

-- Initialize socket server
if SOCKET_ENABLED then
    console.log("Socket Mode: ENABLED (TCP server on port " .. SOCKET_PORT .. ")")
    initSocketServer()
else
    console.log("Socket Mode: DISABLED (file-based only)")
end
console.log("")



-- Create initial files
writeFile(COMMAND_FILE, '{"id":0}')

-- Initial state update
updateState()

console.log("BizHawk Debug API Ready!")
do
    -- Guarded: with no ROM loaded these can error; the script must still
    -- reach the main loop so it can service an open_rom command.
    local ok, sid = pcall(emu.getsystemid)
    console.log("System: " .. (ok and tostring(sid) or "NULL (no ROM loaded)"))
end
console.log("")


-- List memory domains for reference (only meaningful with a core loaded)
do
    local ok, domains = pcall(memory.getmemorydomainlist)
    if ok and domains then
        console.log("Available Memory Domains:")
        for i, domain in ipairs(domains) do
            console.log("  " .. i .. ". " .. domain)
        end
    else
        console.log("Memory domains unavailable (no ROM loaded yet)")
    end
end
console.log("")

-- ═══════════════════════════════════════════════════════════════
-- MAIN LOOP - BizHawk runs this continuously
-- ═══════════════════════════════════════════════════════════════

while true do
    frameCounter = frameCounter + 1

    -- Every step below is pcall-guarded so a transient error (most often a
    -- core-dependent read issued while no ROM is loaded) can never escape and
    -- kill this loop. If the loop dies the heartbeat stops and MCP reports a
    -- stale bridge — the exact failure we are guarding against.
    pcall(checkCommands)

    -- Heartbeat / state (every 10 frames). updateState is self-guarding and
    -- always writes a fresh timestamp, even with no ROM loaded.
    if frameCounter % 10 == 0 then
        pcall(updateState)
    end

    if traceEnabled then
        pcall(Trace.logInstruction)
    end

    -- Apply frozen values (maintain cheats); no-op when none are set.
    pcall(Freeze.apply)

    -- Decide whether to advance a frame. emu.frameadvance() must NOT run when
    -- no ROM is loaded (it errors/hangs on the NULL core) or when the
    -- emulator is paused. In those cases emu.yield() hands control back to
    -- BizHawk (GUI stays responsive, script stays alive) without advancing.
    -- Exactly one of frameadvance/yield runs each iteration, so the script
    -- always returns time to the emulator and never spins.
    local advance = romLoaded()
    if advance and isPausedState then advance = false end
    if advance and client.ispaused then
        local ok, p = pcall(client.ispaused)
        if ok and p then advance = false end
    end
    if advance then
        if not pcall(emu.frameadvance) then
            emu.yield()  -- frameadvance failed unexpectedly; still yield
        end
    else
        emu.yield()
    end
end
