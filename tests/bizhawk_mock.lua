--[[
  Minimal stand-in for the BizHawk/EmuHawk Lua API (MelonDS core), just
  enough to run src/bizhawk_debug_api.lua under plain Lua 5.4.

  The behaviours that matter were checked against BizHawk's source
  (2.10, 2.11.1 and master):
    - memory.getmemorydomainlist() is ZERO-indexed; "Main RAM" is index 0
    - readbyte/writebyte with an unknown domain log and fall back to the
      current domain instead of raising
    - emu.getregisters() keys are "ARM9 r0".."ARM9 r15", "ARM7 r0".."ARM7 r15"
    - event.onmemory*(fn, address, name, scope): 3rd arg is a NAME
    - a script blocked in emu.frameadvance() is NOT resumed while paused;
      emu.yield() is resumed every host tick regardless
    - joypad.setanalog values are sticky autoholds; "" clears one

  frameadvance/yield are coroutine yields, so Host.tick() drives the script
  one EmuHawk loop iteration at a time.
]]

local Mock = {
    system = "NDS",
    paused = false,
    frame = 0,
    logs = {},
    regs = {},
    callbacks = {},
    nextCallback = 1,
    inCallback = false,
    joypadSet = {},
    analog = {},
    slots = {},
    screenshots = {},
    currentDomain = "Main RAM",
}
for cpu = 0, 1 do
    local n = cpu == 0 and 9 or 7
    for r = 0, 15 do Mock.regs["ARM" .. n .. " r" .. r] = 0 end
end

-- ── memory ───────────────────────────────────────────────────────────
local mainRam = {}
local busOther = {}
local DOMAINS = {
    "Main RAM", "Shared WRAM", "ARM7 WRAM", "SRAM", "ROM",
    "Instruction TCM", "Data TCM", "ARM9 BIOS", "ARM7 BIOS", "Firmware",
    "ARM9 System Bus", "ARM7 System Bus",
}
local DOMAIN_SIZE = { ["Main RAM"] = 0x400000, ["ARM9 System Bus"] = 0x100000000,
                      ["ARM7 System Bus"] = 0x100000000 }
local generic = {}
for _, d in ipairs(DOMAINS) do generic[d] = {} end

local function isDomain(name)
    for _, d in ipairs(DOMAINS) do if d == name then return true end end
    return false
end

