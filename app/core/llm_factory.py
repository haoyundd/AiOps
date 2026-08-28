"""Lazy OpenAI-compatible model provider selection."""

from dataclasses import dataclass

from langchain_openai import ChatOpenAI
from loguru import logger

from app.config import config


@dataclass
class LLMRuntimeConfig:
    provider: str
    model: str
    base_url: str
    api_key: str


class LLMFactory:
    PROVIDER_LABELS = {
        "dashscope": "阿里 DashScope",
        "xiaomi": "小米 MiMo",
        "custom": "自定义 OpenAI-compatible",
    }

    def __init__(self) -> None:
        self._runtime_config = self._build_config(config.llm_provider or "dashscope")

    def create_chat_model(
        self,
        model: str | None = None,
        temperature: float = 0.2,
        streaming: bool = True,
    ) -> ChatOpenAI:
        runtime = self.get_runtime_config()
        if not runtime.api_key:
            raise RuntimeError(f"LLM API key is not configured for provider={runtime.provider}")
        selected_model = model or runtime.model
        logger.info(
            "creating chat model provider={} model={} streaming={}",
            runtime.provider,
            selected_model,
            streaming,
        )
        return ChatOpenAI(
            model=selected_model,
            temperature=temperature,
            streaming=streaming,
            base_url=runtime.base_url,
            api_key=runtime.api_key,
        )

    def get_runtime_config(self) -> LLMRuntimeConfig:
        return self._runtime_config

    def get_public_config(self) -> dict[str, str]:
        runtime = self.get_runtime_config()
        return {
            "provider": runtime.provider,
            "provider_label": self.PROVIDER_LABELS.get(runtime.provider, runtime.provider),
            "model": runtime.model,
            "base_url": runtime.base_url,
            "api_key_masked": self._mask_key(runtime.api_key),
        }

    def list_options(self) -> dict[str, dict[str, str]]:
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
        model: str | None = None,
        base_url: str | None = None,
        api_key: str | None = None,
    ) -> dict[str, str]:
        provider = provider.strip().lower()
        if provider not in self.PROVIDER_LABELS:
            raise ValueError(f"不支持的模型供应商: {provider}")
        self._runtime_config = self._build_config(
            provider,
            model=model,
            base_url=base_url,
            api_key=api_key,
            fallback=self._runtime_config,
        )
        return self.get_public_config()

    def _build_config(
        self,
        provider: str,
        *,
        model: str | None = None,
        base_url: str | None = None,
        api_key: str | None = None,
        fallback: LLMRuntimeConfig | None = None,
    ) -> LLMRuntimeConfig:
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
                or self._same_provider_key(provider, fallback),
            )
        return LLMRuntimeConfig(
            provider=provider,
            model=model or config.llm_model or (fallback.model if fallback else config.rag_model),
            base_url=base_url
            or config.llm_base_url
            or (fallback.base_url if fallback and fallback.provider == provider else ""),
            api_key=api_key or config.llm_api_key or self._same_provider_key(provider, fallback),
        )

    @staticmethod
    def _same_provider_key(provider: str, fallback: LLMRuntimeConfig | None) -> str:
        return fallback.api_key if fallback and fallback.provider == provider else ""

    @staticmethod
    def _mask_key(api_key: str) -> str:
        if not api_key:
            return ""
        if len(api_key) <= 8:
            return "***"
        return f"{api_key[:4]}...{api_key[-4:]}"


llm_factory = LLMFactory()
