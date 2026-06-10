"""Demo service 代理接口。"""

from typing import Any, Dict

import httpx
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app.config import config

router = APIRouter()


class FaultSwitchRequest(BaseModel):
    """故障开关请求体。"""

    enabled: bool

#method: HTTP 方法，比如 "GET"、"POST"
#path: demo-service 的接口路径，比如 "/faults/cpu"
#payload: 请求体，比如 {"enabled": true}
async def _proxy_demo_request(method: str, path: str, payload: Dict[str, Any] | None = None) -> Dict[str, Any]:
    """通过后端代理访问 demo-service，避免浏览器跨域请求失败。"""
    #这里不是 localhost:9910，因为它是在 Docker 容器内部访问另一个容器，所以用服务名 demo-service。
    url = f"{config.demo_service_url.rstrip('/')}{path}"
    try: #异步请求 demo-service，创建 AsyncClient 实例，设置超时时间为 10 秒
        async with httpx.AsyncClient(timeout=10.0) as client:#也可以这样写client = httpx.AsyncClient(timeout=10.0)
            response = await client.request(method, url, json=payload)#发送实际的 HTTP 请求
            response.raise_for_status()#检查 HTTP 状态码是否正常
            return response.json()#返回 JSON 格式的响应体
    except httpx.HTTPStatusError as exc:
        raise HTTPException(
            status_code=exc.response.status_code,
            detail=f"demo-service 返回异常: {exc.response.text}",
        ) from exc
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail=f"无法访问 demo-service: {exc}") from exc


@router.post("/demo/faults/clear")
async def clear_demo_faults():
    """清理 demo-service 的全部故障开关。"""
    return {
        "code": 200,
        "message": "success",
        "data": await _proxy_demo_request("POST", "/faults/clear"),
    }


@router.post("/demo/faults/{fault_type}")
async def set_demo_fault(fault_type: str, request: FaultSwitchRequest):
    """打开或关闭 demo-service 的指定故障开关。"""
    if fault_type not in {"cpu", "slow", "error"}:
        raise HTTPException(status_code=400, detail="不支持的故障类型")
    return {
        "code": 200,
        "message": "success",
        "data": await _proxy_demo_request("POST", f"/faults/{fault_type}", request.model_dump()),
    }


@router.get("/demo/work")
async def trigger_demo_work():
    """触发一次 demo-service 业务请求，用于让日志和指标更快变化。"""
    return {
        "code": 200,
        "message": "success",
        "data": await _proxy_demo_request("GET", "/work"),
    }


@router.get("/demo/health")
async def get_demo_health():
    """查询 demo-service 当前健康状态和故障开关。"""
    return {
        "code": 200,
        "message": "success",
        "data": await _proxy_demo_request("GET", "/health"),
    }
