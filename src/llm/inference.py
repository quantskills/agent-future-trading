import os
import json
import time
import threading
from pathlib import Path
from typing import Dict, Any, Optional
from dataclasses import dataclass, field, fields
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
    max_concurrent_calls: Optional[int] = None
    failure_policy: Dict[str, str] = field(default_factory=dict)
    codex_openai: Dict[str, Any] = field(default_factory=dict)
    tqxai: Dict[str, Any] = field(default_factory=dict)
    deepseek: Dict[str, Any] = field(default_factory=dict)


DEFAULT_FAILURE_POLICY = {
    "parse_error": "retry_then_default",
    "rate_limit": "retry_with_backoff",
    "auth_error": "fail_fast",
    "invalid_request": "fail_fast",
    "server_error": "retry_then_default",
    "unknown": "retry_then_default",
}


_LLM_SEMAPHORES: Dict[tuple[str, str, int], threading.BoundedSemaphore] = {}
_LLM_SEMAPHORES_LOCK = threading.Lock()


def _resolve_llm_semaphore(config: LLMConfig) -> Optional[threading.BoundedSemaphore]:
    """Return a process-local provider/model concurrency limiter when configured."""
    try:
        max_concurrent = int(config.max_concurrent_calls or 0)
    except (TypeError, ValueError):
        max_concurrent = 0
    if max_concurrent <= 0:
        return None

    key = (str(config.provider), str(config.model), max_concurrent)
    with _LLM_SEMAPHORES_LOCK:
        semaphore = _LLM_SEMAPHORES.get(key)
        if semaphore is None:
            semaphore = threading.BoundedSemaphore(max_concurrent)
            _LLM_SEMAPHORES[key] = semaphore
        return semaphore


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
        provider_config = config.codex_openai or {}
        base_url = provider_config.get("base_url") or os.getenv("CODEX_OPENAI_BASE_URL") or model_config.base_url
        return _normalize_openai_compatible_root(base_url) if base_url else None
    if provider == Provider.TQXAI:
        provider_config = config.tqxai or {}
        base_url = (
            provider_config.get("base_url")
            or os.getenv("TQX_LLM_BASE_URL")
            or os.getenv("TQXAI_BASE_URL")
            or os.getenv("ANTHROPIC_BASE_URL")
            or model_config.base_url
        )
        return _normalize_openai_compatible_root(base_url) if base_url else None
    return model_config.base_url


def _resolve_provider_api_key(provider: Provider, model_config, config: LLMConfig) -> str:
    """Resolve API keys with explicit env overrides for compatible gateways."""
    env_candidates = []
    if provider in {Provider.CODEX_OPENAI, Provider.TQXAI}:
        provider_config = (
            (config.codex_openai or {})
            if provider == Provider.CODEX_OPENAI
            else (config.tqxai or {})
        )
        has_explicit_env_config = "api_key_env" in provider_config or "api_key_env_fallbacks" in provider_config
        explicit_env = provider_config.get("api_key_env")
        if explicit_env:
            env_candidates.append(str(explicit_env))
        fallback_envs = provider_config.get("api_key_env_fallbacks") or []
        if isinstance(fallback_envs, str):
            fallback_envs = [fallback_envs]
        for item in fallback_envs:
            if not item:
                continue
            env_candidates.append(str(item))
    else:
        has_explicit_env_config = False

    if model_config.env_key and not has_explicit_env_config:
        env_candidates.append(model_config.env_key)

    seen = set()
    deduped_candidates = []
    for env_key in env_candidates:
        if env_key in seen:
            continue
        seen.add(env_key)
        deduped_candidates.append(env_key)

    for env_key in deduped_candidates:
        api_key = os.getenv(env_key)
        if api_key:
            return api_key

    logger.error(
        "API Key Error: Please set one of "
        f"{', '.join(deduped_candidates) if deduped_candidates else '[no env key configured]'}."
    )
    raise ValueError(
        f"{provider} API key not found. Please set one of "
        f"{', '.join(deduped_candidates) if deduped_candidates else '[no env key configured]'}."
    )


