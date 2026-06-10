"""统一聊天模型工厂。

所有对话和 AIOps 推理都从这里创建模型，避免业务模块直接绑定某个供应商。
"""

from dataclasses import dataclass
from typing import Dict, Optional

from langchain_openai import ChatOpenAI
from loguru import logger

from app.config import config


@dataclass
class LLMRuntimeConfig:
    """运行时聊天模型配置。"""

    provider: str
    model: str
    base_url: str
    api_key: str


class LLMFactory:
    """LLM 工厂，支持 OpenAI-compatible 模型供应商切换。"""

    PROVIDER_LABELS = {
        "dashscope": "阿里 DashScope",
        "xiaomi": "小米 MiMo",
        "custom": "自定义 OpenAI-compatible",
    }

    def __init__(self) -> None:
        """初始化运行时配置，默认从环境变量和 `.env` 读取。"""
        self._runtime_config = self._build_config_from_settings()

    def create_chat_model(
        self,
        model: Optional[str] = None,
        temperature: float = 0.7,
        streaming: bool = True,
    ) -> ChatOpenAI:
        """创建聊天模型实例。"""
        runtime = self.get_runtime_config()
        selected_model = model or runtime.model
        effective_api_key = runtime.api_key
        if not effective_api_key or effective_api_key == "your-api-key-here":
            # 允许服务先正常启动；真正发起模型调用时，供应商会返回清晰的鉴权错误。
            # 这样前端、健康检查、incident 面板不会因为暂时没配 key 而整体不可用。
            logger.warning(f"模型 provider={runtime.provider} 暂未配置 API Key，调用模型时会失败")
            effective_api_key = "missing-api-key"

        logger.info(
            f"创建聊天模型 provider={runtime.provider}, model={selected_model}, "
            f"base_url={runtime.base_url}, streaming={streaming}"
        )
        return ChatOpenAI(
            model=selected_model,
            temperature=temperature,
            streaming=streaming,
            base_url=runtime.base_url,
            api_key=effective_api_key,
        )

    def get_runtime_config(self) -> LLMRuntimeConfig:
        """返回当前运行时模型配置。"""
        return self._runtime_config

    def get_public_config(self) -> Dict[str, str]:
        """返回可展示给前端的模型配置，API Key 只展示掩码。"""
        runtime = self.get_runtime_config()
        return {
            "provider": runtime.provider,
            "provider_label": self.PROVIDER_LABELS.get(runtime.provider, runtime.provider),
            "model": runtime.model,
            "base_url": runtime.base_url,
            "api_key_masked": self._mask_key(runtime.api_key),
        }

    def list_options(self) -> Dict[str, Dict[str, str]]:
        """返回内置模型供应商选项。"""
        return {
            "dashscope": {
                "label": self.PROVIDER_LABELS["dashscope"],
                "model": config.dashscope_model,
                "base_url": config.dashscope_api_base,
            },
            "xiaomi": {
                "label": self.PROVIDER_LABELS["xiaomi"],
                "model": config.xiaomi_model,
                "base_url": config.xiaomi_api_base,
            },
            "custom": {
                "label": self.PROVIDER_LABELS["custom"],
                "model": config.llm_model or config.rag_model,
                "base_url": config.llm_base_url,
            },
        }

    def switch_provider(
        self,
        provider: str,
        model: Optional[str] = None,
        base_url: Optional[str] = None,
        api_key: Optional[str] = None,
    ) -> Dict[str, str]:
        """切换运行时模型供应商。"""
        normalized_provider = provider.strip().lower()
        if normalized_provider not in self.PROVIDER_LABELS:
            raise ValueError(f"不支持的模型供应商: {provider}")

        old_config = self._runtime_config
        new_config = self._build_config(
            provider=normalized_provider,
            model=model,
            base_url=base_url,
            api_key=api_key,
            fallback=old_config,
        )
        self._runtime_config = new_config
        logger.info(
            f"模型供应商已切换: {old_config.provider}/{old_config.model} "
            f"-> {new_config.provider}/{new_config.model}"
        )
        return self.get_public_config()

    def _build_config_from_settings(self) -> LLMRuntimeConfig:
        """从 Settings 创建初始模型配置。"""
        provider = (config.llm_provider or "dashscope").strip().lower()
        return self._build_config(provider=provider)

    def _build_config(
        self,
        provider: str,
        model: Optional[str] = None,
        base_url: Optional[str] = None,
        api_key: Optional[str] = None,
        fallback: Optional[LLMRuntimeConfig] = None,
    ) -> LLMRuntimeConfig:
        """根据供应商名称解析实际模型配置。"""
        if provider == "dashscope":
            return LLMRuntimeConfig(
                provider=provider,
                model=model or config.llm_model or config.rag_model or config.dashscope_model,
                base_url=base_url or config.llm_base_url or config.dashscope_api_base,
                api_key=api_key or config.llm_api_key or config.dashscope_api_key,
            )
        if provider == "xiaomi":
            return LLMRuntimeConfig(
                provider=provider,
                model=model or config.llm_model or config.xiaomi_model,
                base_url=base_url or config.llm_base_url or config.xiaomi_api_base,
                api_key=api_key
                or config.llm_api_key
                or config.xiaomi_api_key
                or self._fallback_api_key(provider, fallback),
            )
        return LLMRuntimeConfig(
            provider=provider,
            model=model or config.llm_model or (fallback.model if fallback else config.rag_model),
            base_url=base_url or config.llm_base_url or (fallback.base_url if fallback else ""),
            api_key=api_key or config.llm_api_key or self._fallback_api_key(provider, fallback),
        )

    @staticmethod
    def _fallback_api_key(provider: str, fallback: Optional[LLMRuntimeConfig]) -> str:
        """只在同一供应商内复用旧 Key，避免把小米 Key 误发给自定义服务。"""
        if fallback and fallback.provider == provider:
            return fallback.api_key
        return ""

    @staticmethod
    def _mask_key(api_key: str) -> str:
        """掩码 API Key，防止前端和日志泄露完整密钥。"""
        if not api_key:
            return ""
        if len(api_key) <= 8:
            return "***"
        return f"{api_key[:4]}...{api_key[-4:]}"


llm_factory = LLMFactory()
