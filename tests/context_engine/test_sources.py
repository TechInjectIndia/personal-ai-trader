"""yfinance .news shape mapping + dedupe behaviour (no live network)."""

from __future__ import annotations

from context_engine.sources.yfinance_news import YFinanceNewsSource, _extract


def _nested_item():
    # current yfinance shape: fields under item["content"]
    return {
        "content": {
            "title": "Reliance Q4 profit beats estimates",
            "summary": "Refining margins lifted earnings above consensus.",
            "pubDate": "2026-06-08T06:00:00Z",
            "canonicalUrl": {"url": "https://news.example/reliance-q4"},
        }
    }


def _flat_item():
    # legacy flat shape
    return {
        "title": "ICICI guidance cut",
        "summary": "Management trimmed FY guidance.",
        "link": "https://news.example/icici-cut",
        "providerPublishTime": 1_700_000_000,
    }


def test_extract_nested_shape():
    item = _extract("RELIANCE", "yfinance_news", _nested_item())
    assert item is not None
    assert item.headline == "Reliance Q4 profit beats estimates"
    assert "Refining margins" in item.body
    assert item.url == "https://news.example/reliance-q4"
    assert item.published_ts is not None


def test_extract_flat_shape():
    item = _extract("ICICIBANK", "yfinance_news", _flat_item())
    assert item is not None
    assert item.headline == "ICICI guidance cut"
    assert item.url == "https://news.example/icici-cut"


def test_extract_synthesizes_url_when_missing():
    item = _extract("TCS", "yfinance_news", {"content": {"title": "Some TCS headline"}})
    assert item is not None
    assert item.url.startswith("synthetic://")


def test_extract_drops_empty_item():
    assert _extract("INFY", "yfinance_news", {"content": {}}) is None


def test_fetch_maps_and_filters(monkeypatch):
    src = YFinanceNewsSource()

    class _FakeTicker:
        def __init__(self, sym):
            self.news = [_nested_item(), _flat_item(), {"content": {}}]

    class _FakeYF:
        Ticker = _FakeTicker

    monkeypatch.setitem(__import__("sys").modules, "yfinance", _FakeYF)
    items = src.fetch("RELIANCE")
    # two usable items (the empty one is dropped)
    assert len(items) == 2
    urls = {i.url for i in items}
    assert "https://news.example/reliance-q4" in urls


def test_fetch_returns_empty_on_error(monkeypatch):
    src = YFinanceNewsSource()

    class _BoomYF:
        class Ticker:
            def __init__(self, sym):
                raise RuntimeError("rate limited")

    monkeypatch.setitem(__import__("sys").modules, "yfinance", _BoomYF)
    assert src.fetch("RELIANCE") == []