def _build_provider_kwargs(provider: Provider, config: LLMConfig) -> Dict[str, Any]:
    """Build provider-specific kwargs without hard-coding a model choice."""
    if provider in {Provider.CODEX_OPENAI, Provider.TQXAI}:
        provider_config = (
            (config.codex_openai or {})
            if provider == Provider.CODEX_OPENAI
            else (config.tqxai or {})
        )
        reasoning_config = provider_config.get("reasoning") or {}
        effort = provider_config.get("reasoning_effort") or reasoning_config.get("effort")
        return {"extra_body": {"reasoning_effort": effort}} if effort else {}

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


def llm_audit_metadata(raw_config: Dict[str, Any]) -> Dict[str, Any]:
    """Return non-secret LLM routing metadata for logs and artifacts."""
    llm_cfg = _normalize_llm_config(raw_config)
    provider = Provider(llm_cfg.provider)
    model_config = provider.config
    base_url = _resolve_provider_base_url(provider, model_config, llm_cfg)
    metadata: Dict[str, Any] = {
        "provider": provider.value,
        "model": llm_cfg.model,
        "base_url": base_url,
        "api_key_env": None,
        "reasoning_effort": None,
    }
    if provider in {Provider.CODEX_OPENAI, Provider.TQXAI}:
        provider_config = (
            (llm_cfg.codex_openai or {})
            if provider == Provider.CODEX_OPENAI
            else (llm_cfg.tqxai or {})
        )
        reasoning_config = provider_config.get("reasoning") or {}
        metadata["api_key_env"] = provider_config.get("api_key_env") or model_config.env_key
        metadata["reasoning_effort"] = provider_config.get("reasoning_effort") or reasoning_config.get("effort")
    elif provider == Provider.DEEPSEEK:
        metadata["api_key_env"] = model_config.env_key
        metadata["reasoning_effort"] = (llm_cfg.deepseek or {}).get("reasoning_effort")
    else:
        metadata["api_key_env"] = model_config.env_key
    return metadata


def _normalize_llm_config(raw_config: Dict[str, Any]) -> LLMConfig:
    """Ignore removed/legacy provider keys while keeping current config strict."""
    allowed = {item.name for item in fields(LLMConfig)}
    return LLMConfig(**{key: value for key, value in raw_config.items() if key in allowed})


def get_model(config: LLMConfig):
    """Get a model instance based on configuration."""

    _load_llm_env_files()
    provider = Provider(config.provider)
    model_config = provider.config
    base_url = _resolve_provider_base_url(provider, model_config, config)

    if model_config.requires_api_key:
        api_key = _resolve_provider_api_key(provider, model_config, config)
    
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
    llm_cfg = _normalize_llm_config(llm_config)
    llm = get_model(llm_cfg)
    provider = Provider(llm_cfg.provider)
    model_config = provider.config
    audit_metadata = llm_audit_metadata(llm_config)
    semaphore = _resolve_llm_semaphore(llm_cfg)

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
        f"reasoning_effort={audit_metadata.get('reasoning_effort')}, "
        f"base_url={audit_metadata.get('base_url')}, "
        f"api_key_env={audit_metadata.get('api_key_env')}, "
        f"structured_output={structured_method}, max_retries={llm_cfg.max_retries}, "
        f"max_concurrent_calls={llm_cfg.max_concurrent_calls or 'unlimited'}"
    )

    for attempt in range(llm_cfg.max_retries):
        started_at = time.monotonic()
        try:
            if semaphore is not None:
                logger.info(
                    f"LLM concurrency gate entered: provider={provider.value}, "
                    f"model={llm_cfg.model}, max_concurrent_calls={llm_cfg.max_concurrent_calls}"
                )
                with semaphore:
                    result = llm.invoke(prompt)
            else:
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
