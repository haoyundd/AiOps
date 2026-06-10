"""AIOps 请求和响应模型"""

from typing import Any, Dict, Optional

from pydantic import BaseModel, Field

# Pydantic 自动生成 __init__ 方法
    # 等价于：
    # def __init__(self, session_id, service_name, ...):
    #     self.session_id = session_id
    #     self.service_name = service_name
    #     # ... 验证类型
class AIOpsRequest(BaseModel):
    """AIOps 诊断请求。

    请求以结构化告警为入口，让 Agent 围绕明确的服务、指标和时间窗口收集证据。
    """
    
    session_id: Optional[str] = Field(
        default="default",
        description="会话ID，用于追踪诊断历史"
    )
    service_name: str = Field(
        default="demo-service",
        description="触发告警的服务名称"
    )
    alert_name: str = Field(
        default="HighCPUUsage",
        description="告警名称"
    )
    severity: str = Field(
        default="warning",
        description="告警级别，例如 warning、critical"
    )
    metric_name: str = Field(
        default="demo_cpu_load",
        description="告警关联的指标名称"
    )
    threshold: Optional[float] = Field(
        default=None,
        description="告警阈值"
    )
    starts_at: Optional[str] = Field(
        default=None,
        description="告警开始时间，ISO8601 或 YYYY-MM-DD HH:MM:SS"
    )
    environment: str = Field(
        default="local",
        description="运行环境，例如 local、dev、prod"
    )
    description: Optional[str] = Field(
        default=None,
        description="告警补充描述"
    )
    
    class Config:
        json_schema_extra = {
            "example": {
                "session_id": "session-123",
                "service_name": "demo-service",
                "alert_name": "HighCPUUsage",
                "severity": "critical",
                "metric_name": "demo_cpu_load",
                "threshold": 0.8,
                "environment": "local",
                "description": "demo-service CPU 持续高于阈值"
            }
        }

    def to_diagnosis_task(self) -> str:
        """转换为 Agent 可执行的诊断任务描述。"""
        return (
            "请诊断以下真实 AIOps 告警，并输出带证据链的 Markdown 报告。\n"
            f"- 服务名称: {self.service_name}\n"
            f"- 告警名称: {self.alert_name}\n"
            f"- 告警级别: {self.severity}\n"
            f"- 指标名称: {self.metric_name}\n"
            f"- 阈值: {self.threshold if self.threshold is not None else '未提供'}\n"
            f"- 开始时间: {self.starts_at or '未提供，默认查询最近 30 分钟'}\n"
            f"- 环境: {self.environment}\n"
            f"- 描述: {self.description or '无'}\n\n"
            "必须优先通过 MCP 工具查询 Prometheus 指标、Loki 日志和服务健康状态。"
            "工具参数必须使用上面的真实字段，例如 service_name、metric_name、starts_at，禁止使用占位词。"
            "报告必须包含：告警摘要、证据链、根因判断、置信度、修复方案、待人工确认动作、回滚建议。"
            "如果发现可修复问题，只能提出待确认修复动作，不允许直接执行。"
        )


class AlertInfo(BaseModel):
    """告警信息"""
    alertname: str
    severity: str
    instance: str
    duration: str
    description: Optional[str] = None


class DiagnosisResponse(BaseModel):
    """诊断响应（非流式）"""
    
    code: int = 200
    message: str = "success"
    data: Dict[str, Any]
    
    class Config:
        json_schema_extra = {
            "example": {
                "code": 200,
                "message": "success",
                "data": {
                    "status": "completed",
                    "target_alert": {
                        "alertname": "HighCPUUsage",
                        "severity": "critical"
                    },
                    "diagnosis": {
                        "root_cause": "数据库连接池耗尽",
                        "recommendations": ["扩容数据库连接池", "优化SQL查询"]
                    }
                }
            }
        }


class RemediationExecuteRequest(BaseModel):
    """用户确认后的半自动修复执行请求。"""

    action: str = Field(description="修复动作名称，必须属于白名单")
    service_name: str = Field(default="demo-service", description="目标服务")
    reason: str = Field(description="执行该修复动作的原因")
    approved: bool = Field(default=False, description="用户是否确认执行")
    parameters: Dict[str, Any] = Field(default_factory=dict, description="修复动作参数")
