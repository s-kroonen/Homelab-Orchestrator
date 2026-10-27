"""An adapter that can be replaced while everything holding it keeps working.

Pipelines, schedulers and collectors are handed ``adapters.proxmox`` once, at startup. When a
connection is edited the adapter behind it has to change without restarting the process, so the
bundle hands out a :class:`Switchable` instead: it forwards every attribute and call to the
adapter currently behind it, and :meth:`swap` points it at a new one.
"""

from __future__ import annotations

from typing import Any


class Switchable:
    __slots__ = ("_target",)

    def __init__(self, target: Any) -> None:
        object.__setattr__(self, "_target", target)

    @property
    def target(self) -> Any:
        return object.__getattribute__(self, "_target")

    def swap(self, new: Any) -> Any:
        """Point at ``new``; returns the adapter that was behind it (the caller stops it)."""
        old = self.target
        object.__setattr__(self, "_target", new)
        return old

    def __getattr__(self, name: str) -> Any:
        return getattr(self.target, name)

    def __setattr__(self, name: str, value: Any) -> None:
        setattr(self.target, name, value)

    # isinstance(bundle.proxy, DryRunProxyAdapter) must still answer for what is behind it.
    @property  # type: ignore[misc]
    def __class__(self) -> type:
        return type(self.target)

    def __repr__(self) -> str:
        return f"<Switchable -> {self.target!r}>"
