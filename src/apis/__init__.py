"""Lazy exports for API clients.

Avoid importing provider-specific modules at package import time so futures-only
paths do not eagerly load optional dependencies like pandas/numpy.
"""

from importlib import import_module
from typing import Any

__all__ = ["AlphaVantageAPI", "DataYesAPI", "PandaAIAPI"]


def __getattr__(name: str) -> Any:
    if name == "AlphaVantageAPI":
        return import_module("apis.alphavantage").AlphaVantageAPI
    if name == "DataYesAPI":
        return import_module("apis.datayes").DataYesAPI
    if name == "PandaAIAPI":
        return import_module("apis.pandaai").PandaAIAPI
    raise AttributeError(f"module 'apis' has no attribute {name!r}")
