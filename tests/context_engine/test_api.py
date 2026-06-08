"""API surface tests via FastAPI TestClient — source, LLM, and DB mocked.

Hermetic: no live network, LLM, or Postgres. We patch the app's startup schema
init and the read/ingest functions the routes call, so /health, /context, and
/refresh exercise the HTTP contract without touching the database.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

import context_engine.app as appmod
from context_engine.read import ContextRead


@pytest.fixture
def client(monkeypatch):
    # Neutralize startup DDL and the db probe so no Postgres is needed.
    monkeypatch.setattr(appmod, "init_context_schema", lambda: None)
    monkeypatch.setattr(appmod, "db_ok", lambda: True)
    with TestClient(appmod.app) as c:
        yield c


def test_health_ok(client):
    r = client.get("/health")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert body["service"] == "context_engine"
    assert body["db"] is True


def test_context_fresh_shape(client, monkeypatch):
    fresh = ContextRead(score=-0.412, rationale="Q4 miss; guidance cut.",
                        as_of="2026-06-08T11:32:14+05:30", half_life_min=90, stale=False)
    monkeypatch.setattr(appmod, "current_context", lambda s: fresh)
    r = client.get("/context/reliance")          # lower-case -> upper-cased server-side
    assert r.status_code == 200
    b = r.json()
    assert b == {
        "symbol": "RELIANCE",
        "score": -0.412,
        "rationale": "Q4 miss; guidance cut.",
        "as_of": "2026-06-08T11:32:14+05:30",
        "half_life_min": 90,
        "stale": False,
    }


def test_context_absent_is_stale_neutral(client, monkeypatch):
    absent = ContextRead(score=0.0, rationale="", as_of=None,
                         half_life_min=None, stale=True)
    monkeypatch.setattr(appmod, "current_context", lambda s: absent)
    r = client.get("/context/INFY")
    assert r.status_code == 200
    b = r.json()
    assert b["stale"] is True
    assert b["score"] == 0.0
    assert b["rationale"] == ""
    assert b["as_of"] is None


def test_context_unknown_symbol_404(client):
    r = client.get("/context/NOTREAL")
    assert r.status_code == 404
    assert r.json()["detail"] == "unknown symbol"


def test_refresh_passes_through(client, monkeypatch):
    from context_engine.ingest import IngestResult

    captured = {}

    def _fake_ingest(symbols=None, *, force=False, source=None):
        captured["symbols"] = symbols
        captured["force"] = force
        return IngestResult(ran_at="2026-06-08T11:30:02+05:30",
                            symbols=symbols or ["RELIANCE"], items_fetched=3,
                            items_new=1, scored=1, skipped_window=False)

    monkeypatch.setattr(appmod, "run_ingest", _fake_ingest)
    r = client.post("/refresh", json={"symbols": ["infy"], "force": True})
    assert r.status_code == 200
    b = r.json()
    assert b["items_new"] == 1
    assert b["scored"] == 1
    assert b["skipped_window"] is False
    assert captured["symbols"] == ["INFY"]   # upper-cased by the route
    assert captured["force"] is True


def test_refresh_default_body(client, monkeypatch):
    from context_engine.ingest import IngestResult

    monkeypatch.setattr(
        appmod, "run_ingest",
        lambda symbols=None, *, force=False, source=None: IngestResult(
            ran_at="x", symbols=["RELIANCE"], skipped_window=True),
    )
    r = client.post("/refresh")
    assert r.status_code == 200
    assert r.json()["skipped_window"] is True
