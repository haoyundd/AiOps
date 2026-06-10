"""请求数据模型

定义 API 请求的 Pydantic 模型
"""

from pydantic import BaseModel, Field


class ChatRequest(BaseModel):
    """对话请求"""
#Field就是配置，定规则和说明
    # ... 表示必填字段# alias="Id" 表示在 JSON 中使用 Id 而不是 id
    id: str = Field(..., description="会话 ID", alias="Id")
    question: str = Field(..., description="用户问题", alias="Question")

    class Config:#给 FastAPI 自动生成的接口文档 /docs 提供示例！
        populate_by_name = True
        json_schema_extra = {
            "example": {
                "Id": "session-123",
                "Question": "什么是向量数据库？"
            }
        }


class ClearRequest(BaseModel):
    """清空会话请求"""

    session_id: str = Field(..., description="会话 ID", alias="sessionId")

    class Config:
        populate_by_name = True
