"""AIOps 演示服务。

这个服务用于制造真实的指标和日志，帮助 Agent 在本地 Docker 环境中完成
可复现的故障诊断演示。
"""

import asyncio
import logging
import time
from pathlib import Path
from typing import Dict

from fastapi import FastAPI
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel

SERVICE_NAME = "demo-service"
LOG_PATH = Path("/app/logs/demo-service.log")

app = FastAPI(title="Demo Service", version="1.0.0")

state: Dict[str, bool | int | float] = {
    "cpu_spike": False,
    "slow_response": False,
    "error_mode": False,
    "request_count": 0,
    "error_count": 0,
    "total_latency": 0.0,
}


class FaultSwitch(BaseModel):
    """故障开关请求。"""

    enabled: bool


def _setup_logger() -> logging.Logger:
    """初始化文件日志，供 Promtail 采集到 Loki。"""
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger(SERVICE_NAME)
    logger.setLevel(logging.INFO)
    logger.handlers.clear()

    formatter = logging.Formatter(
        fmt="%(asctime)s %(levelname)s service=%(name)s message=%(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S%z",
    )
    file_handler = logging.FileHandler(LOG_PATH, encoding="utf-8")
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)

    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(formatter)
    logger.addHandler(stream_handler)
    return logger


logger = _setup_logger()


@app.get("/health")
async def health() -> Dict[str, object]:
    """服务健康检查。"""
    return {"service": SERVICE_NAME, "status": "ok", "faults": _faults()}


@app.get("/work")
#调 5 次 /work 造一点业务请求和日志
async def work() -> Dict[str, object]:
    """模拟业务请求，并根据故障开关制造延迟或错误。"""
    start = time.perf_counter()
    state["request_count"] = int(state["request_count"]) + 1

    if state["slow_response"]:
        await asyncio.sleep(1.2)
        logger.warning("slow response injected latency_ms=1200")

    if state["cpu_spike"]:

        _burn_cpu(0.2)
        logger.warning("cpu spike injected cpu_load=high")

    if state["error_mode"]:
        state["error_count"] = int(state["error_count"]) + 1
        logger.error("error mode injected exception=DemoInjectedError")
        return {"service": SERVICE_NAME, "status": "error", "message": "injected error"}

    latency = time.perf_counter() - start
    state["total_latency"] = float(state["total_latency"]) + latency
    logger.info(f"request handled status=ok latency_ms={latency * 1000:.2f}")
    return {"service": SERVICE_NAME, "status": "ok", "latency_ms": round(latency * 1000, 2)}


#每隔15秒 Prometheus 定时发的，GET demo-service:9910/metrics
@app.get("/metrics", response_class=PlainTextResponse)
async def metrics() -> str:
    """输出 Prometheus text format 指标。"""
    request_count = int(state["request_count"])
    error_count = int(state["error_count"])
    total_latency = float(state["total_latency"])
    avg_latency = total_latency / request_count if request_count else 0.0
    cpu_load = 0.95 if state["cpu_spike"] else 0.15
    memory_load = 0.35
    slow_flag = 1 if state["slow_response"] else 0
    error_flag = 1 if state["error_mode"] else 0

    return "\n".join(
        [
            "# HELP demo_cpu_load Simulated CPU load for demo-service.",
            "# TYPE demo_cpu_load gauge",
            f'demo_cpu_load{{service="{SERVICE_NAME}"}} {cpu_load}',
            "# HELP demo_memory_load Simulated memory load for demo-service.",
            "# TYPE demo_memory_load gauge",
            f'demo_memory_load{{service="{SERVICE_NAME}"}} {memory_load}',
            "# HELP demo_request_total Total demo requests.",
            "# TYPE demo_request_total counter",
            f'demo_request_total{{service="{SERVICE_NAME}"}} {request_count}',
            "# HELP demo_error_total Total injected errors.",
            "# TYPE demo_error_total counter",
            f'demo_error_total{{service="{SERVICE_NAME}"}} {error_count}',
            "# HELP demo_average_latency_seconds Average request latency.",
            "# TYPE demo_average_latency_seconds gauge",
            f'demo_average_latency_seconds{{service="{SERVICE_NAME}"}} {avg_latency}',
            "# HELP demo_fault_slow_response Slow response fault switch.",
            "# TYPE demo_fault_slow_response gauge",
            f'demo_fault_slow_response{{service="{SERVICE_NAME}"}} {slow_flag}',
            "# HELP demo_fault_error_mode Error mode fault switch.",
            "# TYPE demo_fault_error_mode gauge",
            f'demo_fault_error_mode{{service="{SERVICE_NAME}"}} {error_flag}',
            "",
        ]
    )


@app.post("/faults/cpu")
async def set_cpu_fault(request: FaultSwitch) -> Dict[str, object]:
    """打开或关闭 CPU 故障注入。"""
    state["cpu_spike"] = request.enabled#state["cpu_spike"] =True
    logger.warning(f"fault switch changed name=cpu_spike enabled={request.enabled}")
    return {"service": SERVICE_NAME, "fault": "cpu_spike", "enabled": request.enabled}
#,返回给前端。到这里为止，只完成了,故障开关打开。还没有真正产生告警


@app.post("/faults/slow")
async def set_slow_fault(request: FaultSwitch) -> Dict[str, object]:
    """打开或关闭慢响应故障注入。"""
    state["slow_response"] = request.enabled
    logger.warning(f"fault switch changed name=slow_response enabled={request.enabled}")
    return {"service": SERVICE_NAME, "fault": "slow_response", "enabled": request.enabled}


@app.post("/faults/error")
async def set_error_fault(request: FaultSwitch) -> Dict[str, object]:
    """打开或关闭错误日志故障注入。"""
    state["error_mode"] = request.enabled
    logger.warning(f"fault switch changed name=error_mode enabled={request.enabled}")
    return {"service": SERVICE_NAME, "fault": "error_mode", "enabled": request.enabled}


@app.post("/faults/clear")
async def clear_faults() -> Dict[str, object]:
    """清理全部故障注入开关。"""
    state["cpu_spike"] = False
    state["slow_response"] = False
    state["error_mode"] = False
    logger.info("all fault switches cleared")
    return {"service": SERVICE_NAME, "faults": _faults()}


def _faults() -> Dict[str, bool]:
    """返回当前故障开关状态。"""
    return {
        "cpu_spike": bool(state["cpu_spike"]),
        "slow_response": bool(state["slow_response"]),
        "error_mode": bool(state["error_mode"]),
    }


def _burn_cpu(duration_seconds: float) -> None:
    """短时间占用 CPU，用于制造可观测的请求延迟。"""
    end_time = time.perf_counter() + duration_seconds
    value = 0
    while time.perf_counter() < end_time:
        value = (value + 1) % 1000003
