import os
import json
import time
from pathlib import Path
from typing import Dict, Any, Optional
from dataclasses import dataclass, field
from dotenv import load_dotenv
from pydantic import BaseModel
from llm.provider import Provider
from util.logger import logger

@dataclass
class LLMConfig:
    """Configuration for LLM inference"""
    provider: str
    model: str
    temperature: Optional[float] = 0.5
    max_retries: int = 3
    structured_output_method: Optional[str] = None
    failure_policy: Dict[str, str] = field(default_factory=dict)
    codex_openai: Dict[str, Any] = field(default_factory=dict)
    deepseek: Dict[str, Any] = field(default_factory=dict)
    openrouter: Dict[str, Any] = field(default_factory=dict)


DEFAULT_FAILURE_POLICY = {
    "parse_error": "retry_then_default",
    "rate_limit": "retry_with_backoff",
    "auth_error": "fail_fast",
    "invalid_request": "fail_fast",
    "server_error": "retry_then_default",
    "unknown": "retry_then_default",
}


def _classify_llm_error(exc: Exception) -> str:
    """Classify provider errors into retry-policy buckets."""
    text = str(exc).lower()
    if "output_parsing_failure" in text or "failed to parse" in text or "validation error" in text:
        return "parse_error"
    if "rate limit" in text or "429" in text:
        return "rate_limit"
    if "401" in text or "403" in text or "unauthorized" in text or "forbidden" in text:
        return "auth_error"
    if "400" in text or "invalid_request" in text or "tool_choice" in text or "response_format" in text:
        return "invalid_request"
    if "500" in text or "502" in text or "503" in text or "504" in text:
        return "server_error"
    return "unknown"


def _resolve_failure_policy(config: LLMConfig, error_type: str) -> str:
    policy = {**DEFAULT_FAILURE_POLICY, **(config.failure_policy or {})}
    return policy.get(error_type, DEFAULT_FAILURE_POLICY["unknown"])


def _load_llm_env_files() -> None:
    """Load project-level env files without overriding already exported values."""
    project_root = Path(__file__).resolve().parents[2]
    candidates = []
    explicit_env = os.getenv("AGENTQUANT_ENV_FILE")
    if explicit_env:
        candidates.append(Path(explicit_env))
    candidates.extend(
        [
            project_root / ".env",
            project_root.parent / f"{project_root.name}.env",
            project_root.parent / "Codex.env",
            project_root.parent / "Codex" / f"{project_root.name}.env",
            project_root.parent / "Codex" / "Codex.env",
            Path.cwd() / ".env",
        ]
    )

    seen = set()
    for path in candidates:
        try:
            resolved = path.resolve()
        except OSError:
            continue
        if resolved in seen or not resolved.exists():
            continue
        seen.add(resolved)
        load_dotenv(dotenv_path=resolved, override=False)


def _normalize_openai_compatible_root(base_url: str) -> str:
    base_url = str(base_url).strip().rstrip("/")
    return base_url if base_url.endswith("/v1") else f"{base_url}/v1"


def _resolve_provider_base_url(provider: Provider, model_config, config: LLMConfig) -> Optional[str]:
    """Resolve provider base URLs, including env/config overrides for local gateways."""
    if provider == Provider.CODEX_OPENAI:
        codex_config = config.codex_openai or {}
        base_url = codex_config.get("base_url") or os.getenv("CODEX_OPENAI_BASE_URL") or model_config.base_url
        return _normalize_openai_compatible_root(base_url) if base_url else None
    return model_config.base_url


def _build_provider_kwargs(provider: Provider, config: LLMConfig) -> Dict[str, Any]:
    """Build provider-specific kwargs without hard-coding a model choice."""
    if provider == Provider.OPENROUTER:
        openrouter_config = config.openrouter or {}
        reasoning_config = openrouter_config.get("reasoning") or {}
        extra_body: Dict[str, Any] = {}

        if reasoning_config.get("enabled", False):
            reasoning: Dict[str, Any] = {"enabled": True}
            if reasoning_config.get("effort"):
                reasoning["effort"] = reasoning_config["effort"]
            if reasoning_config.get("max_tokens") is not None:
                reasoning["max_tokens"] = reasoning_config["max_tokens"]
            if reasoning_config.get("exclude") is not None:
                reasoning["exclude"] = bool(reasoning_config["exclude"])
            extra_body["reasoning"] = reasoning

        kwargs: Dict[str, Any] = {}
        if extra_body:
            kwargs["extra_body"] = extra_body

        headers: Dict[str, str] = {}
        if openrouter_config.get("site_url"):
            headers["HTTP-Referer"] = str(openrouter_config["site_url"])
        if openrouter_config.get("app_name"):
            headers["X-Title"] = str(openrouter_config["app_name"])
        if headers:
            kwargs["default_headers"] = headers

        return kwargs

    if provider != Provider.DEEPSEEK:
        return {}

    deepseek_config = config.deepseek or {}
    thinking_config = deepseek_config.get("thinking") or {}
    if not thinking_config.get("enabled", False):
        return {}

    kwargs: Dict[str, Any] = {
        "extra_body": {"thinking": {"type": "enabled"}},
    }
    reasoning_effort = deepseek_config.get("reasoning_effort")
    if reasoning_effort:
        kwargs["reasoning_effort"] = reasoning_effort
    return kwargs


