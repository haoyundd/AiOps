"""Incident Command Model 请求模型。"""

from typing import Any, Dict, Optional

from pydantic import BaseModel, Field


class IncidentCommandActionRequest(BaseModel):
    """提交 incident 指挥动作的请求。"""

    action_type: str = Field(description="动作类型，例如 acknowledge、assign、add_note、escalate、mitigate、resolve、reopen")
    actor: str = Field(default="operator", description="执行动作的人或系统")
    note: str = Field(default="", description="动作说明或处置记录")
    assignee: Optional[str] = Field(default=None, description="分派对象，仅 assign 类动作需要")
    severity: Optional[str] = Field(default=None, description="升级后的严重级别，仅 escalate 类动作需要")
    payload: Dict[str, Any] = Field(default_factory=dict, description="扩展字段，保留后续集成空间")