local function resolve(domain)
    if domain == nil or domain == "" then return Mock.currentDomain end
    if not isDomain(domain) then
        Mock.logs[#Mock.logs + 1] = "Unable to find domain: " .. tostring(domain) .. ", falling back to current"
        return Mock.currentDomain
    end
    return domain
end

local function peek(domain, addr)
    if domain == "Main RAM" then return mainRam[addr % 0x400000] or 0 end
    if domain == "ARM9 System Bus" or domain == "ARM7 System Bus" then
        if addr >= 0x02000000 and addr < 0x03000000 then
            return mainRam[(addr - 0x02000000) % 0x400000] or 0
        end
        return busOther[addr] or 0
    end
    return generic[domain][addr] or 0
end

local function poke(domain, addr, value)
    value = value & 0xFF
    if domain == "Main RAM" then mainRam[addr % 0x400000] = value; return end
    if domain == "ARM9 System Bus" or domain == "ARM7 System Bus" then
        if addr >= 0x02000000 and addr < 0x03000000 then
            mainRam[(addr - 0x02000000) % 0x400000] = value
        else
            busOther[addr] = value
        end
        return
    end
    generic[domain][addr] = value
end

Mock.peek = function(addr, domain) return peek(domain or "ARM9 System Bus", addr) end
Mock.poke = function(addr, value, domain) poke(domain or "ARM9 System Bus", addr, value) end
Mock.poke16 = function(addr, v) Mock.poke(addr, v & 0xFF); Mock.poke(addr + 1, (v >> 8) & 0xFF) end
Mock.poke32 = function(addr, v) Mock.poke16(addr, v & 0xFFFF); Mock.poke16(addr + 2, (v >> 16) & 0xFFFF) end

memory = {}
function memory.getmemorydomainlist()
    local t = {}
    for i, d in ipairs(DOMAINS) do t[i - 1] = d end
    return t
end
function memory.getmemorydomainsize(name) return DOMAIN_SIZE[resolve(name)] or 0x10000 end
function memory.getcurrentmemorydomain() return Mock.currentDomain end
function memory.usememorydomain(d)
    if isDomain(d) then Mock.currentDomain = d; return true end
    return false
end
function memory.readbyte(addr, domain) return peek(resolve(domain), addr) end
function memory.writebyte(addr, value, domain) poke(resolve(domain), addr, value) end
function memory.read_u16_le(addr, domain)
    local d = resolve(domain)
    return peek(d, addr) | (peek(d, addr + 1) << 8)
end
function memory.read_u32_le(addr, domain)
    return memory.read_u16_le(addr, domain) | (memory.read_u16_le(addr + 2, domain) << 16)
end
function memory.read_bytes_as_array(addr, length, domain)
    local d, t = resolve(domain), {}
    for i = 0, length - 1 do t[i + 1] = peek(d, addr + i) end
    return t
end

-- ── emu / client ─────────────────────────────────────────────────────
emu = {}
function emu.getsystemid() return Mock.system end
function emu.framecount() return Mock.frame end
function emu.getregisters()
    local t = {}
    for k, v in pairs(Mock.regs) do t[k] = v end
    return t
end
function emu.getregister(name) return Mock.regs[name] end
function emu.frameadvance()
    if Mock.inCallback then error("emu.frameadvance() is not allowed during any callbacks") end
    if Mock.system == "NULL" then error("NULL core cannot frameadvance") end
    coroutine.yield("frame")
end
function emu.yield()
    if Mock.inCallback then error("emu.yield() is not allowed during any callbacks") end
    coroutine.yield("yield")
end

client = {}
function client.pause() Mock.paused = true end
function client.unpause() Mock.paused = false end
function client.ispaused() return Mock.paused end
function client.screenshot(path) Mock.screenshots[#Mock.screenshots + 1] = path end
function client.reboot_core() Mock.frame = 0 end
function client.closerom() Mock.system = "NULL" end
function client.openrom(path) Mock.system = "NDS"; Mock.openedRom = path end

console = {}
function console.log(...)
    local parts = {}
    for i = 1, select("#", ...) do parts[#parts + 1] = tostring((select(i, ...))) end
    Mock.logs[#Mock.logs + 1] = table.concat(parts, " ")
end

-- ── joypad ───────────────────────────────────────────────────────────
joypad = {}
function joypad.get() local t = {}; for k, v in pairs(Mock.joypadSet) do t[k] = v end; return t end
function joypad.getimmediate() return {} end
function joypad.set(t) for k, v in pairs(t) do Mock.joypadSet[k] = v end end
function joypad.setanalog(t)
    for k, v in pairs(t) do
        local n = math.tointeger(tonumber(v))
        Mock.analog[k] = n   -- nil (from "") clears the autohold
    end
end

-- ── savestate ────────────────────────────────────────────────────────
savestate = {}
function savestate.saveslot(n) Mock.slots[n] = Mock.frame; return true end
function savestate.loadslot(n) return Mock.slots[n] ~= nil end
function savestate.save(path) Mock.slots[path] = Mock.frame; return true end
function savestate.load(path) return Mock.slots[path] ~= nil end

-- ── events / memory callbacks ────────────────────────────────────────
event = {}
local function register(kind, fn, addr, name, scope)
    local id = "guid-" .. Mock.nextCallback
    Mock.nextCallback = Mock.nextCallback + 1
    Mock.callbacks[id] = { kind = kind, fn = fn, addr = addr, name = name, scope = scope }
    return id
end
function event.onmemoryread(fn, addr, name, scope) return register("read", fn, addr, name, scope) end
function event.onmemorywrite(fn, addr, name, scope) return register("write", fn, addr, name, scope) end
function event.onmemoryexecute(fn, addr, name, scope) return register("execute", fn, addr, name, scope) end
function event.onmemoryexecuteany(fn, name, scope) return register("execute", fn, nil, name, scope) end
function event.unregisterbyid(id)
    local existed = Mock.callbacks[id] ~= nil
    Mock.callbacks[id] = nil
    return existed
end

-- Simulate the core touching memory: fires matching callbacks.
function Mock.access(kind, addr, value, pc)
    if pc then Mock.regs["ARM9 r15"] = pc end
    Mock.inCallback = true
    for _, cb in pairs(Mock.callbacks) do
        if cb.kind == kind and (cb.addr == nil or cb.addr == addr) then
            cb.fn(addr, value or 0, 0)
        end
    end
    Mock.inCallback = false
end
function Mock.callbackCount()
    local n = 0
    for _ in pairs(Mock.callbacks) do n = n + 1 end
    return n
end

-- ── host loop ────────────────────────────────────────────────────────
local Host = { waiting = nil, stuck = 0 }

function Host.start(path)
    Host.co = coroutine.create(assert(loadfile(path)))
    Host.waiting = nil
end

-- One EmuHawk loop iteration. Returns what the script is waiting on.
function Host.tick()
    if coroutine.status(Host.co) == "dead" then return "dead" end
    if Host.waiting == "frame" then
        if Mock.paused then
            Host.stuck = Host.stuck + 1
            return "stuck"
        end
        Mock.frame = Mock.frame + 1
        if Mock.onFrame then Mock.onFrame(Mock.frame) end  -- "emulate" the frame
    end
    local ok, why = coroutine.resume(Host.co)
    if not ok then error(why, 0) end
    Host.waiting = why
    return why
end

Mock.Host = Host

-- Strict globals: reading an undefined global (a typo, a stale reference)
-- raises instead of silently yielding nil.
setmetatable(_G, { __index = function(_, k) error("undefined global '" .. tostring(k) .. "'", 2) end })

return Mock
