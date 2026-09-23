"""Application settings for the AIOps control plane."""

from functools import lru_cache
from typing import Any

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    app_name: str = "AIOps Incident Agent"
    app_version: str = "2.0.0"
    environment: str = "local"
    debug: bool = False
    host: str = "0.0.0.0"
    port: int = 9900
    cors_origins: list[str] = Field(default_factory=lambda: ["http://localhost:9900"])

    database_url: str = "sqlite+aiosqlite:///./data/aiops.db"
    database_auto_create: bool = True
    diagnosis_poll_interval_seconds: float = 1.0
    aiops_auto_diagnosis_enabled: bool = True

    jwt_secret: str = "change-this-local-development-secret"
    jwt_algorithm: str = "HS256"
    jwt_expire_minutes: int = 30
    admin_username: str = "admin"
    admin_password: str = ""
    alertmanager_webhook_secret: str = ""

    dashscope_api_key: str = ""
    dashscope_api_base: str = "https://dashscope.aliyuncs.com/compatible-mode/v1"
    dashscope_model: str = "qwen-max"
    dashscope_embedding_model: str = "text-embedding-v4"
    embedding_dimensions: int = 1024
    llm_provider: str = "dashscope"
    llm_model: str = ""
    llm_base_url: str = ""
    llm_api_key: str = ""
    xiaomi_api_key: str = ""
    xiaomi_api_base: str = "https://api.xiaomimimo.com/v1"
    xiaomi_model: str = "mimo-v2-flash"

    rag_top_k: int = 3
    rag_model: str = "qwen-max"
    chunk_max_size: int = 800
    chunk_overlap: int = 100

    prometheus_url: str = "http://localhost:9090"
    loki_url: str = "http://localhost:3100"
    tempo_url: str = "http://localhost:3200"
    merchantflow_health_url: str = "http://localhost:8081/actuator/health"
    merchantflow_ops_url: str = "http://localhost:8081"
    aksk_access_key: str = ""
    aksk_secret_key: str = ""
    observability_timeout_seconds: float = 8.0
    max_query_range_minutes: int = 120
    # 告警进入 firing 通常晚于首条异常日志；只允许有限回溯，避免把历史故障混入本轮诊断。
    diagnosis_pre_alert_lookback_seconds: int = Field(default=120, ge=0, le=300)
    max_query_results: int = 200

    lab_mode: bool = False
    allow_mutations: bool = False
    toxiproxy_url: str = "http://localhost:8474"
    mcp_ops_transport: str = "streamable-http"
    mcp_ops_url: str = "http://localhost:8004/mcp"
    # Docker 环境必须经过 MCP 读取观测证据；单元测试可显式关闭以使用 Fake Registry。
    mcp_ops_enabled: bool = True

    @field_validator("cors_origins", mode="before")
    @classmethod
    def parse_cors_origins(cls, value: Any) -> Any:
        if isinstance(value, str):
            return [item.strip() for item in value.split(",") if item.strip()]
        return value

    @property
    def is_postgres(self) -> bool:
        return self.database_url.startswith(("postgresql", "postgres"))

    @property
    def mcp_servers(self) -> dict[str, dict[str, str]]:
        return {"ops": {"transport": self.mcp_ops_transport, "url": self.mcp_ops_url}}


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()


config = get_settings()
