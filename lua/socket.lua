-----------------------------------------------------------------------------
-- LuaSocket helper module
-- Creates and returns the socket namespace.
-- Loads socket.core if available.
-----------------------------------------------------------------------------

local socket = require("socket.core")

-- Useful for creating timeouts
function socket.newtry(finalizer)
    return function(...)
        local status = (...)
        if not status then
            pcall(finalizer)
            error((select(2, ...)), 0)
        end
        return ...
    end
end

function socket.protect(func)
    return function(...)
        local co = coroutine.create(func)
        while true do
            local results = {coroutine.resume(co, ...)}
            local status = results[1]
            if not status then
                return nil, results[2]
            end
            if coroutine.status(co) == "suspended" then
                -- pass (yield support if needed)
            else
                table.remove(results, 1)
                return table.unpack(results)
            end
        end
    end
end

-- Timeout handler
function socket.skip(d, ...)
    local ok, err = d
    if ok then return ok, err, ... end
    return nil, err
end

-- Basic namespace functions
socket.connect = socket.tcp and function(address, port, laddress, lport)
    local sock, err = socket.tcp()
    if not sock then return nil, err end
    if laddress then
        local res, err = sock:bind(laddress, lport or 0)
        if not res then return nil, err end
    end
    local res, err = sock:connect(address, port)
    if not res then return nil, err end
    return sock
end or nil

return socket
