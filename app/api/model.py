"""模型配置 API。"""

from fastapi import APIRouter, HTTPException

from app.config import config
from app.core.llm_factory import llm_factory
from app.models.model_config import ModelSwitchRequest
from app.services.rag_agent_service import rag_agent_service

router = APIRouter()


@router.get("/model/config")
async def get_model_config():
    """查询当前聊天模型配置。"""
    return {
        "code": 200,
        "message": "success",
        "data": {
            "current": llm_factory.get_public_config(),
            "options": llm_factory.list_options(),
            "aiops_auto_diagnosis_enabled": config.aiops_auto_diagnosis_enabled,
            "note": "该配置只影响聊天和 AIOps 推理模型，不改变 Milvus embedding 模型。",
        },
    }


@router.post("/model/switch")
async def switch_model(request: ModelSwitchRequest):
    """切换聊天模型供应商，并重建普通对话 Agent。"""
    try:
        current = llm_factory.switch_provider(
            provider=request.provider,
            model=request.model,
            base_url=request.base_url,
            api_key=request.api_key,
        )
        rag_agent_service.reload_model()
        return {
            "code": 200,
            "message": "success",
            "data": {
                "current": current,
                "note": "模型已切换，后续普通对话和新 AIOps 诊断会使用新模型。",
            },
        }
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
