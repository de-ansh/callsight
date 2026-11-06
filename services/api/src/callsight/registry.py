"""A tiny plugin registry: providers and analyzers register themselves by name."""

from __future__ import annotations

import importlib
from typing import Any, Callable

KINDS = ("asr", "llm", "analyzer")
_REGISTRY: dict[str, dict[str, Callable[..., Any]]] = {kind: {} for kind in KINDS}


def register(kind: str, name: str) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    if kind not in _REGISTRY:
        raise ValueError(f"unknown plugin kind '{kind}'. expected one of {KINDS}")

    def decorator(factory: Callable[..., Any]) -> Callable[..., Any]:
        _REGISTRY[kind][name] = factory
        return factory

    return decorator


def create(kind: str, name: str, **kwargs: Any) -> Any:
    try:
        factory = _REGISTRY[kind][name]
    except KeyError:
        raise KeyError(f"unknown {kind} '{name}'. available: {available(kind)}") from None
    return factory(**kwargs)


def available(kind: str) -> list[str]:
    return sorted(_REGISTRY[kind])


def load_builtins() -> None:
    """Import the built-in plugin packages so they register themselves."""
    for module in ("callsight.analyzers", "callsight.providers"):
        importlib.import_module(module)
