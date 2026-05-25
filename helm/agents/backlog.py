"""
Backlog clustering for the PM "drain the backlog" path.

The retro pipeline tends to emit near-duplicate `improvement_proposals`: the
same theme ("widen ORB stop on choppy mornings") surfaces from many separate
trades, so the house backlog accumulates ~hundreds of status='open' rows that
are mostly restatements of a few dozen distinct ideas. The weekly PM review
clusters only by an exact loose-title key (`pm._cluster_key`), which leaves a
long tail of phrasing variants un-collapsed.

This module clusters more aggressively — Jaccard token overlap on a normalized
`title + proposed_change` — so the PM backlog mode can feed the LLM a tractable
shortlist of distinct ideas instead of 228 rows. It is a PURE module: the only
DB access is the loader `cluster_open_proposals`; the clustering/ranking math is
factored into stdlib-only pure functions (`cluster_proposals`,
`_jaccard`, `_normalize`) that the unit tests exercise without a live DB.

No new dependencies — stdlib only (re, itertools via simple loops).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from helm.config import HOUSE_COMPETITOR_ID, HOUSE_TRADE_FILTER
from helm.data.store import conn

# Jaccard token-overlap threshold above which two proposals (within the same
# category) are considered near-duplicates and merged into one cluster. 0.6 is
# deliberately loose enough to fold phrasing variants of the same idea together
# but tight enough that two genuinely different ideas in the same category stay
# apart. Tune here; the unit tests pin the qualitative behaviour, not the value.
DEFAULT_SIMILARITY_THRESHOLD = 0.6

# Tiny stop-word list so high-frequency filler ("the", "a", "to") doesn't
# inflate the overlap between otherwise-unrelated proposals. Kept minimal and
# domain-agnostic on purpose.
_STOPWORDS = frozenset({
    "the", "a", "an", "to", "of", "and", "or", "in", "on", "for", "with",
    "is", "are", "be", "this", "that", "it", "as", "at", "by", "we", "our",
    "should", "when", "if", "than", "then", "into", "from", "up", "out",
})

_TOKEN_RE = re.compile(r"[a-z0-9]+")


def _normalize(text: str) -> frozenset[str]:
    """Lowercase, tokenize to alphanumeric words, drop stop-words.

    Returns a frozenset of tokens (the bag is a set, so Jaccard is well
    defined). Empty / None text → empty set.
    """
    if not text:
        return frozenset()
    toks = _TOKEN_RE.findall(text.lower())
    return frozenset(t for t in toks if t not in _STOPWORDS and len(t) > 1)


def _jaccard(a: frozenset[str], b: frozenset[str]) -> float:
    """Jaccard similarity |A∩B| / |A∪B|. Two empty sets → 0.0 (no signal)."""
    if not a and not b:
        return 0.0
    inter = len(a & b)
    if inter == 0:
        return 0.0
    union = len(a | b)
    return inter / union


@dataclass
class ProposalCluster:
    """One group of near-duplicate open proposals within a single category.

    `representative` is the proposal the PM should act on (highest confidence;
    ties broken by most recent). `member_ids` includes the representative.
    `recurrence` = number of members. `score` = recurrence × representative
    confidence — the same recurrence-beats-novelty weighting the PM prompt uses.
    """
    category: str
    representative: dict
    members: list[dict]
    member_ids: list[int] = field(default_factory=list)

    @property
    def recurrence(self) -> int:
        return len(self.members)

    @property
    def confidence(self) -> int:
        c = self.representative.get("confidence")
        return int(c) if c is not None else 0

    @property
    def score(self) -> int:
        return self.recurrence * self.confidence

    @property
    def representative_id(self) -> int:
        return int(self.representative["id"])

    def to_prompt_dict(self) -> dict:
        """Compact view fed to the PM LLM — the representative plus the cluster
        metadata (recurrence is the headline signal)."""
        rep = self.representative
        return {
            "category": self.category,
            "representative_id": self.representative_id,
            "recurrence": self.recurrence,
            "confidence": self.confidence,
            "score": self.score,
            "member_ids": self.member_ids,
            "title": rep.get("title"),
            "rationale": rep.get("rationale"),
            "proposed_change": rep.get("proposed_change"),
            "evidence": rep.get("evidence"),
        }


def _representative(members: list[dict]) -> dict:
    """Pick the cluster head: highest confidence, ties → most recent.

    `created_ts` may be a datetime, a preformatted string, or absent; we sort
    on (confidence, created_ts) using a tuple that degrades gracefully.
    """
    def key(p: dict) -> tuple:
        conf = p.get("confidence")
        conf = int(conf) if conf is not None else 0
        # str() so heterogeneous created_ts types still compare; ISO-ish
        # timestamps sort lexically the same as chronologically.
        return (conf, str(p.get("created_ts") or ""))

    return max(members, key=key)


def cluster_proposals(
    proposals: list[dict],
    *,
    threshold: float = DEFAULT_SIMILARITY_THRESHOLD,
) -> list[ProposalCluster]:
    """Pure clustering + ranking. No DB.

    Groups near-duplicates by single-link agglomeration on Jaccard token
    overlap of normalized (title + " " + proposed_change), but ONLY within the
    same category — a 'risk' idea never merges with a 'sizing' idea even if the
    wording rhymes. Each input proposal must carry: id, category, title,
    proposed_change, confidence, created_ts (extra keys are passed through).

    Returns clusters ranked by score = recurrence × representative.confidence,
    descending; ties broken by recurrence desc, then representative id asc for a
    stable order.
    """
    # Precompute token sets once.
    enriched: list[tuple[dict, frozenset[str]]] = []
    for p in proposals:
        blob = f"{p.get('title') or ''} {p.get('proposed_change') or ''}"
        enriched.append((p, _normalize(blob)))

    # Bucket by category first — clustering only happens within a category.
    by_cat: dict[str, list[tuple[dict, frozenset[str]]]] = {}
    for item in enriched:
        by_cat.setdefault(item[0].get("category") or "", []).append(item)

    clusters: list[ProposalCluster] = []
    for category, items in by_cat.items():
        # Single-link union-find: i and j merge if similarity >= threshold.
        n = len(items)
        parent = list(range(n))

        def find(x: int, _parent: list[int] = parent) -> int:
            while _parent[x] != x:
                _parent[x] = _parent[_parent[x]]
                x = _parent[x]
            return x

        def union(a: int, b: int, _parent: list[int] = parent) -> None:
            ra, rb = find(a), find(b)
            if ra != rb:
                _parent[rb] = ra

        for i in range(n):
            for j in range(i + 1, n):
                if _jaccard(items[i][1], items[j][1]) >= threshold:
                    union(i, j)

        groups: dict[int, list[dict]] = {}
        for idx in range(n):
            groups.setdefault(find(idx), []).append(items[idx][0])

        for members in groups.values():
            rep = _representative(members)
            member_ids = sorted(int(m["id"]) for m in members)
            clusters.append(ProposalCluster(
                category=category,
                representative=rep,
                members=members,
                member_ids=member_ids,
            ))

    clusters.sort(
        key=lambda cl: (-cl.score, -cl.recurrence, cl.representative_id),
    )
    return clusters


# ─── DB loader ────────────────────────────────────────────────────────


def _load_open_proposals(competitor_id: str) -> list[dict]:
    """Load all status='open' proposals for one agent.

    House uses the (NULL OR ='house-claude') idiom because the live retro path
    historically wrote NULL; freestyle uses an exact competitor_id match. No
    30-day window here — backlog drain deliberately clears the WHOLE backlog,
    not just the recent slice the weekly review looks at.
    """
    if competitor_id == HOUSE_COMPETITOR_ID:
        where = f"status = 'open' AND {HOUSE_TRADE_FILTER}"
        args: tuple = ()
    else:
        where = "status = 'open' AND competitor_id = %s"
        args = (competitor_id,)
    sql = (
        "SELECT id, category, title, rationale, proposed_change, evidence, "
        "       confidence, created_ts, competitor_id "
        "FROM improvement_proposals "
        f"WHERE {where} "  # noqa: S608 — where is a hardcoded literal
        "ORDER BY created_ts DESC"
    )
    with conn() as c:
        rows = list(c.execute(sql, args))
    out: list[dict] = []
    for r in rows:
        out.append({
            "id": int(r["id"]),
            "category": r["category"],
            "title": r["title"],
            "rationale": r["rationale"],
            "proposed_change": r["proposed_change"],
            "evidence": r["evidence"],
            "confidence": int(r["confidence"]) if r["confidence"] is not None else None,
            "created_ts": r["created_ts"],
        })
    return out


def cluster_open_proposals(
    competitor_id: str,
    *,
    threshold: float = DEFAULT_SIMILARITY_THRESHOLD,
) -> list[ProposalCluster]:
    """Load + cluster + rank one agent's entire status='open' backlog.

    Thin DB-backed wrapper around the pure `cluster_proposals`. Returns clusters
    ranked by score = recurrence × representative.confidence, descending.
    """
    proposals = _load_open_proposals(competitor_id)
    return cluster_proposals(proposals, threshold=threshold)


def open_proposal_count(competitor_id: str) -> int:
    """How many status='open' proposals remain for this agent. Used by the
    drain loop to decide when the backlog is empty."""
    if competitor_id == HOUSE_COMPETITOR_ID:
        where = f"status = 'open' AND {HOUSE_TRADE_FILTER}"
        args: tuple = ()
    else:
        where = "status = 'open' AND competitor_id = %s"
        args = (competitor_id,)
    with conn() as c:
        row = c.execute(
            f"SELECT COUNT(*) AS n FROM improvement_proposals WHERE {where}",  # noqa: S608
            args,
        ).fetchone()
    return int(row["n"])


__all__ = [
    "ProposalCluster",
    "cluster_proposals",
    "cluster_open_proposals",
    "open_proposal_count",
    "DEFAULT_SIMILARITY_THRESHOLD",
]
