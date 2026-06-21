"""Loki MCP Server.

提供真实日志查询工具，数据来自 Loki Query API，而不是本地 mock。
"""

import os
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

import httpx
from fastmcp import FastMCP

mcp = FastMCP("LokiLog")

LOKI_URL = os.getenv("LOKI_URL", "http://localhost:3100").rstrip("/")
DEFAULT_SERVICE = os.getenv("DEMO_SERVICE_NAME", "demo-service")


def _parse_time_ns(value: Optional[str], default: datetime) -> int:
    """解析时间并转换为 Loki 使用的纳秒时间戳。"""
    if value:
        normalized = value.replace("Z", "+00:00")
        for parser in (
            lambda text: datetime.fromisoformat(text),
            lambda text: datetime.strptime(text, "%Y-%m-%d %H:%M:%S"),
        ):
            try:
                parsed = parser(normalized)
                if parsed.tzinfo is None:
                    parsed = parsed.replace(tzinfo=timezone.utc)
                return int(parsed.timestamp() * 1_000_000_000)
            except ValueError:
                continue

    return int(default.timestamp() * 1_000_000_000)


def _loki_query_range(params: Dict[str, Any]) -> Dict[str, Any]:
    """调用 Loki query_range 接口。"""
    url = f"{LOKI_URL}/loki/api/v1/query_range"
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


def _extract_log_samples(data: Dict[str, Any], limit: int) -> List[Dict[str, str]]:
    """从 Loki 响应中抽取日志样本，便于报告引用。"""
    samples: List[Dict[str, str]] = []
    for stream in data.get("result", []):
        labels = stream.get("stream", {})
        for ts, line in stream.get("values", []):
            samples.append(
                {
                    "timestamp_ns": ts,
                    "labels": str(labels),
                    "line": line,
                }
            )
            if len(samples) >= limit:
                return samples
    return samples


def _query_service_logs_impl(
    service_name: str = DEFAULT_SERVICE,
    keyword: Optional[str] = None,
    log_level: Optional[str] = None,
    start_time: Optional[str] = None,
    end_time: Optional[str] = None,
    limit: int = 20,
) -> Dict[str, Any]:
    """查询指定服务的真实 Loki 日志。

    Args:
        service_name: 服务名，对应 service label。
        keyword: 日志关键词，可选。
        log_level: 日志级别关键词，例如 error、warning。
        start_time: 开始时间，默认最近 30 分钟。
        end_time: 结束时间，默认当前时间。
        limit: 返回日志样本上限。
    """
    now = datetime.now(timezone.utc)
    start_ns = _parse_time_ns(start_time, now - timedelta(minutes=30))
    end_ns = _parse_time_ns(end_time, now)

    query = f'{{service="{service_name}"}}'
    if log_level:
        query += f' |~ "(?i){log_level}"'
    if keyword:
        query += f' |~ "{keyword}"'

    result = _loki_query_range(
        {
            "query": query,
            "start": start_ns,
            "end": end_ns,
            "limit": limit,
            "direction": "backward",
        }
    )
    result["evidence_type"] = "service_logs"
    result["service_name"] = service_name
    result["query"] = query
    result["samples"] = _extract_log_samples(result.get("data", {}), limit)
    return result


@mcp.tool()
def query_service_logs(
    service_name: str = DEFAULT_SERVICE,
    keyword: Optional[str] = None,
    log_level: Optional[str] = None,
    start_time: Optional[str] = None,
    end_time: Optional[str] = None,
    limit: int = 20,
) -> Dict[str, Any]:
    """查询指定服务的真实 Loki 日志。"""
    return _query_service_logs_impl(
        service_name=service_name,
        keyword=keyword,
        log_level=log_level,
        start_time=start_time,
        end_time=end_time,
        limit=limit,
    )


@mcp.tool()
def find_error_patterns(
    service_name: str = DEFAULT_SERVICE,
    start_time: Optional[str] = None,
    end_time: Optional[str] = None,
    limit: int = 50,
) -> Dict[str, Any]:
    """查找服务近期错误日志模式。"""
    result = _query_service_logs_impl(
        service_name=service_name,
        log_level="error|exception|timeout|failed",
        start_time=start_time,
        end_time=end_time,
        limit=limit,
    )

    pattern_counts: Dict[str, int] = {}
    for sample in result.get("samples", []):
        line = sample.get("line", "").lower()
        if "timeout" in line:
            pattern_counts["timeout"] = pattern_counts.get("timeout", 0) + 1
        elif "cpu" in line:
            pattern_counts["cpu"] = pattern_counts.get("cpu", 0) + 1
        elif "memory" in line:
            pattern_counts["memory"] = pattern_counts.get("memory", 0) + 1
        elif "error" in line or "exception" in line:
            pattern_counts["generic_error"] = pattern_counts.get("generic_error", 0) + 1

    result["evidence_type"] = "error_patterns"
    result["pattern_counts"] = pattern_counts
    return result


def _find_fault_signals_impl(
    service_name: str = DEFAULT_SERVICE,
    start_time: Optional[str] = None,
    end_time: Optional[str] = None,
    limit: int = 50,
) -> Dict[str, Any]:
    """查找故障线索日志，覆盖 warning、CPU、慢响应和注入开关等非 error 证据。"""
    result = _query_service_logs_impl(
        service_name=service_name,
        log_level="cpu|slow|fault|injected|latency|warning|error|exception|timeout|failed",
        start_time=start_time,
        end_time=end_time,
        limit=limit,
    )

    signal_counts: Dict[str, int] = {}
    for sample in result.get("samples", []):
        line = sample.get("line", "").lower()
        # 按真实故障排查关注点归类，报告层可以直接说明命中了哪些线索。
        if "cpu" in line:
            signal_counts["cpu"] = signal_counts.get("cpu", 0) + 1
        if "slow" in line or "latency" in line:
            signal_counts["latency"] = signal_counts.get("latency", 0) + 1
        if "fault" in line or "injected" in line:
            signal_counts["fault_injection"] = signal_counts.get("fault_injection", 0) + 1
        if "warning" in line:
            signal_counts["warning"] = signal_counts.get("warning", 0) + 1
        if "error" in line or "exception" in line or "failed" in line:
            signal_counts["error"] = signal_counts.get("error", 0) + 1
        if "timeout" in line:
            signal_counts["timeout"] = signal_counts.get("timeout", 0) + 1

    result["evidence_type"] = "fault_signals"
    result["signal_counts"] = signal_counts
    result["total_signal_samples"] = len(result.get("samples", []))
    return result


@mcp.tool()
def find_fault_signals(
    service_name: str = DEFAULT_SERVICE,
    start_time: Optional[str] = None,
    end_time: Optional[str] = None,
    limit: int = 50,
) -> Dict[str, Any]:
    """查找故障线索日志，覆盖 warning、CPU、慢响应和注入开关等非 error 证据。"""
    return _find_fault_signals_impl(
        service_name=service_name,
        start_time=start_time,
        end_time=end_time,
        limit=limit,
    )


@mcp.tool()
def search_log(
    service_name: str = DEFAULT_SERVICE,
    keyword: Optional[str] = None,
    limit: int = 20,
) -> Dict[str, Any]:
    """兼容旧工具名的日志查询入口。"""
    return _query_service_logs_impl(service_name=service_name, keyword=keyword, limit=limit)


if __name__ == "__main__":
    mcp.run(transport="streamable-http", host="0.0.0.0", port=8003, path="/mcp")
