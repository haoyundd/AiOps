"""Prometheus MCP Server.

提供真实指标查询工具，数据来自 Prometheus HTTP API，而不是本地 mock。
"""

import os
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

import httpx
from fastmcp import FastMCP

mcp = FastMCP("PrometheusMonitor")  # 创建 MCP 服务
#monitor_server 容器内部用 httpx 直接调 Prometheus 的 HTTP API。
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


def _extract_numeric_values(result: Dict[str, Any]) -> List[float]:
    """从 Prometheus query_range 响应中抽取数值样本，用于生成证据摘要。"""
    values: List[float] = []
    for series in result.get("data", {}).get("result", []):
        for _, raw_value in series.get("values", []):
            try:
                values.append(float(raw_value))
            except (TypeError, ValueError):
                continue
    return values


def _query_metric_summary_impl(
    metric_name: str,
    service_name: str = DEFAULT_SERVICE,
    threshold: Optional[float] = None,
    start_time: Optional[str] = None,
    end_time: Optional[str] = None,
    step: str = "30s",
) -> Dict[str, Any]:
    """查询指标区间并生成面向报告的摘要证据。"""
    range_result = _query_metric_range_impl(
        metric_name=metric_name,
        service_name=service_name,
        start_time=start_time,
        end_time=end_time,
        step=step,
    )
    values = _extract_numeric_values(range_result)
    exceeded = False
    if threshold is not None and values:
        exceeded = max(values) > threshold

    summary = {
        "sample_count": len(values),
        "last": values[-1] if values else None,
        "min": min(values) if values else None,
        "max": max(values) if values else None,
        "avg": round(sum(values) / len(values), 6) if values else None,
        "threshold": threshold,
        "exceeded": exceeded,
    }
    return {
        "evidence_type": "metric_summary",
        "source": "prometheus",
        "metric_name": metric_name,
        "service_name": service_name,
        "query": range_result["query"],
        "summary": summary,
        "range": {
            "start": range_result["query_params"].get("start"),
            "end": range_result["query_params"].get("end"),
            "step": step,
        },
        "raw_status": range_result.get("status"),
    }

   # 注册工具
@mcp.tool()

def query_metric_range(
    metric_name: str,
    service_name: str = DEFAULT_SERVICE,
    start_time: Optional[str] = None,
    end_time: Optional[str] = None,
    step: str = "30s",
) -> Dict[str, Any]:
    """查询指定服务的 Prometheus 区间指标。"""
    # 工具实现：调 Prometheus API
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
def query_metric_summary(
    metric_name: str,
    service_name: str = DEFAULT_SERVICE,
    threshold: Optional[float] = None,
    start_time: Optional[str] = None,
    end_time: Optional[str] = None,
    step: str = "30s",
) -> Dict[str, Any]:
    """查询指标摘要，返回 last/min/max/avg/exceeded 等可直接引用的证据。"""
    return _query_metric_summary_impl(
        metric_name=metric_name,
        service_name=service_name,
        threshold=threshold,
        start_time=start_time,
        end_time=end_time,
        step=step,
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
