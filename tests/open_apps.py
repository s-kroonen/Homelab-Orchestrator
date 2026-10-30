"""Test apps started without a ``with`` block, shut down when their test ends (``conftest.py``).

A ``TestClient`` that is entered and never left keeps its app running in its own thread for the
rest of the run, schedulers and all, writing to whatever database the next test uses.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

OPEN: list[TestClient] = []


def keep(client: TestClient) -> TestClient:
    """Enter ``client`` and have it shut down when the test ends."""
    client.__enter__()
    OPEN.append(client)
    return client


def close_all() -> None:
    while OPEN:
        OPEN.pop().__exit__(None, None, None)
