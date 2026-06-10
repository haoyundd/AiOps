"""Prometheus MCP Server.

提供真实指标查询工具，数据来自 Prometheus HTTP API，而不是本地 mock。
"""

import os
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional

import httpx
from fastmcp import FastMCP

mcp = FastMCP("PrometheusMonitor")

PROMETHEUS_URL = os.getenv("PROMETHEUS_URL", "http://localhost:9090").rstrip("/")
DEFAULT_SERVICE = os.getenv("DEMO_SERVICE_NAME", "demo-service")


def _parse_time(value: Optional[str], default: datetime) -> datetime:
    """解析用户传入时间，失败时使用默认值。"""
    if not value:
        return default

    normalized = value.replace("Z", "+00:00")
    for parser in (
        lambda text: datetime.fromisoformat(text),
        lambda text: datetime.strptime(text, "%Y-%m-%d %H:%M:%S"),
    ):
        try:
            parsed = parser(normalized)
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return parsed.astimezone(timezone.utc)
        except ValueError:
            continue

    return default


def _prometheus_get(path: str, params: Dict[str, Any]) -> Dict[str, Any]:
    """调用 Prometheus HTTP API，并返回结构化结果。"""
    url = f"{PROMETHEUS_URL}{path}"
    with httpx.Client(timeout=10.0) as client:
        response = client.get(url, params=params)
        response.raise_for_status()
        payload = response.json()

    return {
        "url": url,
        "query_params": params,
        "status": payload.get("status"),
        "data": payload.get("data", {}),
    }


def _query_metric_range_impl(
    metric_name: str,
    service_name: str = DEFAULT_SERVICE,
    start_time: Optional[str] = None,
    end_time: Optional[str] = None,
    step: str = "30s",
) -> Dict[str, Any]:
    """查询指定服务的 Prometheus 区间指标。

    Args:
        metric_name: Prometheus 指标名，例如 demo_cpu_load。
        service_name: 服务名，对应 service label。
        start_time: 开始时间，默认最近 30 分钟。
        end_time: 结束时间，默认当前时间。
        step: 查询步长，例如 30s、1m。
    """
    now = datetime.now(timezone.utc)
    start_dt = _parse_time(start_time, now - timedelta(minutes=30))
    end_dt = _parse_time(end_time, now)
    query = f'{metric_name}{{service="{service_name}"}}'

    result = _prometheus_get(
        "/api/v1/query_range",
        {
            "query": query,
            "start": start_dt.isoformat(),
            "end": end_dt.isoformat(),
            "step": step,
        },
    )
    result["evidence_type"] = "metric_range"
    result["metric_name"] = metric_name
    result["service_name"] = service_name
    result["query"] = query
    return result


@mcp.tool()
def query_metric_range(
    metric_name: str,
    service_name: str = DEFAULT_SERVICE,
    start_time: Optional[str] = None,
    end_time: Optional[str] = None,
    step: str = "30s",
) -> Dict[str, Any]:
    """查询指定服务的 Prometheus 区间指标。"""
    return _query_metric_range_impl(metric_name, service_name, start_time, end_time, step)


@mcp.tool()
def query_cpu_metrics(
    service_name: str = DEFAULT_SERVICE,
    start_time: Optional[str] = None,
    end_time: Optional[str] = None,
    interval: str = "30s",
) -> Dict[str, Any]:
    """查询 demo service 的 CPU 压力指标。"""
    return _query_metric_range_impl(
        metric_name="demo_cpu_load",
        service_name=service_name,
        start_time=start_time,
        end_time=end_time,
        step=interval,
    )


@mcp.tool()
def query_memory_metrics(
    service_name: str = DEFAULT_SERVICE,
    start_time: Optional[str] = None,
    end_time: Optional[str] = None,
    interval: str = "30s",
) -> Dict[str, Any]:
    """查询 demo service 的内存压力指标。"""
    return _query_metric_range_impl(
        metric_name="demo_memory_load",
        service_name=service_name,
        start_time=start_time,
        end_time=end_time,
        step=interval,
    )


def _get_service_health_impl(service_name: str = DEFAULT_SERVICE) -> Dict[str, Any]:
    """查询 Prometheus 中目标服务是否处于 up 状态。"""
    query = f'up{{service="{service_name}"}}'
    result = _prometheus_get("/api/v1/query", {"query": query})
    result["evidence_type"] = "service_health"
    result["service_name"] = service_name
    result["query"] = query
    return result


@mcp.tool()
def get_service_health(service_name: str = DEFAULT_SERVICE) -> Dict[str, Any]:
    """查询 Prometheus 中目标服务是否处于 up 状态。"""
    return _get_service_health_impl(service_name)


if __name__ == "__main__":
    mcp.run(transport="streamable-http", host="0.0.0.0", port=8004, path="/mcp")
