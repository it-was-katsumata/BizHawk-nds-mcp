"""Protocol smoke test: spawn the real server over stdio with the installed
mcp SDK's client (catches SDK API breaks like the 1.x -> 2.x handler change)."""

import asyncio
import json
import os
import sys

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def test_initialize_list_and_call_over_stdio():
    async def scenario():
        params = StdioServerParameters(command=sys.executable,
                                       args=[os.path.join(ROOT, "src", "bizhawk_mcp_server.py")])
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                tools = (await session.list_tools()).tools
                by_name = {t.name: t for t in tools}
                assert len(by_name) == 42
                def hints(name):  # wire format (2.x attributes are snake_case)
                    return by_name[name].annotations.model_dump(by_alias=True, exclude_none=True)
                assert hints("emu_debug_read_memory")["readOnlyHint"] is True
                assert hints("emu_debug_raw_command")["destructiveHint"] is True
                assert hints("emu_debug_write_memory") == {"readOnlyHint": False, "destructiveHint": False,
                                                           "openWorldHint": False}
                # The state file is not where the server looks in this test
                # (or is stale), so this must come back as a tool error.
                result = await session.call_tool("emu_debug_raw_command", {"action": "memory.getDomains"})
                payload = json.loads(result.content[0].text)
                is_error = getattr(result, "is_error", getattr(result, "isError", None))
                return is_error, payload

    is_error, payload = asyncio.run(asyncio.wait_for(scenario(), timeout=60))
    assert is_error is True
    assert "error" in payload
