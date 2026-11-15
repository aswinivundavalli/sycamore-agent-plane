"""Look up agent adapters by name."""

from __future__ import annotations

from .base import Adapter
from .claude import ClaudeAdapter
from .fake import FakeAdapter

_ADAPTERS: dict[str, type[Adapter]] = {
    ClaudeAdapter.name: ClaudeAdapter,
    FakeAdapter.name: FakeAdapter,
}
ADAPTER_NAMES: list[str] = list(_ADAPTERS)


def get_adapter(name: str) -> Adapter:
    try:
        return _ADAPTERS[name]()
    except KeyError:
        raise KeyError(f"unknown agent {name!r}; known: {', '.join(ADAPTER_NAMES)}") from None
