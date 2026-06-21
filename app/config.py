"""配置管理模块

使用 Pydantic Settings 实现类型安全的配置管理
"""

from typing import Dict, Any
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """应用配置"""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,#表示大小写不敏感
        extra="ignore",
    )

    # 应用配置
    app_name: str = "SuperBizAgent"
    app_version: str = "1.0.0"
    debug: bool = False
    host: str = "0.0.0.0"
    port: int = 9900

    # DashScope 配置
    dashscope_api_key: str = ""  # 默认空字符串，实际使用需从环境变量加载
    dashscope_api_base: str = "https://dashscope.aliyuncs.com/compatible-mode/v1"
    dashscope_model: str = "qwen-max"
    dashscope_embedding_model: str = "text-embedding-v4"  # v4 支持多种维度（默认 1024）

    # 聊天模型配置：只影响对话和 AIOps 推理，不影响 Milvus embedding 维度。
    llm_provider: str = "xiaomi"
    llm_model: str = ""
    llm_base_url: str = ""
    llm_api_key: str = ""

    # 小米 MiMo OpenAI-compatible 配置，可通过环境变量覆盖真实地址和模型名。
    xiaomi_api_key: str = ""
    xiaomi_api_base: str = "https://api.xiaomimimo.com/v1"
    xiaomi_model: str = "mimo-v2-flash"

    # AIOps 自动诊断：默认收到真实告警后自动启动 Agent。
    # 如需控制成本或做手动演示，可通过环境变量改为 false。
    aiops_auto_diagnosis_enabled: bool = True

    # Milvus 配置
    milvus_host: str = "localhost"
    milvus_port: int = 19530
    milvus_timeout: int = 10000  # 毫秒

    # RAG 配置
    rag_top_k: int = 3
    rag_model: str = "qwen-max"  # 使用快速响应模型，不带扩展思考

    # 文档分块配置
    chunk_max_size: int = 800
    chunk_overlap: int = 100

    # MCP 服务配置
    mcp_cls_transport: str = "streamable-http"
    mcp_cls_url: str = "http://localhost:8003/mcp"
    mcp_monitor_transport: str = "streamable-http"
    mcp_monitor_url: str = "http://localhost:8004/mcp"
    mcp_remediation_transport: str = "streamable-http"
    mcp_remediation_url: str = "http://localhost:8005/mcp"

    # 真实观测栈配置
    prometheus_url: str = "http://localhost:9090"
    loki_url: str = "http://localhost:3100"
    demo_service_url: str = "http://localhost:9910"

    # Harness 持久化数据库。默认使用本地 SQLite，便于演示和面试复现。
    database_url: str = "sqlite:///./volumes/aiops_harness.db"

    @property
    def mcp_servers(self) -> Dict[str, Dict[str, Any]]:
        """获取完整的 MCP 服务器配置"""
        return {
            "cls": {
                "transport": self.mcp_cls_transport,
                "url": self.mcp_cls_url,
            },
            "monitor": {
                "transport": self.mcp_monitor_transport,
                "url": self.mcp_monitor_url,
            },
            "remediation": {
                "transport": self.mcp_remediation_transport,
                "url": self.mcp_remediation_url,
            }
        }


# 全局配置实例
config = Settings()
