"""最小化的 Streamable HTTP MCP 客户端。

诊断 Agent 只通过这个客户端访问 mcp-ops，不直接接触 Prometheus、Loki 或 Tempo。
"""

from __future__ import annotations

import json
from typing import Any

import httpx


class MCPProtocolError(RuntimeError):
    """MCP 服务返回无法解析的协议响应时抛出。"""


def _decode_response(response: httpx.Response) -> dict[str, Any]:
    """解析 JSON 或 SSE 中的 JSON-RPC 响应。"""
    if response.status_code >= 400:
        raise MCPProtocolError(f"MCP HTTP {response.status_code}: {response.text[:300]}")
    if not response.content:
        return {}
    content_type = response.headers.get("content-type", "")
    if "application/json" in content_type:
        payload = response.json()
        if isinstance(payload, dict):
            return payload
    for line in response.text.splitlines():
        if line.startswith("data:"):
            payload = json.loads(line.removeprefix("data:").strip())
            if isinstance(payload, dict):
                return payload
    raise MCPProtocolError("MCP response did not contain a JSON-RPC message")


class StreamableHTTPMCPClient:
    """每次工具调用建立一个短生命周期 MCP 会话，避免跨任务共享会话状态。"""

    def __init__(self, url: str, timeout: float) -> None:
        self.url = url
        self.timeout = timeout

    async def _post(
        self,
        client: httpx.AsyncClient,
        headers: dict[str, str],
        message: dict[str, Any],
    ) -> tuple[dict[str, Any], str | None]:
        """发送单个 JSON-RPC 请求并返回响应与会话 ID。"""
        response = await client.post(self.url, headers=headers, json=message)
        return _decode_response(response), response.headers.get("mcp-session-id")

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        """初始化 MCP 会话、调用工具并返回工具原始结构化结果。"""
        headers = {
            "Accept": "application/json, text/event-stream",
            "Content-Type": "application/json",
            # 部分 Windows Docker 转发链路会将默认 httpx User-Agent 错误转成 502。
            "User-Agent": "aiops-mcp-client/1.0",
            "MCP-Protocol-Version": "2025-06-18",
        }
        async with httpx.AsyncClient(timeout=self.timeout, trust_env=False, http2=False) as client:
            initialize, session_id = await self._post(
                client,
                headers,
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": {
                        "protocolVersion": "2025-06-18",
                        "capabilities": {},
                        "clientInfo": {"name": "aiops-incident-agent", "version": "2.0"},
                    },
                },
            )
            if "error" in initialize or not session_id:
                raise MCPProtocolError(f"MCP initialize failed: {initialize}")
            headers["mcp-session-id"] = session_id

            # initialized 是 MCP 通知，不要求服务端返回 JSON-RPC 结果。
            await client.post(
                self.url,
                headers=headers,
                json={"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}},
            )
            response, _ = await self._post(
                client,
                headers,
                {
                    "jsonrpc": "2.0",
                    "id": 2,
                    "method": "tools/call",
                    "params": {"name": name, "arguments": arguments},
                },
            )
            if "error" in response:
                raise MCPProtocolError(f"MCP tool {name} failed: {response['error']}")
            result = response.get("result")
            if not isinstance(result, dict):
                raise MCPProtocolError(f"MCP tool {name} returned no result")
            structured = result.get("structuredContent")
            if isinstance(structured, dict):
                return structured
            for item in result.get("content", []):
                if item.get("type") == "text":
                    payload = json.loads(item.get("text", "{}"))
                    if isinstance(payload, dict):
                        return payload
            raise MCPProtocolError(f"MCP tool {name} returned no structured content")
