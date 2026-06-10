"""模型供应商切换测试。"""

import pytest

from app.core.llm_factory import LLMFactory


def test_switch_to_xiaomi_returns_masked_public_config():
    """切到小米后，前端只能看到掩码 Key，不能拿到完整密钥。"""
    factory = LLMFactory()

    current = factory.switch_provider(
        provider="xiaomi",
        model="mimo-v2-flash",
        base_url="https://api.xiaomimimo.com/v1",
        api_key="sk-test-123456",
    )

    assert current["provider"] == "xiaomi"
    assert current["provider_label"] == "小米 MiMo"
    assert current["model"] == "mimo-v2-flash"
    assert current["base_url"] == "https://api.xiaomimimo.com/v1"
    assert current["api_key_masked"] == "sk-t...3456"


def test_switch_to_custom_does_not_reuse_xiaomi_api_key():
    """切到自定义服务时，不复用小米 Key，避免密钥发错供应商。"""
    factory = LLMFactory()
    factory.switch_provider(
        provider="xiaomi",
        model="mimo-v2-flash",
        base_url="https://api.xiaomimimo.com/v1",
        api_key="sk-xiaomi-secret",
    )

    factory.switch_provider(
        provider="custom",
        model="local-model",
        base_url="http://localhost:11434/v1",
    )
    runtime = factory.get_runtime_config()

    assert runtime.provider == "custom"
    assert runtime.model == "local-model"
    assert runtime.base_url == "http://localhost:11434/v1"
    assert runtime.api_key == ""


def test_switch_unknown_provider_rejected():
    """不认识的供应商直接拒绝，避免模型路由进入不可预期状态。"""
    factory = LLMFactory()

    with pytest.raises(ValueError):
        factory.switch_provider(provider="unknown")
