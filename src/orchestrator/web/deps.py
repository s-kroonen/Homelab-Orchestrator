"""FastAPI DI helpers.

Kept intentionally thin so tests can monkeypatch adapters + sessions without
touching FastAPI machinery.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Annotated

from fastapi import Depends, Request
from sqlmodel import Session

from orchestrator.adapters.factory import AdapterBundle
from orchestrator.config import Settings, get_settings
from orchestrator.db.session import get_engine


def get_settings_dep() -> Settings:
    return get_settings()


def get_session() -> Iterator[Session]:
    session = Session(get_engine())
    try:
        yield session
    finally:
        session.close()


def get_adapters(request: Request) -> AdapterBundle:
    """Fetch the adapters that ``lifespan`` attached to ``app.state``."""
    return request.app.state.adapters  # type: ignore[no-any-return]


SettingsDep = Annotated[Settings, Depends(get_settings_dep)]
SessionDep = Annotated[Session, Depends(get_session)]
AdaptersDep = Annotated[AdapterBundle, Depends(get_adapters)]
