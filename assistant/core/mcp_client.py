"""Thin client for Era's remote MCP server (Streamable HTTP transport, API-key auth).

Each call opens a short-lived session rather than holding a persistent connection —
Era calls aren't a hot path like reminders, so the simplicity is worth the small
per-call latency cost.
"""
import asyncio
import time

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client


class MCPClient:
    def __init__(self, url: str, api_key: str | None = None):
        self.url = url
        self.api_key = api_key

    async def _run(self, fn):
        # Some Era account-aggregation calls are genuinely slow (live bank lookups);
        # 60s gives real requests room without hanging forever on a truly dead connection.
        # api_key is optional — the phone MCP server (LAN-only, no auth) doesn't need it.
        headers = {"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}
        http_client = httpx.AsyncClient(headers=headers, timeout=60.0)
        async with streamable_http_client(self.url, http_client=http_client) as (read, write):
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
        """Returns [{"name", "description", "input_schema"}, ...]."""

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