def get_model(config: LLMConfig):
    """Get a model instance based on configuration."""

    _load_llm_env_files()
    provider = Provider(config.provider)
    model_config = provider.config
    base_url = _resolve_provider_base_url(provider, model_config, config)

    if model_config.requires_api_key:
        api_key = os.getenv(model_config.env_key)
        if not api_key:
            logger.error(f"API Key Error: Please make sure {model_config.env_key} is set in your .env file.")
            raise ValueError(f"{provider} API key not found. Please set {model_config.env_key} in .env file.")
    
    kwargs = {
        "model": config.model,
        **({"api_key": api_key} if model_config.requires_api_key else {}),
        **({"base_url": base_url} if base_url else {}),
        **({"temperature": config.temperature} if config.temperature is not None else {}),
        **_build_provider_kwargs(provider, config),
    }
    
    try:
        return model_config.model_class(**kwargs)
    except Exception as e:
        logger.error(f"{provider} Chat Error: {e}")
        raise ValueError(f"{provider} Chat Error: {e}")

def agent_call(prompt: str, llm_config: Dict[str, Any], pydantic_model: BaseModel):
    """
    Makes an agent call with retry logic and structured output.
    
    Args:
        prompt: The prompt to send to the LLM
        llm_config: Configuration for the LLM
        output_model: The Pydantic model to use for structured output
    Returns:
        An instance of output_model (with defaults if error occurs)
    """
    llm_cfg = LLMConfig(**llm_config)
    llm = get_model(llm_cfg)
    provider = Provider(llm_cfg.provider)
    model_config = provider.config

    structured_method = llm_cfg.structured_output_method or model_config.structured_output_method
    if structured_method == "json_mode":
        schema = (
            pydantic_model.model_json_schema()
            if hasattr(pydantic_model, "model_json_schema")
            else pydantic_model.schema()
        )
        prompt = (
            f"{prompt}\n\n"
            "Return only one valid JSON object that matches this schema. "
            "Do not include markdown fences or explanatory text.\n"
            f"{json.dumps(schema, ensure_ascii=False)}"
        )

    llm = llm.with_structured_output(pydantic_model, method=structured_method)
    logger.info(
        f"LLM call configured: provider={provider.value}, model={llm_cfg.model}, "
        f"structured_output={structured_method}, max_retries={llm_cfg.max_retries}"
    )

    for attempt in range(llm_cfg.max_retries):
        started_at = time.monotonic()
        try:
            result = llm.invoke(prompt)
            if result is None:
                raise ValueError("LLM returned None")
            if isinstance(result, dict):
                result = pydantic_model(**result)
            elapsed = time.monotonic() - started_at
            logger.info(
                f"LLM call succeeded: provider={provider.value}, model={llm_cfg.model}, "
                f"attempt={attempt + 1}, elapsed={elapsed:.2f}s"
            )
            return result
        except Exception as e:
            error_type = _classify_llm_error(e)
            action = _resolve_failure_policy(llm_cfg, error_type)
            elapsed = time.monotonic() - started_at
            logger.warning(
                f"Attempt {attempt + 1}/{llm_cfg.max_retries} failed: "
                f"provider={provider.value}, model={llm_cfg.model}, "
                f"error_type={error_type}, policy={action}, elapsed={elapsed:.2f}s, error={e}"
            )
            if action in {"raise", "fail_hard"}:
                logger.error(
                    f"LLM call raising on provider/config error: provider={provider.value}, "
                    f"model={llm_cfg.model}, error_type={error_type}"
                )
                raise RuntimeError(
                    f"LLM call failed: provider={provider.value}, model={llm_cfg.model}, "
                    f"error_type={error_type}, error={e}"
                ) from e
            if action == "fail_fast":
                logger.error(
                    f"LLM call failed fast: provider={provider.value}, model={llm_cfg.model}, "
                    f"error_type={error_type}"
                )
                return pydantic_model()
            if attempt == llm_cfg.max_retries - 1:
                logger.error(f"All {llm_cfg.max_retries} attempts failed")
                return pydantic_model()
            if action == "retry_with_backoff":
                time.sleep(min(2 ** attempt, 8))
    
    return pydantic_model() 
