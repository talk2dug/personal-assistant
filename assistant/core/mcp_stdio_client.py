"""Client for MCP servers that speak stdio rather than Streamable HTTP.

mcp_client.py's MCPClient only knows Streamable HTTP because Era, the phone, and HA are
all reachable that way. Third-party MCP servers distributed as npm/PyPI packages (the
Airbnb, Ticketmaster, Kroger, CCXT integrations) are, without exception, spawned-child-
process servers instead: the client launches `npx some-package` or an installed binary
and talks JSON-RPC over its stdin/stdout. That's a different transport, not a different
protocol, so this class exposes the exact same list_tools()/call_tool() shape MCPClient
does — every context dataclass and _dispatch_tool_call in engine.py can treat the two
interchangeably without knowing which one they're holding.

One process per call, same as MCPClient's one HTTP session per call: these aren't a hot
path, and a subprocess spawn is a few hundred ms, which is fine for a tool a person
triggers by asking Jarvis to look something up. A server that authenticates once and
caches the result to its own disk (Kroger's OAuth token) keeps that state itself, in its
own process's storage, across separate spawns — nothing here needs to persist a session.

Server code frequently runs in its own isolated venv or via npx's global cache, entirely
separate from Jarvis's own Python environment — see kroger-mcp's .venv-kroger. This
client only needs a command to exec; it does not care what runtime backs it.
"""
import asyncio
import os
import time

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


class StdioMCPClient:
    def __init__(self, command: str, args: list[str] | None = None,
                env: dict[str, str] | None = None, timeout: float = 60.0):
        self.command = command
        self.args = args or []
        # Merge over the real environment rather than replacing it — the child process
        # still needs PATH etc. to find its own runtime (node, python) on Windows.
        # PYTHONPATH/PYTHONHOME are the one exception: real incident, found live under
        # jarvis-core.service -- that service's own Environment registry value sets
        # PYTHONPATH to the *main* .venv's site-packages (pythonservice.exe needs it to
        # find pywin32; see deploy/windows_service.py's _fix_venv_hosting), and Python
        # inherits that verbatim into every child process by default. kroger-mcp.exe
        # lives in its own, deliberately incompatible .venv-kroger (this module's own
        # docstring: installing kroger-mcp into the main env broke the shared `mcp` SDK
        # package) -- inheriting the wrong venv's PYTHONPATH made it crash on startup
        # (MCPError: Connection closed) every time under the real service, while working
        # fine from any plain interactive shell that never had that override set. A
        # venv-installed console-script .exe resolves its own site-packages from its own
        # embedded interpreter path regardless of PYTHONPATH, so dropping it here is safe
        # for every caller, not just Kroger.
        self.env = {k: v for k, v in os.environ.items() if k not in ("PYTHONPATH", "PYTHONHOME")}
        self.env.update(env or {})
        self.timeout = timeout

    async def _run(self, fn):
        params = StdioServerParameters(command=self.command, args=self.args, env=self.env)
        async with asyncio.timeout(self.timeout):
            async with stdio_client(params) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    return await fn(session)

    def _run_with_retry(self, fn, attempts: int = 2):
        last_error = None
        for attempt in range(attempts):
            try:
                return asyncio.run(self._run(fn))
            except Exception as e:
                last_error = e
                if attempt < attempts - 1:
                    time.sleep(1)
        raise last_error

    def list_tools(self) -> list[dict]:
        """Returns [{"name", "description", "input_schema"}, ...] — same shape MCPClient
        returns, so callers never need to know which transport they're holding."""

        async def _list(session):
            result = await session.list_tools()
            return [
                {"name": t.name, "description": t.description, "input_schema": t.input_schema}
                for t in result.tools
            ]

        return self._run_with_retry(_list)

    def call_tool(self, name: str, arguments: dict) -> dict:
        async def _call(session):
            result = await session.call_tool(name, arguments)
            return {
                "is_error": bool(getattr(result, "isError", False)),
                "content": [getattr(block, "text", str(block)) for block in result.content],
            }

        return self._run_with_retry(_call)
