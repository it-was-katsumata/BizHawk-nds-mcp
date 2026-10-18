--[[
  BizHawk/EmuHawk Debug API for AI — NDS adaptation
  Comprehensive debugging interface for AI agents.

  Originally written for NES; this fork adapts it for Nintendo DS via
  the MelonDS core in BizHawk 2.10+.

  Memory domains exposed by MelonDS in BizHawk 2.10 / 2.11.x (from the
  core's GetMemoryAreas):
    Main RAM (4 MB, offset 0 = bus 0x02000000), Shared WRAM, ARM7 WRAM,
    SRAM, ROM, Instruction TCM, Data TCM, ARM9 BIOS, ARM7 BIOS, Firmware,
    ARM9 System Bus, ARM7 System Bus
  There are no separate palette / VRAM / OAM domains. Use ARM9 System Bus
  (the default here) to address by physical DS address: 0x02000000 main
  RAM, 0x04000000 I/O, 0x05000000 palette, 0x06xxxxxx VRAM, 0x07000000 OAM.
  Note memory.getmemorydomainlist() is ZERO-indexed, so ipairs() over it
  skips "Main RAM".

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
  - CPU state access (ARM9 / ARM7 r0-r15 as exposed by MelonDS; BizHawk
    does not expose CPSR, and MelonDS cannot single-step instructions)
  - Execution control (pause, frame step, resume)
  - Memory breakpoints with callbacks (need MelonDS JIT disabled)
  - Execution trace via exec callbacks (CPU-intensive, bounded)
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
local SERVER_INFO_FILE = BASE_PATH .. "debug_server.json"  -- written by the MCP server

-- NDS main RAM is 4 MB (0x02000000..0x023FFFFF). Memory.search and
-- Memory.snapshot sweep all of it by default on ARM9 System Bus, so the
-- addresses they report are bus addresses usable with breakpoints and
-- memory.read. Bulk reads go through readBlock() (one host call per
-- 64 KB), so a full sweep takes well under a second.
local RAM_SIZE = 0x400000          -- 4 MB Main RAM (full)
local MAIN_RAM_BASE = 0x02000000
local MAX_RESULTS = 1000           -- cap on search/compare entries returned
local MAX_RANGE = 0x10000          -- cap on memory.readRange length

-- ARM9 System Bus gives the ARM9's full memory map (see header). It is
-- also the only scope MelonDS offers for memory callbacks.
local DEFAULT_SYSTEM_DOMAIN = "ARM9 System Bus"

-- ═══════════════════════════════════════════════════════════════
-- SOCKET CONFIGURATION (BizHawk's built-in comm.socketServer* client)
-- ═══════════════════════════════════════════════════════════════
--
-- EmuHawk's Lua can only open an OUTBOUND socket, so the Python MCP server
-- listens and this script is the client. The client object exists only when
-- EmuHawk was started with --socket_ip=127.0.0.1 --socket_port=9876, and
-- EmuHawk connects during startup (it shows a fatal error if nothing is
-- listening yet), so start the MCP server first. Without the flags
-- everything goes through the JSON files, as before.
--
-- Protocol (BizHawk 2.6.2+ frames each message as "<byte length> <payload>"):
-- every loop iteration this script sends READY or "RESULT <json>", and the
-- server answers each message at once with NONE or a command. The answer
-- is read at the start of the NEXT iteration, by which time it is already
-- buffered, so receiving does not stall emulation.
--
-- BizHawk 2.11.1 quirks this relies on (SocketServer.cs):
--   - socketServerIsConnected() only reports that a connect once succeeded,
--     so a dead peer is detected from a failed send or a missing answer.
--   - Every (re)connect makes a new socket with an infinite receive
--     timeout, so the timeout is set again after each connect.
--   - ReceiveString spins forever if a read returns end-of-stream while it
--     reads a length prefix. The Python side therefore always aborts with
--     RST (SO_LINGER 0), so the first read after a reset fails cleanly. But
--     later reads on a reset socket can return end-of-stream (Linux), so
--     this script never calls socketServerResponse twice without a
--     successful send in between: a send on a dead connection fails, and
--     the connection is then dropped before anything reads from it again.

local SOCKET_RECEIVE_TIMEOUT_MS = 50     -- only waited for when an answer is missing
local SOCKET_MAX_MISSES = 3              -- missing answers in a row before giving up
local SOCKET_RECHECK_SECONDS = 2         -- how often to look for a (new) MCP server
local SOCKET_RETRY_AFTER_FAIL = 10       -- back-off after a failed connect
local SERVER_INFO_MAX_AGE = 5            -- seconds; the server refreshes it every second

-- Optional LuaSocket, used only for sub-second wall-clock time in the
-- input.sequence `sleep` step. BizHawk doesn't bundle it; without it,
-- `sleep` counts 60 frames per second.
local luasocket = nil
pcall(function() luasocket = require("socket") end)



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

-- JSON serializer. Builds into a buffer (string .. in a loop is quadratic,
-- which made large compare/trace responses crawl).
local function jsonString(s)
    return '"' .. s:gsub('[%c"\\]', function(c)
        if c == '"' then return '\\"' end
        if c == '\\' then return '\\\\' end
        if c == '\n' then return '\\n' end
        if c == '\r' then return '\\r' end
        if c == '\t' then return '\\t' end
        return string.format('\\u%04x', c:byte())
    end) .. '"'
end

local function encodeJson(v, buf)
    local t = type(v)
    if t == "string" then
        buf[#buf + 1] = jsonString(v)
    elseif t == "boolean" then
        buf[#buf + 1] = v and "true" or "false"
    elseif t == "number" then
        -- NaN / inf are not valid JSON
        buf[#buf + 1] = (v ~= v or v == math.huge or v == -math.huge) and "null" or tostring(v)
    elseif t ~= "table" then
        buf[#buf + 1] = "null"
    else
        local n, isArray = 0, true
        for k in pairs(v) do
            n = n + 1
            if math.type(k) ~= "integer" or k < 1 then isArray = false; break end
        end
        if isArray and n ~= #v then isArray = false end
        if isArray then
            buf[#buf + 1] = "["
            for i = 1, n do
                if i > 1 then buf[#buf + 1] = "," end
                encodeJson(v[i], buf)
            end
            buf[#buf + 1] = "]"
        else
            buf[#buf + 1] = "{"
            local first = true
            for k, val in pairs(v) do
                if type(val) ~= "function" then
                    if not first then buf[#buf + 1] = "," end
                    buf[#buf + 1] = jsonString(tostring(k))
                    buf[#buf + 1] = ":"
                    encodeJson(val, buf)
                    first = false
                end
            end
            buf[#buf + 1] = "}"
        end
    end
end

local function toJson(v)
    local buf = {}
    encodeJson(v, buf)
    return table.concat(buf)
end

-- JSON parser. A real recursive-descent decoder: the previous pattern-based
-- one could not handle nested objects/arrays, commas or braces inside
-- strings, escape sequences (Windows paths, \uXXXX for non-ASCII), and
-- turned numeric-looking strings into numbers. JSON null becomes nil.
local function parseJson(str)
    if type(str) ~= "string" then return nil end
    local pos = 1

    local function skip()
        pos = str:find("[^ \t\r\n]", pos) or #str + 1
    end

    local function fail(msg)
        error("JSON parse error at " .. pos .. ": " .. msg, 0)
    end

    local value

    local function parseString()
        pos = pos + 1  -- opening quote
        local buf = {}
        while true do
            local s, e = str:find('["\\]', pos)
            if not s then fail("unterminated string") end
            buf[#buf + 1] = str:sub(pos, s - 1)
            if str:sub(s, s) == '"' then
                pos = s + 1
                return table.concat(buf)
            end
            local esc = str:sub(s + 1, s + 1)
            local simple = ({ ['"'] = '"', ['\\'] = '\\', ['/'] = '/', b = '\b',
                              f = '\f', n = '\n', r = '\r', t = '\t' })[esc]
            if simple then
                buf[#buf + 1] = simple
                pos = s + 2
            elseif esc == "u" then
                local cp = tonumber(str:sub(s + 2, s + 5), 16)
                if not cp then fail("bad \\u escape") end
                pos = s + 6
                if cp >= 0xD800 and cp <= 0xDBFF and str:sub(pos, pos + 1) == "\\u" then
                    local lo = tonumber(str:sub(pos + 2, pos + 5), 16)
                    if lo and lo >= 0xDC00 and lo <= 0xDFFF then
                        cp = 0x10000 + (cp - 0xD800) * 0x400 + (lo - 0xDC00)
                        pos = pos + 6
                    end
                end
                buf[#buf + 1] = utf8.char(cp)
            else
                fail("bad escape")
            end
        end
    end

    function value()
        skip()
        local c = str:sub(pos, pos)
        if c == "{" then
            pos = pos + 1
            local obj = {}
            skip()
            if str:sub(pos, pos) == "}" then pos = pos + 1; return obj end
            while true do
                skip()
                if str:sub(pos, pos) ~= '"' then fail("expected key") end
                local k = parseString()
                skip()
                if str:sub(pos, pos) ~= ":" then fail("expected ':'") end
                pos = pos + 1
                obj[k] = value()
                skip()
                local d = str:sub(pos, pos)
                pos = pos + 1
                if d == "}" then return obj end
                if d ~= "," then fail("expected ',' or '}'") end
            end
        elseif c == "[" then
            pos = pos + 1
            local arr, n = {}, 0
            skip()
            if str:sub(pos, pos) == "]" then pos = pos + 1; return arr end
            while true do
                n = n + 1
                arr[n] = value()
                skip()
                local d = str:sub(pos, pos)
                pos = pos + 1
                if d == "]" then return arr end
                if d ~= "," then fail("expected ',' or ']'") end
            end
        elseif c == '"' then
            return parseString()
        elseif str:sub(pos, pos + 3) == "true" then
            pos = pos + 4; return true
        elseif str:sub(pos, pos + 4) == "false" then
            pos = pos + 5; return false
        elseif str:sub(pos, pos + 3) == "null" then
            pos = pos + 4; return nil
        else
            local num = str:match("^-?%d+%.?%d*[eE]?[-+]?%d*", pos)
            local n = num and tonumber(num)
            if not n then fail("unexpected character") end
            pos = pos + #num
            return math.tointeger(n) or n
        end
    end

    local ok, result = pcall(value)
    if not ok then return nil, result end
    return result
end

-- Parse an address: number, "$hex", "0xhex", decimal digits, or bare hex
-- containing a-f (same rules as normalize_address in the Python server).
-- Returns nil for anything else.
local function parseAddress(addr)
    if type(addr) == "number" then return math.tointeger(addr) end
    if type(addr) ~= "string" then return nil end
    local s = addr:match("^%s*(.-)%s*$")
    local hex = s:match("^%$(%x+)$") or s:match("^0[xX](%x+)$")
    if hex then return tonumber(hex, 16) end
    if s:match("^%d+$") then return tonumber(s) end
    if s:match("^%x+$") then return tonumber(s, 16) end
    return nil
end

-- Format address as hex (NDS addresses are 32-bit)
local function hexAddr(addr)
    return string.format("$%08X", addr or 0)
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

-- getmemorydomainlist() is zero-indexed; return a plain 1-indexed list.
function Memory.getDomains()
    local raw = memory.getmemorydomainlist() or {}
    local list = {}
    local i = raw[0] ~= nil and 0 or 1
    while raw[i] ~= nil do
        list[#list + 1] = raw[i]
        i = i + 1
    end
    return list
end

-- Resolve a domain argument. BizHawk silently falls back to the current
-- domain on an unknown name, which turned typos into reads/writes at the
-- same offset in the wrong domain, so unknown names are an error here.
local function resolveDomain(domain)
    if domain == nil or domain == "" or domain == "null" or domain == DEFAULT_SYSTEM_DOMAIN then
        return DEFAULT_SYSTEM_DOMAIN  -- fast path: Freeze.apply calls this every frame
    end
    for _, d in ipairs(Memory.getDomains()) do
        if d == domain then return domain end
    end
    return nil, "Unknown memory domain '" .. tostring(domain) ..
        "' (see memory.getDomains / emu_debug_list_memory_domains)"
end

-- Read `length` bytes as a binary string, one host call per 64 KB.
local function readBlock(addr, length, domain)
    local parts = {}
    local CHUNK = 0x10000
    for off = 0, length - 1, CHUNK do
        local n = math.min(CHUNK, length - off)
        if memory.read_bytes_as_binary_string then  -- BizHawk 2.11.1+
            parts[#parts + 1] = memory.read_bytes_as_binary_string(addr + off, n, domain)
        else
            local bytes = memory.read_bytes_as_array(addr + off, n, domain)
            for i = 1, n, 4096 do
                parts[#parts + 1] = string.char(table.unpack(bytes, i, math.min(i + 4095, n)))
            end
        end
    end
    return table.concat(parts)
end

-- Multi-byte values are little-endian, 1, 2 or 4 bytes wide. Negative
-- values are encoded as two's complement (-1 with size 2 is 0xFFFF).
local function checkSize(size)
    size = size or 1
    if size ~= 1 and size ~= 2 and size ~= 4 then
        return nil, "Invalid size: must be 1, 2 or 4"
    end
    return size
end

local function packValue(value, size)
    if math.type(value) ~= "integer" then return nil, "Invalid value: must be an integer" end
    local limit = 1 << (8 * size)
    if value < -(limit >> 1) or value >= limit then
        return nil, "Invalid value: out of range for " .. size .. " byte(s)"
    end
    return string.pack("<I" .. size, value & (limit - 1))
end

function Memory.read(addr, domain, size)
    addr = parseAddress(addr)
    if addr == nil then
        return { error = "Invalid address" }
    end
    local err
    size, err = checkSize(size)
    if not size then return { error = err } end
    local d
    d, err = resolveDomain(domain)
    if not d then return { error = err } end
    if size == 1 then return memory.readbyte(addr, d) end
    return (string.unpack("<I" .. size, readBlock(addr, size, d)))
end

function Memory.write(addr, value, domain, size)
    addr = parseAddress(addr)
    if addr == nil then
        return { error = "Invalid address" }
    end
    local err, bytes, d
    size, err = checkSize(size)
    if not size then return { error = err } end
    bytes, err = packValue(value, size)
    if not bytes then return { error = err } end
    d, err = resolveDomain(domain)
    if not d then return { error = err } end
    for k = 1, size do
        memory.writebyte(addr + k - 1, bytes:byte(k), d)
    end
    return { written = true }
end

function Memory.readRange(startAddr, length, domain)
    startAddr = parseAddress(startAddr)

    -- Validate startAddr
    if startAddr == nil then
        return { error = "Invalid address: address parameter is nil or invalid" }
    end

    -- Validate length
    length = length or 16  -- Default to 16 bytes
    if math.type(length) ~= "integer" or length < 1 or length > MAX_RANGE then
        return { error = "Invalid length: must be an integer 1-" .. MAX_RANGE }
    end
    local d, err = resolveDomain(domain)
    if not d then return { error = err } end
    return { readBlock(startAddr, length, d):byte(1, -1) }
end

-- Region options shared by search/snapshot: {domain, start, length}.
-- Defaults to all of main RAM on ARM9 System Bus.
local function resolveRegion(opts)
    opts = opts or {}
    local d, err = resolveDomain(opts.domain)
    if not d then return nil, err end
    local start
    if opts.start == nil then
        start = (d == DEFAULT_SYSTEM_DOMAIN or d == "ARM7 System Bus") and MAIN_RAM_BASE or 0
    else
        start = parseAddress(opts.start)
        if start == nil then return nil, "Invalid start address" end
    end
    local length = opts.length or RAM_SIZE
    if math.type(length) ~= "integer" or length < 1 or length > RAM_SIZE then
        return nil, "Invalid length: must be an integer 1-" .. RAM_SIZE
    end
    return { domain = d, start = start, length = length }
end

-- opts: region fields plus size (1/2/4) and aligned (size > 1 only;
-- default true, since DS code keeps 16/32-bit values aligned).
function Memory.search(value, opts)
    opts = opts or {}
    local size, err = checkSize(opts.size)
    if not size then return { error = err } end
    local needle
    needle, err = packValue(value, size)
    if not needle then return { error = err } end
    local region
    region, err = resolveRegion(opts)
    if not region then return { error = err } end
    local block = readBlock(region.start, region.length, region.domain)
    local aligned = size > 1 and opts.aligned ~= false
    local unsigned = string.unpack("<I" .. size, needle)
    local results, total = {}, 0
    local i = block:find(needle, 1, true)
    while i do
        local addr = region.start + i - 1
        if not aligned or addr % size == 0 then
            total = total + 1
            if total <= MAX_RESULTS then
                results[total] = { address = addr, hex = hexAddr(addr), value = unsigned }
            end
        end
        i = block:find(needle, i + 1, true)
    end
    return results, total
end

function Memory.snapshot(name, opts)
    name = name or "default"
    local region, err = resolveRegion(opts)
    if not region then return { error = err } end
    region.data = readBlock(region.start, region.length, region.domain)
    snapshots[name] = region
    return { name = name, size = region.length, domain = region.domain,
             start = hexAddr(region.start) }
end

-- Snapshots are raw bytes, so one snapshot can be compared at any width.
-- opts: size (1/2/4), signed (compare as signed numbers), aligned (size > 1;
-- default true), addresses (only compare values starting at these),
-- oldValue (keep entries whose old value equals it), minDelta (keep
-- entries with |diff| >= it).
-- Returns (changes capped at MAX_RESULTS, total matches).
function Memory.compare(name, filter, opts)
    name = name or "default"
    filter = filter or "changed"
    opts = opts or {}

    local snapshot = snapshots[name]
    if not snapshot then return { error = "Snapshot not found: " .. tostring(name) } end
    if filter ~= "changed" and filter ~= "increased" and filter ~= "decreased"
        and filter ~= "same" and filter ~= "unchanged" then
        return { error = "Invalid filter: use changed/increased/decreased/same" }
    end
    local size, err = checkSize(opts.size)
    if not size then return { error = err } end

    local current = readBlock(snapshot.start, snapshot.length, snapshot.domain)
    local old = snapshot.data
    local changes, total = {}, 0
    local fmt = (opts.signed and "<i" or "<I") .. size
    local step = (size > 1 and opts.aligned ~= false) and size or 1
    local last = snapshot.length - size + 1  -- last offset where a whole value fits

    -- First 1-based offset >= i whose address is a multiple of `step`
    local function alignUp(i)
        local r = (snapshot.start + i - 1) % step
        return r == 0 and i or i + step - r
    end

    local function consider(i)
        local o = string.unpack(fmt, old, i)
        local c = string.unpack(fmt, current, i)
        local diff = c - o
        local include
        if filter == "changed" then include = (diff ~= 0)
        elseif filter == "increased" then include = (diff > 0)
        elseif filter == "decreased" then include = (diff < 0)
        else include = (diff == 0)
        end
        if include and opts.oldValue ~= nil then include = (o == opts.oldValue) end
        if include and opts.minDelta ~= nil then include = (math.abs(diff) >= opts.minDelta) end
        if include then
            total = total + 1
            if total <= MAX_RESULTS then
                local addr = snapshot.start + i - 1
                changes[total] = { address = addr, hex = hexAddr(addr), old = o, new = c, diff = diff }
            end
        end
    end

    if type(opts.addresses) == "table" then
        for _, a in ipairs(opts.addresses) do
            local addr = parseAddress(a)
            local i = addr and (addr - snapshot.start + 1)
            if i and i >= 1 and i <= last then consider(i) end
        end
    elseif filter == "same" or filter == "unchanged" then
        for i = alignUp(1), last, step do consider(i) end
    else
        -- Skip identical 4 KB pages wholesale; only diff values that have
        -- a byte in a page that changed (including values straddling in
        -- from the previous page).
        local PAGE = 4096
        local nextI = alignUp(1)  -- offsets below this were already considered
        for p = 1, snapshot.length, PAGE do
            local q = math.min(p + PAGE - 1, snapshot.length)
            if old:sub(p, q) ~= current:sub(p, q) then
                local i = math.max(nextI, alignUp(math.max(1, p - size + 1)))
                while i <= q and i <= last do
                    consider(i)
                    i = i + step
                end
                nextI = i
            end
        end
    end
    return changes, total
end

-- ═══════════════════════════════════════════════════════════════
-- CPU STATE API (BizHawk-specific)
-- ═══════════════════════════════════════════════════════════════

local CPU = {}

-- MelonDS exposes "ARM9 r0".."ARM9 r15" and "ARM7 r0".."ARM7 r15" (no CPSR,
-- so no flags or Thumb bit). r15 is the core's pipelined PC, which runs
-- ahead of the executing instruction; exec breakpoints report the exact
-- instruction address instead.
function CPU.getState()
    local regs = emu.getregisters()
    if not regs then
        return { error = "Cannot get CPU state" }
    end
    local state = { frameCount = emu.framecount() }
    for _, n in ipairs({ 9, 7 }) do
        local cpu = {}
        for r = 0, 15 do
            cpu["r" .. r] = regs["ARM" .. n .. " r" .. r] or 0
        end
        cpu.sp, cpu.lr, cpu.pc = cpu.r13, cpu.r14, cpu.r15
        state["arm" .. n] = cpu
    end
    return state
end

function CPU.getRegisters()
    local state = CPU.getState()
    if state.error then return state end
    local out = {}
    for _, k in ipairs({ "arm9", "arm7" }) do
        local h = {}
        for r = 0, 15 do h["r" .. r] = hexAddr(state[k]["r" .. r]) end
        out[k] = h
    end
    return out
end

function CPU.getPC()
    local regs = emu.getregisters()
    if not regs then return 0 end
    return regs["ARM9 r15"] or 0
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

-- Advance n frames, honouring the pause state. EmuHawk only resumes a
-- script blocked in emu.frameadvance() right before it emulates a frame,
-- so calling it while paused hangs the bridge until someone unpauses by
-- hand. Unpause around each frame and restore the pause afterwards.
local function advanceFrames(n)
    local wasPaused = isPausedState
    if client.ispaused then
        local ok, p = pcall(client.ispaused)
        wasPaused = wasPaused or (ok and p)
    end
    for _ = 1, n do
        if wasPaused then client.unpause() end
        emu.frameadvance()
    end
    if wasPaused then
        client.pause()
        isPausedState = true
        currentState = STATE_PAUSED
    end
end

-- STEP - Frame advance
function Execution.step(count, stepType)
    count = count or 1
    stepType = stepType or "frame"
    if math.type(count) ~= "integer" or count < 1 then
        return { error = "Invalid count: must be a positive integer" }
    end
    if stepType ~= "frame" then
        -- MelonDS reports IDebuggable.CanStep == false: there is no
        -- instruction stepping in BizHawk for NDS. (The old "instruction"
        -- mode called emu.yield(), which steps nothing.)
        return { error = "Only stepType='frame' is supported on the MelonDS core" }
    end

    local pcBefore = CPU.getPC()
    advanceFrames(count)

    return {
        stepped = count,
        type = stepType,
        pcBefore = hexAddr(pcBefore),
        newPC = hexAddr(CPU.getPC()),
        frameCount = emu.framecount()
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
    -- A nil address would make BizHawk fire on EVERY access of that type.
    if addr == nil then
        return { error = "Invalid address" }
    end
    local register = ({ read = event.onmemoryread, write = event.onmemorywrite,
                        execute = event.onmemoryexecute })[bpType]
    if not register then
        return { error = "Invalid breakpoint type: " .. tostring(bpType) }
    end

    local id = nextBreakpointId
    nextBreakpointId = nextBreakpointId + 1

    -- Runs inside the core's memory callback: keep it cheap (no file I/O,
    -- no console spam) since hot addresses can fire thousands of times a frame.
    local callback = function(address, value, flags)
        -- For execute breakpoints `address` is the exact instruction address.
        local pc = (bpType == "execute") and address or CPU.getPC()
        local info = {
            breakpointId = id,
            type = bpType,
            address = hexAddr(address),
            value = value,
            PC = hexAddr(pc),
            registers = CPU.getRegisters(),
            timestamp = os.time(),
            frameCount = emu.framecount()
        }

        -- Add to hit queue
        table.insert(breakpointHitQueue, info)
        if #breakpointHitQueue > maxQueueSize then
            table.remove(breakpointHitQueue, 1)
        end

        local bp = breakpoints[id]
        if bp then
            bp.hitCount = (bp.hitCount or 0) + 1
            if bp.hitCount == 1 then
                log("Breakpoint " .. id .. " first hit at PC " .. hexAddr(pc))
            end
        end

        -- Pause if autoPauseMode is enabled (takes effect at the end of
        -- the current frame, not at the instruction)
        if autoPauseMode then
            client.pause()
            isPausedState = true
        end
    end

    -- event.onmemory*(fn, address, name, scope): the 3rd argument is the
    -- callback's name, the 4th the memory scope.
    local ok, eventGuid = pcall(register, callback, addr, "bp" .. id, DEFAULT_SYSTEM_DOMAIN)
    if not ok then
        return { error = "Could not register memory callback (" .. tostring(eventGuid) ..
            "). MelonDS memory callbacks require JIT to be disabled in the NDS sync settings." }
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
-- TRACE LOGGER API
-- ═══════════════════════════════════════════════════════════════

-- Execution trace via event.onmemoryexecuteany: fires before every ARM9
-- instruction, so it is CPU-intensive. Recording stops by itself after
-- `limit` instructions (default traceMaxLines). Requires JIT disabled.
local Trace = {}
local traceGuid = nil

local function stopTraceCallback()
    if traceGuid then
        pcall(event.unregisterbyid, traceGuid)
        traceGuid = nil
    end
    traceEnabled = false
end

function Trace.start(limit)
    limit = limit or traceMaxLines
    if math.type(limit) ~= "integer" or limit < 1 or limit > traceMaxLines then
        return { error = "Invalid limit: must be an integer 1-" .. traceMaxLines }
    end
    stopTraceCallback()
    traceLog = {}
    local ok, guid = pcall(event.onmemoryexecuteany, function(addr)
        local n = #traceLog + 1
        traceLog[n] = { pc = hexAddr(addr) }
        if n >= limit then stopTraceCallback() end
    end, "trace", DEFAULT_SYSTEM_DOMAIN)
    if not ok then
        return { error = "Could not register exec callback (" .. tostring(guid) ..
            "). Tracing requires JIT to be disabled in the NDS sync settings." }
    end
    traceGuid = guid
    traceEnabled = true
    return { tracing = true, limit = limit }
end

function Trace.stop()
    stopTraceCallback()
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

-- Count executed ARM9 instruction addresses over `frames` frames and
-- return the `topN` hottest. Every instruction runs a Lua callback, so
-- emulation slows down drastically while this runs; keep frames small.
function Trace.hotspots(frames, topN)
    frames = frames or 1
    topN = topN or 20
    if math.type(frames) ~= "integer" or frames < 1 or frames > 60 then
        return { error = "Invalid frames: must be an integer 1-60" }
    end
    stopTraceCallback()
    local counts, total = {}, 0
    local ok, guid = pcall(event.onmemoryexecuteany, function(addr)
        counts[addr] = (counts[addr] or 0) + 1
        total = total + 1
    end, "hotspots", DEFAULT_SYSTEM_DOMAIN)
    if not ok then
        return { error = "Could not register exec callback (" .. tostring(guid) ..
            "). Tracing requires JIT to be disabled in the NDS sync settings." }
    end
    local advanced = pcall(advanceFrames, frames)
    pcall(event.unregisterbyid, guid)
    if not advanced then return { error = "frame advance failed while tracing" } end

    local list = {}
    for addr, c in pairs(counts) do list[#list + 1] = { addr = addr, count = c } end
    table.sort(list, function(a, b) return a.count > b.count end)
    local top = {}
    for i = 1, math.min(topN, #list) do
        top[i] = { pc = hexAddr(list[i].addr), count = list[i].count }
    end
    return { framesTraced = frames, instructionsExecuted = total,
             uniqueAddresses = #list, hotspots = top }
end

-- ═══════════════════════════════════════════════════════════════
-- WATCH API
-- ═══════════════════════════════════════════════════════════════

local Watch = {}

function Watch.add(addr, name)
    addr = parseAddress(addr)
    if addr == nil then return { error = "Invalid address" } end
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
    if addr == nil then return { error = "Invalid address" } end
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
    if addr == nil then return { error = "Invalid address" } end
    if math.type(value) ~= "integer" or value < 0 or value > 255 then
        return { error = "Invalid value: must be an integer 0-255" }
    end
    local err
    domain, err = resolveDomain(domain)
    if not domain then return { error = err } end
    
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
        if addr == nil then return { error = "Invalid address" } end
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

local function dispatchCommand(cmd)
    if type(cmd) ~= "table" or not cmd.action then
        return { success = false, error = "Invalid command" }
    end

    local action = cmd.action
    local response = { success = true, action = action }
    local region = { domain = cmd.domain, start = cmd.start, length = cmd.length,
                     size = cmd.size, aligned = cmd.aligned }

    -- Memory commands
    if action == "memory.read" then
        response.data = Memory.read(cmd.address, cmd.domain, cmd.size)

    elseif action == "memory.write" then
        response.data = Memory.write(cmd.address, cmd.value, cmd.domain, cmd.size)

    elseif action == "memory.readRange" then
        response.data = Memory.readRange(cmd.address, cmd.length, cmd.domain)

    elseif action == "memory.search" then
        response.data, response.total = Memory.search(cmd.value, region)

    elseif action == "memory.snapshot" then
        response.data = Memory.snapshot(cmd.name, region)

    elseif action == "memory.compare" then
        response.data, response.total = Memory.compare(cmd.name, cmd.filter, {
            addresses = cmd.addresses, oldValue = cmd.oldValue, minDelta = cmd.minDelta,
            size = cmd.size, signed = cmd.signed, aligned = cmd.aligned })

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
        -- `id` is the command envelope's id; the breakpoint id is `bpId`.
        response.data = Breakpoints.remove(cmd.bpId)
        
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

    -- Trace commands
    elseif action == "trace.start" then
        response.data = Trace.start(cmd.limit)

    elseif action == "trace.hotspots" then
        response.data = Trace.hotspots(cmd.frames, cmd.topN)

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
        -- `id` is the command envelope's id; the freeze id is `freezeId`.
        response.data = Freeze.remove(cmd.freezeId, cmd.address, cmd.removeAll)
        
    elseif action == "freeze.list" then
        response.data = Freeze.list()
        
    -- Save state commands
    -- savestate.* return false on failure (missing slot/file); slots are 1-10.
    elseif action == "state.save" then
        if cmd.slot then
            local ok = savestate.saveslot(cmd.slot)
            response.data = ok == false and { error = "Save to slot " .. tostring(cmd.slot) .. " failed" }
                or { saved = true, slot = cmd.slot }
        elseif cmd.path then
            local ok = savestate.save(cmd.path)
            response.data = ok == false and { error = "Save to " .. tostring(cmd.path) .. " failed" }
                or { saved = true, path = cmd.path }
        else
            response.data = { error = "No slot or path specified" }
        end

    elseif action == "state.load" then
        if cmd.slot then
            local ok = savestate.loadslot(cmd.slot)
            response.data = ok == false and { error = "No savestate in slot " .. tostring(cmd.slot) }
                or { loaded = true, slot = cmd.slot }
        elseif cmd.path then
            local ok = savestate.load(cmd.path)
            response.data = ok == false and { error = "Could not load savestate " .. tostring(cmd.path) }
                or { loaded = true, path = cmd.path }
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
        -- NDS button names carry no "P1 " prefix ("A", "Up", "Touch"); the
        -- prefixed form is kept for other cores, as input.press does.
        local analog_input = {}
        for btn, val in pairs(buttons) do
            if btn == "Touch" then
                joypad_input["Touch"] = val
            elseif btn == "TouchX" or btn == "Touch X" then
                analog_input["Touch X"] = val
            elseif btn == "TouchY" or btn == "Touch Y" then
                analog_input["Touch Y"] = val
            elseif type(val) == "boolean" then
                joypad_input[btn] = val
                joypad_input["P" .. player .. " " .. btn] = val
            elseif type(val) == "number" then
                analog_input[btn] = val
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
        -- setanalog values are sticky autoholds; "" clears them, so later
        -- frames stop XOR-ing a stale coordinate against the mouse.
        joypad.setanalog({ ["Touch X"] = "", ["Touch Y"] = "" })
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

    elseif action == "input.sequence" then
        -- Run several input steps in ONE command, so a whole navigation
        -- sequence costs a single round trip instead of one per button.
        -- `seq` is a FLAT string (this JSON parser is flat-only, no nested
        -- arrays): steps separated by ";", fields by spaces.
        --   press <BTN> [frames]      e.g. "press A 8"
        --   touch <x> <y> [frames]    e.g. "touch 128 96 8"
        --   wait <frames>             e.g. "wait 8"   (emulated frames)
        --   sleep <seconds>           e.g. "sleep 1.5" (60 frames/s; wall-clock with LuaSocket)
        -- Example: seq = "press A; press Right; press Down; press A; touch 128 96"
        -- (omitted frame counts default to 8, matching input.press / input.touch).
        local seq = cmd.seq or ""
        isPausedState = false
        currentState = STATE_RUNNING
        local results = {}
        for stepStr in (seq .. ";"):gmatch("([^;]+);") do
            local tok = {}
            for w in stepStr:gmatch("%S+") do tok[#tok + 1] = w end
            local kind = tok[1]
            if kind == "press" then
                local btn = tok[2] or "A"
                local fr = tonumber(tok[3]) or 8
                local k1, k2 = "P1 " .. btn, btn
                for _ = 1, fr do
                    if client.unpause then client.unpause() end
                    joypad.set({ [k1] = true, [k2] = true })
                    emu.frameadvance()
                end
                if client.unpause then client.unpause() end
                joypad.set({ [k1] = false, [k2] = false })
                emu.frameadvance()
                results[#results + 1] = "press " .. btn .. " x" .. fr
            elseif kind == "touch" then
                local x = tonumber(tok[2]) or 128
                local y = tonumber(tok[3]) or 96
                local fr = tonumber(tok[4]) or 8
                for _ = 1, fr do
                    if client.unpause then client.unpause() end
                    local ds = joypad.get() or {}
                    ds["Touch"] = true; ds["Touch X"] = x; ds["Touch Y"] = y
                    joypad.set(ds)
                    joypad.setanalog({ ["Touch X"] = x, ["Touch Y"] = y })
                    emu.frameadvance()
                end
                if client.unpause then client.unpause() end
                -- release the digital touch (do NOT re-issue setanalog here:
                -- a sticky analog touch gets XOR'd to the mouse position and
                -- would register as a stray touch on later frames).
                joypad.set({ ["Touch"] = false })
                emu.frameadvance()
                results[#results + 1] = "touch " .. x .. "," .. y .. " x" .. fr
            elseif kind == "wait" then
                local fr = tonumber(tok[2]) or 8
                for _ = 1, fr do emu.frameadvance() end
                results[#results + 1] = "wait " .. fr
            elseif kind == "sleep" then
                -- Wall-clock wait: advance frames until N real seconds have
                -- elapsed (socket.gettime() is wall-clock, sub-second). Lets a
                -- sequence be specified in real time; the game keeps running
                -- during the wait. Faster-than-native speed just advances more
                -- frames in the same real time (harmless on a settled screen).
                local secs = tonumber(tok[2]) or 1
                if luasocket and luasocket.gettime then
                    local deadline = luasocket.gettime() + secs
                    while luasocket.gettime() < deadline do
                        emu.frameadvance()
                    end
                else
                    -- LuaSocket unavailable: approximate at 60 fps.
                    for _ = 1, math.floor(secs * 60) do emu.frameadvance() end
                end
                results[#results + 1] = "sleep " .. secs .. "s"
            end
        end
        response.data = { ran = #results, steps = results }

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
        if math.type(n) ~= "integer" or n < 1 then
            response.data = { error = "frames must be a positive integer" }
            return response
        end
        advanceFrames(n)
        response.data = { advanced = n, frameCount = emu.framecount() }

    elseif action == "memory.dump_to_file" then
        -- Read `length` bytes from `address` in `domain` and write the raw
        -- bytes to `path`.  Avoids transferring large memory regions through
        -- the JSON/MCP layer.
        local addr = parseAddress(cmd.address or 0)
        local length = cmd.length or 0
        local path = cmd.path
        local domain, err = resolveDomain(cmd.domain)
        if not path or not addr or math.type(length) ~= "integer" or length <= 0 then
            response.data = { error = "memory.dump_to_file requires {address, length, path}" }
            return response
        end
        if not domain then
            response.data = { error = err }
            return response
        end
        local f = io.open(path, "wb")
        if not f then
            response.data = { error = "cannot open " .. tostring(path) }
            return response
        end
        f:write(readBlock(addr, length, domain))
        f:close()
        response.data = { saved = true, path = path, bytes = length,
                          address = string.format("0x%08X", addr) }

    elseif action == "screenshot.take" then
        local path = cmd.path
        if not path or path == "" then
            response.data = { error = "screenshot.take requires `path`" }
            return response
        end
        -- EmuHawk saves synchronously (PNG unless the path ends in .jpg) and
        -- creates missing directories.
        local ok, err = pcall(function() client.screenshot(path) end)
        response.data = ok and { saved = true, path = path, frameCount = emu.framecount() }
                            or  { error = "screenshot failed: " .. tostring(err) }

    elseif action == "powcnt.read" then
        local prev = memory.getcurrentmemorydomain()
        setMemoryDomain(DEFAULT_SYSTEM_DOMAIN)
        local v = memory.read_u16_le(0x04000304)
        setMemoryDomain(prev)
        -- Bit 15 "Display Swap": 0 = engine A to the lower screen, 1 = upper
        -- (GBATEK; libnds lcdMainOnTop() sets it).
        local swap = (v & 0x8000) ~= 0
        response.data = {
            powcnt1 = v,
            hex = string.format("0x%04X", v),
            lcd_swap = swap,
            engine_a_screen = swap and "top" or "bottom",
            engine_b_screen = swap and "bottom" or "top",
            engine_a_enabled = (v & 0x0002) ~= 0,
            engine_b_enabled = (v & 0x0200) ~= 0,
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
            obj_boundary = (math.floor(dc / 2^20) % 4),  -- bits 20-21
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
            -- VRAMCNT_A..G are 0x240..0x246; 0x247 is WRAMCNT, so H/I
            -- sit one byte later at 0x248/0x249.
            local v = memory.readbyte(0x04000240 + i + (i >= 7 and 1 or 0))
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
        if engine ~= "sub" then
            -- Engine A adds DISPCNT's 64 KB screen base (bits 27-29) and
            -- char base (bits 24-26); engine B has no such fields.
            local dc = memory.read_u32_le(0x04000000)
            map_addr = map_addr + ((dc >> 27) & 7) * 0x10000
            tile_addr = tile_addr + ((dc >> 24) & 7) * 0x10000
        end
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

-- Handlers report failures as { error = ... } in data; lift those so the
-- response says success=false instead of "succeeding" with an error payload.
local function processCommand(cmd)
    local response = dispatchCommand(cmd)
    local data = response.data
    if response.success and type(data) == "table" and data.error ~= nil then
        response.success = false
        response.error = tostring(data.error)
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
        -- also true when paused from the EmuHawk GUI
        isPaused = isPausedState or safe(client.ispaused, false),
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

-- Run one command. pcall so a single command's runtime error doesn't kill
-- the bridge (and the main loop) and leave BizHawk stale.
local function runCommand(cmd, channel)
    lastCommandId = cmd.id
    log("[" .. channel .. "] Command: " .. tostring(cmd.action) .. " (id=" .. tostring(cmd.id) .. ")")
    local ok, response = pcall(processCommand, cmd)
    if not ok then
        console.log("[" .. channel .. "] Command error: " .. tostring(response))
        response = { success = false, error = tostring(response) }
    end
    response.commandId = cmd.id
    return response
end

-- Socket state (see SOCKET CONFIGURATION at the top)
local Sock = {
    available = false,  -- EmuHawk was started with --socket_ip/--socket_port
    active = false,     -- exchanging messages with an MCP server right now
    awaiting = false,   -- an answer to our last message is due
    misses = 0,         -- answers missed in a row
    pending = nil,      -- response to deliver with the next message
    nextCheck = 0,      -- os.time() at which to look for a server again
}

local function sockSend(msg)
    local ok, n = pcall(comm.socketServerSend, msg)
    return ok and type(n) == "number" and n > 0
end

-- Start the READY/answer exchange on the current connection.
local function sockStart()
    Sock.misses = 0
    Sock.pending = nil
    -- Without a receive timeout, one unanswered READY would freeze EmuHawk.
    if not pcall(comm.socketServerSetTimeout, SOCKET_RECEIVE_TIMEOUT_MS) then
        Sock.active = false
        Sock.awaiting = false
        return
    end
    Sock.active = sockSend("READY")
    Sock.awaiting = Sock.active
    if Sock.active then console.log("[SOCKET] Connected to the MCP server") end
end

local function sockDrop(why)
    Sock.active = false
    Sock.awaiting = false
    console.log("[SOCKET] " .. why .. "; using file IPC until the MCP server is back")
end

-- (Re)connect, but only to a server that announced itself recently in
-- debug_server.json: on Windows a connect to a port nobody listens on can
-- block EmuHawk for a couple of seconds.
local function sockReconnect()
    local now = os.time()
    if now < Sock.nextCheck then return end
    Sock.nextCheck = now + SOCKET_RECHECK_SECONDS
    local info = parseJson(readFile(SERVER_INFO_FILE) or "")
    if type(info) ~= "table" or math.type(info.port) ~= "integer"
        or now - (tonumber(info.timestamp) or 0) > SERVER_INFO_MAX_AGE then
        return
    end
    -- Setting the port makes BizHawk open a new connection.
    if pcall(comm.socketServerSetPort, info.port) then sockStart() end
    if not Sock.active then Sock.nextCheck = now + SOCKET_RETRY_AFTER_FAIL end
end

-- Socket fast path. Returns true if a command was run.
local function checkSocketCommands()
    if not Sock.available then return false end
    if not Sock.active then
        sockReconnect()
        return false
    end

    local handled = false
    if Sock.awaiting then
        local ok, msg = pcall(comm.socketServerResponse)
        if not ok or type(msg) ~= "string" or msg == "" then
            -- No answer (timeout or a dead connection). Don't read again
            -- yet: the READY sent below doubles as a probe (it fails on a
            -- dead connection). If the answer was merely late, answers now
            -- run one behind, which the server handles.
            Sock.misses = Sock.misses + 1
            if Sock.misses >= SOCKET_MAX_MISSES then
                sockDrop("MCP server stopped answering")
                return false
            end
            msg = "NONE"
        else
            Sock.misses = 0
        end
        Sock.awaiting = false
        if msg ~= "NONE" then
            local cmd = parseJson(msg)
            if type(cmd) ~= "table" or not cmd.id then
                -- A garbled frame means the stream is out of step; a new
                -- connection starts clean.
                sockDrop("Unreadable message from the MCP server")
                Sock.nextCheck = 0
                return false
            end
            Sock.pending = runCommand(cmd, "SOCKET")
            handled = true
        end
    end

    local out = Sock.pending and ("RESULT " .. toJson(Sock.pending)) or "READY"
    if sockSend(out) then
        Sock.pending = nil
        Sock.awaiting = true
    else
        sockDrop("Lost the connection to the MCP server")
    end
    return handled
end



-- Check for file-based commands (fallback)
local lastFileCommandId = nil
local function checkFileCommands()
    local content = readFile(COMMAND_FILE)
    if not content or content == "" then return end

    local cmd = parseJson(content)
    if not cmd or not cmd.id then return end
    -- Process whenever the id CHANGES, not only when it increases. After the
    -- MCP server restarts its id counter resets to a low value; the old
    -- "id <= lastCommandId" test rejected every post-restart command because
    -- the previous session's id was still higher, hanging the file fallback.
    if cmd.id == lastFileCommandId then return end
    lastFileCommandId = cmd.id

    local response = runCommand(cmd, "FILE")
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
console.log("║        Breakpoints, Trace                 ║")
console.log("║  + NO EXECUTION TIME LIMITS               ║")
console.log("║  + FULL AI CONTROL (pause/resume/step)    ║")
console.log("║  + SOCKET COMMUNICATION (FAST!)           ║")
console.log("╚═══════════════════════════════════════════╝")
console.log("")
console.log("Files:")
console.log("  State: " .. STATE_FILE)
console.log("  Commands: " .. COMMAND_FILE)
console.log("")

-- Socket fast path: only if EmuHawk was started with the socket flags
-- (socketServerGetInfo returns "" otherwise).
do
    local ok, info = false, nil
    if comm and comm.socketServerGetInfo then ok, info = pcall(comm.socketServerGetInfo) end
    Sock.available = ok and type(info) == "string" and info ~= ""
    if Sock.available then
        console.log("Socket Mode: ENABLED (BizHawk comm client -> " .. info .. ")")
        sockStart()
    else
        console.log("Socket Mode: OFF (start EmuHawk with --socket_ip=127.0.0.1 --socket_port=9876 to enable); using file IPC")
    end
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
    local ok, domains = pcall(Memory.getDomains)
    if ok and domains and #domains > 0 then
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
