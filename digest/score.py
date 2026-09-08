"""Dedupe, score, and cut the raw pile down to a shortlist worth paying for.

The point of this module is cost and noise control. Sixty sources produce
300-600 items a day; only ~60 ever reach the model.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timedelta, timezone
from difflib import SequenceMatcher
from pathlib import Path

from .collect import Item

log = logging.getLogger(__name__)

SEEN_RETENTION_DAYS = 60
TITLE_MATCH_THRESHOLD = 0.86

_WORD_RE = re.compile(r"[a-z0-9]+")


def _normalise_title(title: str) -> str:
    return " ".join(_WORD_RE.findall(title.lower()))


def load_seen(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text())
    except json.JSONDecodeError:
        log.warning("seen.json corrupt, starting fresh")
        return {}


def save_seen(path: Path, seen: dict[str, str]) -> None:
    cutoff = datetime.now(timezone.utc) - timedelta(days=SEEN_RETENTION_DAYS)
    pruned = {
        url: first_seen
        for url, first_seen in seen.items()
        if datetime.fromisoformat(first_seen) >= cutoff
    }
    path.write_text(json.dumps(pruned, indent=0, sort_keys=True))
    log.info("seen cache: %d urls (pruned %d)", len(pruned), len(seen) - len(pruned))


def drop_seen(items: list[Item], seen: dict[str, str]) -> list[Item]:
    return [it for it in items if it.url not in seen]


def dedupe(items: list[Item]) -> list[Item]:
    """Collapse the same story arriving from several sources.

    Exact URL match first, then fuzzy title match. The higher-weighted source
    wins and absorbs the others into extra['also_covered_by'].
    """
    def absorb(winner: Item, loser: Item) -> None:
        """Fold a duplicate into the winner without losing its detail.

        The highest-weighted source wins the headline, but it often has the
        thinnest blurb -- a wire-service stub beating a write-up that actually
        listed the licence and VRAM. Keep whichever summary is richer.
        """
        winner.extra.setdefault("also_covered_by", []).append(loser.source)
        if len(loser.summary) > len(winner.summary):
            winner.summary = loser.summary
        if not winner.extra.get("image") and loser.extra.get("image"):
            winner.extra["image"] = loser.extra["image"]

    by_url: dict[str, Item] = {}
    for it in sorted(items, key=lambda i: -i.weight):
        if it.url in by_url:
            absorb(by_url[it.url], it)
        else:
            by_url[it.url] = it

    kept: list[Item] = []
    norms: list[tuple[str, Item]] = []

    for it in sorted(by_url.values(), key=lambda i: -i.weight):
        norm = _normalise_title(it.title)
        if len(norm) < 12:
            kept.append(it)
            norms.append((norm, it))
            continue

        match = None
        for other_norm, other_item in norms:
            if SequenceMatcher(None, norm, other_norm).ratio() >= TITLE_MATCH_THRESHOLD:
                match = other_item
                break

        if match is not None:
            absorb(match, it)
            match.extra.setdefault("also_at", []).append(it.url)
        else:
            kept.append(it)
            norms.append((norm, it))

    log.info("dedupe: %d -> %d items", len(items), len(kept))
    return kept


def score(
    items: list[Item],
    keywords: dict,
    source_bias: dict[str, float] | None = None,
    term_bias: dict[str, float] | None = None,
) -> list[Item]:
    """Keyword relevance x source weight, with a mild recency bonus.

    source_bias and term_bias come from your ratings and are applied on top of
    the hand-set values in sources.yaml, never replacing them -- so a source you
    deliberately weighted up stays up even before any feedback exists.
    """
    source_bias = source_bias or {}
    term_bias = term_bias or {}
    bands = [
        (keywords.get("strong", []), 3.0),
        (keywords.get("medium", []), 1.5),
        (keywords.get("weak", []), 0.5),
        (keywords.get("negative", []), -2.0),
    ]
    now = datetime.now(timezone.utc)

    for it in items:
        haystack = f"{it.title} {it.summary}".lower()
        raw = 0.0
        hits: list[str] = []
        for terms, points in bands:
            for term in terms:
                if term.lower() in haystack:
                    raw += points
                    if points > 0:
                        hits.append(term)

        # Title hits count double -- headlines are where the real signal is.
        title_lower = it.title.lower()
        raw += sum(
            points
            for terms, points in bands[:3]
            for term in terms
            if term.lower() in title_lower
        )

        # Terms learned from your ratings.
        learned: list[str] = []
        for term, adjustment in term_bias.items():
            if term in haystack:
                raw += adjustment
                learned.append(term)

        age_hours = (now - datetime.fromisoformat(it.published)).total_seconds() / 3600
        recency = max(0.0, 1.0 - age_hours / 48.0)

        bias = source_bias.get(it.source, 1.0)
        it.score = round(raw * it.weight * bias + recency * 2.0, 3)
        it.extra["keyword_hits"] = sorted(set(hits))[:8]
        if bias != 1.0:
            it.extra["source_bias"] = bias
        if learned:
            it.extra["learned_hits"] = sorted(learned)[:6]

    return sorted(items, key=lambda i: -i.score)


def shortlist(items: list[Item], limit: int) -> list[Item]:
    """Take the top N, but guarantee every channel gets some representation
    so a busy tool-release day doesn't bury all the research."""
    picked: list[Item] = []
    picked_urls: set[str] = set()

    per_channel_floor = max(2, limit // 8)
    for channel in ("r", "g", "b", "a"):
        in_channel = [i for i in items if i.channel == channel][:per_channel_floor]
        for it in in_channel:
            if it.url not in picked_urls and it.score > 0:
                picked.append(it)
                picked_urls.add(it.url)

    for it in items:
        if len(picked) >= limit:
            break
        if it.url not in picked_urls and it.score > 0:
            picked.append(it)
            picked_urls.add(it.url)

    picked.sort(key=lambda i: -i.score)
    log.info("shortlist: %d items -> %d candidates", len(items), len(picked))
    return picked
