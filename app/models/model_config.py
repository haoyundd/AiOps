"""模型切换请求模型。"""

from typing import Optional

from pydantic import BaseModel, Field


class ModelSwitchRequest(BaseModel):
    """切换聊天模型供应商的请求。"""

    provider: str = Field(description="模型供应商，例如 dashscope、xiaomi、custom")
    model: Optional[str] = Field(default=None, description="模型名称，不传则使用该供应商默认模型")
    base_url: Optional[str] = Field(default=None, description="OpenAI-compatible base_url")
    api_key: Optional[str] = Field(default=None, description="模型供应商 API Key，不返回给前端")
