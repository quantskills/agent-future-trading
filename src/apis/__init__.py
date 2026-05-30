"""Lazy exports for AgentQuant API clients."""

from importlib import import_module
from typing import Any

__all__ = ["PandaAIAPI"]


def __getattr__(name: str) -> Any:
    if name == "PandaAIAPI":
        return import_module("apis.pandaai").PandaAIAPI
    raise AttributeError(f"module 'apis' has no attribute {name!r}")
