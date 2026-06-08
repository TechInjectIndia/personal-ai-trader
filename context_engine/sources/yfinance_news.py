"""Free yfinance .news ContextSource.

Reads ``yf.Ticker(f"{symbol}.NS").news``. In current yfinance the useful
fields are NESTED under ``item["content"]`` (title / summary / pubDate /
canonicalUrl / clickThroughUrl), NOT flat at the top level — a flat-key
assumption silently yields empty text. We defensively read both the nested
and (legacy) flat shapes so the source survives a yfinance schema flip.

This is unauthenticated Yahoo scraping: it rate-limits and returns empty lists
intermittently. That risk is owned entirely here, behind the ContextSource
ABC. ``fetch`` NEVER raises — on any error it returns ``[]`` so ingestion
no-ops cleanly and the bot (which fails open) keeps trading regardless.
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone

from context_engine.sources.base import ContextItem, ContextSource


def _parse_pub_ts(value: object) -> datetime | None:
    """Parse a yfinance pubDate into an aware datetime, or None.

    Handles two shapes seen across yfinance versions: an ISO-8601 string
    (nested ``content.pubDate``) and a unix epoch int (legacy flat
    ``providerPublishTime``).
    """
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(float(value), tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    if isinstance(value, str):
        try:
            # yfinance ISO strings end in 'Z'; fromisoformat handles +00:00.
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    return None


def _synth_url(symbol: str, source_name: str, headline: str) -> str:
    """Stable synthetic dedupe key when the feed has no canonical link."""
    h = hashlib.sha1(f"{source_name}:{headline}".encode("utf-8")).hexdigest()[:16]
    return f"synthetic://{source_name}/{symbol}/{h}"


def _extract(symbol: str, source_name: str, raw: dict) -> ContextItem | None:
    """Map one raw yfinance news dict to a ContextItem, or None if unusable."""
    content = raw.get("content") if isinstance(raw.get("content"), dict) else {}

    headline = (content.get("title") or raw.get("title") or "").strip()
    body = (content.get("summary") or content.get("description")
            or raw.get("summary") or "").strip()

    # canonicalUrl/clickThroughUrl are nested dicts {"url": "..."} in current
    # yfinance; link is the legacy flat key.
    url = ""
    for key in ("canonicalUrl", "clickThroughUrl"):
        node = content.get(key)
        if isinstance(node, dict) and node.get("url"):
            url = str(node["url"]).strip()
            break
    if not url:
        url = str(raw.get("link") or "").strip()

    published_ts = _parse_pub_ts(content.get("pubDate") or raw.get("providerPublishTime"))

    if not headline and not body:
        return None  # nothing useful to score
    if not url:
        url = _synth_url(symbol, source_name, headline or body[:80])

    return ContextItem(
        symbol=symbol,
        source=source_name,
        url=url,
        headline=headline,
        body=body,
        published_ts=published_ts,
    )


class YFinanceNewsSource(ContextSource):
    """yfinance .news adapter (free). Black-box per the WORKAROUNDS ethos."""

    name = "yfinance_news"

    def __init__(self, suffix: str = ".NS") -> None:
        self.suffix = suffix

    def fetch(self, symbol: str, since: datetime | None = None) -> list[ContextItem]:
        try:
            import yfinance as yf

            ticker = yf.Ticker(f"{symbol}{self.suffix}")
            raw_items = ticker.news or []
        except Exception:
            # Yahoo scraping is flaky (rate-limit / transport / schema). Degrade
            # to no items so the ingest cycle no-ops cleanly for this symbol.
            return []

        out: list[ContextItem] = []
        for raw in raw_items:
            if not isinstance(raw, dict):
                continue
            item = _extract(symbol, self.name, raw)
            if item is None:
                continue
            if since is not None and item.published_ts is not None \
                    and item.published_ts < since:
                continue
            out.append(item)
        return out
