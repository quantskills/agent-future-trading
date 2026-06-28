from dataclasses import dataclass
from enum import Enum
from typing import Optional, Type

from langchain_openai import ChatOpenAI
from langchain_anthropic import ChatAnthropic
from langchain_ollama import ChatOllama
from langchain_fireworks import ChatFireworks
from langchain_core.language_models.chat_models import BaseChatModel

@dataclass
class ModelConfig:
    """Configuration for a model provider"""
    model_class: Type[BaseChatModel]
    env_key: Optional[str] = None
    base_url: Optional[str] = None
    requires_api_key: bool = True
    structured_output_method: str = "function_calling"

class Provider(str, Enum):
    """Supported LLM providers"""
    OPENAI = "OpenAI"
    CODEX_OPENAI = "CodexOpenAI"
    TQXAI = "TQXAI"
    ANTHROPIC = "Anthropic"
    DEEPSEEK = "DeepSeek"
    ALIBABA = "Alibaba"
    ZHIPU = "ZhiPu"
    OLLAMA = "Ollama"
    FIREWORKS= "Fireworks"
    YIZHAN = "YiZhan"
    AIHUBMIX = "AiHubMix"

    @property
    def config(self) -> ModelConfig:
        """Get the configuration for this provider"""
        PROVIDER_CONFIGS = {
            Provider.OPENAI: ModelConfig(
                model_class=ChatOpenAI,
                env_key="OPENAI_API_KEY",
            ),
            Provider.CODEX_OPENAI: ModelConfig(
                model_class=ChatOpenAI,
                base_url="http://47.74.0.65/v1",
                env_key="CODEX_OPENAI_API_KEY",
                structured_output_method="json_mode",
            ),
            Provider.TQXAI: ModelConfig(
                model_class=ChatOpenAI,
                base_url="https://llm.tqx.ai/v1",
                env_key="TQX_LLM_API_KEY",
                structured_output_method="json_mode",
            ),
            Provider.ANTHROPIC: ModelConfig(
                model_class=ChatAnthropic,
                env_key="ANTHROPIC_API_KEY",
            ),
            Provider.DEEPSEEK: ModelConfig(
                model_class=ChatOpenAI,
                base_url="https://api.deepseek.com",
                env_key="DEEPSEEK_API_KEY",
                structured_output_method="json_mode",
            ),
            Provider.ALIBABA: ModelConfig(
                model_class=ChatOpenAI,
                base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
                env_key="QWEN_API_KEY",
            ),
            Provider.ZHIPU: ModelConfig(
                model_class=ChatOpenAI,
                base_url="https://open.bigmodel.cn/api/paas/v4",
                env_key="ZHIPU_API_KEY",
            ),
            Provider.OLLAMA: ModelConfig(
                model_class=ChatOllama,
                requires_api_key=False,
            ),
            Provider.FIREWORKS: ModelConfig(
                model_class=ChatFireworks,
                env_key="FIREWORKS_API_KEY",
            ),
            Provider.YIZHAN: ModelConfig(
                model_class=ChatOpenAI,
                env_key="YIZHAN_API_KEY",
                base_url="https://vip.yi-zhan.top/v1",
            ),
            Provider.AIHUBMIX: ModelConfig(
                model_class=ChatOpenAI,
                env_key="AIHUBMIX_API_KEY",
                base_url="https://api.aihubmix.com/v1",
            ),
        }
        return PROVIDER_CONFIGS[self]
