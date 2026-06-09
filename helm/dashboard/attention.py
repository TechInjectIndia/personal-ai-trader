"""Action Center — the single source of "what needs the human's attention".

The dashboard is read-only and the self-improvement loop decides autonomously, so
the only things that should ever reach the human are: (a) state that needs a
*judgment call the loop is forbidden to make* (graduating a review-pending
feature), (b) an inconsistent/unsafe config (a review-pending flag running live
without evidence), or (c) an explicit ask pushed by a script or operator.

Two sources, merged + severity-sorted by :func:`attention_items`:

* **computed** — cheap live-state checks (autonomy paused, F5 evidence gate).
* **queued**   — a JSON list under the ``attention_queue`` setting that any
  script (or a human at the psql prompt) can push to via :func:`enqueue` so an
  arbitrary "do this" item shows up as a banner with steps. Cleared with
  :func:`dismiss`.

Every check is wrapped fail-soft: a broken probe yields no item rather than
taking down the Overview. Severity order: ``action`` > ``warn`` > ``info``.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from helm.data.store import conn, get_setting, set_setting

REAL = "pt.competitor_id IS NOT NULL AND left(pt.competitor_id, 3) <> 'zzz'"

# F5 (conviction sizing) graduates only on a monotone confidence->outcome
# relationship over this many conf-tagged closed TAKEs. Mirrors REVIEW.md /
# docs/frd/F5-conviction-weighted-sizing.md.
F5_MIN_TRADES = 50

_LEVEL_RANK = {"action": 0, "warn": 1, "info": 2}
_QUEUE_KEY = "attention_queue"


@dataclass
class AttentionItem:
    """One thing the human should see. `where` = where to go; `steps` = how."""

    level: str  # action | warn | info
    title: str
    detail: str
    where: str = ""
    steps: list[str] = field(default_factory=list)
    key: str = ""  # stable id (used to dismiss queued items)

    @property
    def rank(self) -> int:
        return _LEVEL_RANK.get(self.level, 99)


def _truthy(v: object) -> bool:
    return v in (True, "true", "True", 1, "1")


# ───────────────────────── computed checks ─────────────────────────


def _autonomy_item() -> AttentionItem | None:
    if not _truthy(get_setting("autonomy_paused")):
        return None
    return AttentionItem(
        level="warn",
        title="Self-improvement loop is paused",
        detail="The PM → Engineer → Tester loop will not ship or verify any "
        "changes while paused. No trades are affected.",
        where="⚙️ Settings → Feature flags",
        steps=["Untick “Pause self-improvement loop” when you want it to resume."],
        key="autonomy_paused",
    )


def _f5_evidence() -> tuple[int, bool]:
    """(#conf-tagged closed TAKEs, monotone win%↑ across buckets) since inception.

    Confidence is parsed from ``decisions.reasoning`` (``conf=0.xx``) exactly as
    the review digest's "F5 gate" block does, so the banner and the digest agree.
    """
    with conn() as c:
        rows = list(c.execute(f"""
            WITH d AS (
              SELECT (regexp_match(de.reasoning,'conf=([0-9.]+)'))[1]::numeric conf,
                     pt.net_pnl_inr
              FROM decisions de JOIN paper_trades pt ON pt.decision_id=de.id
              WHERE de.verdict='TAKE' AND pt.status='CLOSED'
                AND de.reasoning ~ 'conf=' AND {REAL})
            SELECT CASE WHEN conf<0.5 THEN 1 WHEN conf<0.7 THEN 2
                        WHEN conf<0.85 THEN 3 ELSE 4 END bucket,
                   count(*) n,
                   round(100.0*sum((net_pnl_inr>0)::int)/count(*),1) win_pct
            FROM d GROUP BY 1 ORDER BY 1
        """))
    n = sum(int(r["n"]) for r in rows)
    wins = [float(r["win_pct"]) for r in rows]
    monotone = len(wins) >= 2 and all(b >= a for a, b in zip(wins, wins[1:]))
    return n, monotone


def _f5_item() -> AttentionItem | None:
    on = _truthy(get_setting("CONVICTION_SIZING_ENABLED"))
    n, monotone = _f5_evidence()

    if on and not (n >= F5_MIN_TRADES and monotone):
        return AttentionItem(
            level="warn",
            title="F5 conviction sizing is LIVE but unproven",
            detail=f"It is scaling position size on decider confidence, but only "
            f"{n}/{F5_MIN_TRADES} evidence trades have closed since the baseline "
            f"reset and the confidence→outcome relationship is not yet monotone. "
            f"This adds variance without proven edge.",
            where="⚙️ Settings → Feature flags",
            steps=["Untick “Conviction sizing (F5)” to revert to flat sizing, "
                   "or confirm you want it on early."],
            key="f5_unproven_on",
        )

    if (not on) and n >= F5_MIN_TRADES and monotone:
        return AttentionItem(
            level="action",
            title="F5 conviction sizing is ready for your go/no-go",
            detail=f"Decider confidence now predicts outcome monotonically over "
            f"{n} closed TAKEs (≥{F5_MIN_TRADES} bar met). This is the evidence "
            f"gate from REVIEW.md — the loop won't flip this flag, it's your call.",
            where="🔁 Self-Improvement → Live experiments",
            steps=[
                "Review the “F5 gate” block (win% rising across confidence buckets).",
                "If you agree, enable “Conviction sizing (F5)” on ⚙️ Settings.",
            ],
            key="f5_ready",
        )
    return None


_COMPUTED = (_autonomy_item, _f5_item)


def _computed_items() -> list[AttentionItem]:
    out: list[AttentionItem] = []
    for probe in _COMPUTED:
        try:
            item = probe()
        except Exception:  # a broken probe must never take down the Overview
            continue
        if item:
            out.append(item)
    return out


# ───────────────────────── queued (push) items ─────────────────────────


def _queued_items() -> list[AttentionItem]:
    raw = get_setting(_QUEUE_KEY)
    if not isinstance(raw, list):
        return []
    out: list[AttentionItem] = []
    for d in raw:
        if not isinstance(d, dict) or not d.get("title"):
            continue
        out.append(AttentionItem(
            level=str(d.get("level", "info")),
            title=str(d["title"]),
            detail=str(d.get("detail", "")),
            where=str(d.get("where", "")),
            steps=[str(s) for s in d.get("steps", []) if s],
            key=str(d.get("key", "")),
        ))
    return out


def enqueue(key: str, title: str, detail: str = "", *, level: str = "info",
            where: str = "", steps: list[str] | None = None,
            actor: str = "system") -> None:
    """Push (or replace, by `key`) an item onto the human action queue."""
    raw = get_setting(_QUEUE_KEY)
    queue = [d for d in raw if isinstance(d, dict) and d.get("key") != key] \
        if isinstance(raw, list) else []
    queue.append({
        "key": key, "title": title, "detail": detail,
        "level": level, "where": where, "steps": steps or [],
    })
    set_setting(_QUEUE_KEY, queue, actor=actor)


def dismiss(key: str, actor: str = "dashboard") -> None:
    """Remove a queued item by key (no-op if absent)."""
    raw = get_setting(_QUEUE_KEY)
    if not isinstance(raw, list):
        return
    set_setting(_QUEUE_KEY, [d for d in raw if isinstance(d, dict)
                             and d.get("key") != key], actor=actor)


# ───────────────────────── public API ─────────────────────────


def attention_items() -> list[AttentionItem]:
    """All current attention items, severity-sorted (action → warn → info).

    Queued items are de-duplicated against computed ones by `key` so a script
    can't double-post something the live checks already surface.
    """
    computed = _computed_items()
    seen = {i.key for i in computed if i.key}
    merged = computed + [i for i in _queued_items() if i.key not in seen]
    return sorted(merged, key=lambda i: (i.rank, i.title))
