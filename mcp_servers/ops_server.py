"""Unified read-only MCP surface for observability evidence."""

from __future__ import annotations

from typing import Any

from fastmcp import FastMCP

from app.observability.tools import ops_tool_registry

mcp = FastMCP("aiops-readonly-ops")


async def _execute(name: str, **kwargs: Any) -> dict[str, Any]:
    result, _record = await ops_tool_registry.execute(name, **kwargs)
    return result.model_dump(mode="json")


@mcp.tool()
async def get_service_health(health_url: str) -> dict[str, Any]:
    return await _execute("get_service_health", health_url=health_url)


@mcp.tool()
async def get_red_metrics(service_name: str) -> dict[str, Any]:
    return await _execute("get_red_metrics", service_name=service_name)


@mcp.tool()
async def get_jvm_metrics(service_name: str) -> dict[str, Any]:
    return await _execute("get_jvm_metrics", service_name=service_name)


@mcp.tool()
async def get_messaging_metrics(service_name: str) -> dict[str, Any]:
    """返回真实 RocketMQ 发布失败计数；工具只读且不接触消息内容。"""
    return await _execute("get_messaging_metrics", service_name=service_name)


@mcp.tool()
async def get_top_endpoint_metrics(
    service_name: str, limit: int = 10
) -> dict[str, Any]:
    """返回路由模板级热点指标；原始 URL 和请求参数不会进入结果。"""
    return await _execute(
        "get_top_endpoint_metrics", service_name=service_name, limit=limit
    )


@mcp.tool()
async def get_jvm_thread_snapshot(service_name: str) -> dict[str, Any]:
    """CPU 告警专用只读工具；MerchantFlow 端继续执行 AK/SK 和限流校验。"""
    return await _execute("get_jvm_thread_snapshot", service_name=service_name)


@mcp.tool()
async def query_loki_logs(
    service_name: str, keyword: str = "", limit: int = 100
) -> dict[str, Any]:
    return await _execute(
        "query_loki_logs", service_name=service_name, keyword=keyword, limit=limit
    )


@mcp.tool()
async def get_log_error_patterns(
    service_name: str, start: str | None = None, end: str | None = None
) -> dict[str, Any]:
    """按告警时间窗口统计日志，避免独立 MCP 进程丢失 Incident 上下文。"""
    return await _execute(
        "get_log_error_patterns", service_name=service_name, start=start, end=end
    )


@mcp.tool()
async def search_tempo_traces(
    service_name: str,
    limit: int = 20,
    start: str | None = None,
    end: str | None = None,
) -> dict[str, Any]:
    """按告警时间窗口搜索 Trace，随后由 Agent 展开慢 Trace 详情。"""
    return await _execute(
        "search_tempo_traces",
        service_name=service_name,
        limit=limit,
        start=start,
        end=end,
    )


@mcp.tool()
async def get_trace_detail(trace_id: str) -> dict[str, Any]:
    return await _execute("get_trace_detail", trace_id=trace_id)


if __name__ == "__main__":
    mcp.run(transport="streamable-http", host="0.0.0.0", port=8004)
