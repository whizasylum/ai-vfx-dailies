"""Collect your ratings and turn them into filtering pressure.

Two capture paths, because the friction has to be near zero or you stop doing it:

  Discord  -- tap the thumb on the embed. Reactions are pre-seeded so it's one
              tap, and the bot reads them back on the next run.
  Web page -- the thumbs open a prefilled GitHub issue. Uses your existing
              GitHub session, so there is no token in the page.

Three places the ratings then land, weakest to strongest:

  1. source_bias    -- a multiplier per source. Needs ~50 ratings to mean much.
  2. term_bias      -- keywords discovered from what you liked. Needs volume too.
  3. examples       -- recent rated headlines injected into the summariser prompt
                       as worked examples. Starts working at ~10 ratings and does
                       most of the real work.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import httpx

log = logging.getLogger(__name__)

UP, DOWN = "\N{THUMBS UP SIGN}", "\N{THUMBS DOWN SIGN}"
DISCORD_API = "https://discord.com/api/v10"

# Smoothing so three bad days don't bury a source that is usually good.
PRIOR_STRENGTH = 4.0
MIN_TERM_OBSERVATIONS = 6
RATING_RETENTION_DAYS = 180
MESSAGE_TRACK_DAYS = 21

_STOPWORDS = {
    "the", "a", "an", "and", "or", "for", "with", "from", "to", "of", "in",
    "on", "at", "by", "is", "are", "new", "now", "its", "it", "this", "that",
    "how", "what", "why", "you", "your", "can", "will", "has", "have", "was",
    "more", "into", "out", "up", "as", "be", "but", "not", "all", "just",
    # Generic news verbs and nouns. Without these the term learner happily
    # concludes you love the word "released", which appears in every headline
    # and therefore predicts nothing.
    "release", "released", "releases", "launch", "launches", "launched",
    "announce", "announced", "announces", "add", "adds", "added", "ship",
    "ships", "shipped", "bring", "brings", "get", "gets", "make", "makes",
    "update", "updates", "updated", "version", "available", "introduce",
    "introduces", "unveil", "unveils", "reveal", "reveals", "arrive",
    "arrives", "model", "models", "tool", "tools", "first", "best",
    "top", "use", "using", "used", "way", "ways", "here", "look", "looks",
}
_WORD = re.compile(r"[a-z][a-z0-9.+-]{2,}")


def story_id(story: dict) -> str:
    """Stable id derived from the primary source URL."""
    sources = story.get("sources") or []
    seed = sources[0]["url"] if sources else story.get("headline", "")
    return hashlib.sha1(seed.encode()).hexdigest()[:10]


def terms_of(story: dict) -> list[str]:
    text = f"{story.get('headline','')} {story.get('why','')}".lower()
    return sorted({w for w in _WORD.findall(text) if w not in _STOPWORDS})[:14]


# --------------------------------------------------------------------------
# state
# --------------------------------------------------------------------------

def _load(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text())
    except json.JSONDecodeError:
        log.warning("%s corrupt, resetting", path.name)
        return default


def load_ratings(state_dir: Path) -> list[dict]:
    return _load(state_dir / "feedback.json", {"ratings": []})["ratings"]


def save_ratings(state_dir: Path, ratings: list[dict]) -> None:
    cutoff = datetime.now(timezone.utc) - timedelta(days=RATING_RETENTION_DAYS)
    kept = [
        r for r in ratings
        if datetime.fromisoformat(r["at"]) >= cutoff
    ]
    (state_dir / "feedback.json").write_text(
        json.dumps({"ratings": kept}, indent=1)
    )
    log.info("ratings: %d stored", len(kept))


# --------------------------------------------------------------------------
# capture: discord reactions
# --------------------------------------------------------------------------

def _discord_headers() -> dict[str, str] | None:
    token = os.environ.get("DISCORD_BOT_TOKEN")
    if not token:
        return None
    return {
        "Authorization": f"Bot {token}",
        "User-Agent": "ai-vfx-dailies/1.0",
    }


def ingest_discord(state_dir: Path, ratings: list[dict]) -> int:
    """Read reactions off messages we posted, convert to ratings."""
    headers = _discord_headers()
    channel_id = os.environ.get("DISCORD_CHANNEL_ID")
    if not headers or not channel_id:
        log.info("discord bot not configured, skipping reaction ingest")
        return 0

    msg_path = state_dir / "discord_messages.json"
    tracked: dict[str, dict] = _load(msg_path, {})
    cutoff = datetime.now(timezone.utc) - timedelta(days=MESSAGE_TRACK_DAYS)
    already = {(r["story_id"], r["via"]) for r in ratings}

    added = 0
    still_tracked: dict[str, dict] = {}

    with httpx.Client(timeout=20.0, headers=headers) as c:
        for message_id, meta in tracked.items():
            if datetime.fromisoformat(meta["at"]) < cutoff:
                continue
            still_tracked[message_id] = meta

            if (meta["story_id"], "discord") in already:
                continue
            try:
                r = c.get(f"{DISCORD_API}/channels/{channel_id}/messages/{message_id}")
                if r.status_code == 404:
                    continue
                r.raise_for_status()
                reactions = r.json().get("reactions", [])
            except Exception as exc:  # noqa: BLE001
                log.warning("discord read %s: %s", message_id, exc)
                continue

            counts = {
                x["emoji"]["name"]: x["count"] for x in reactions
            }
            # We seed one of each, so anything above 1 is a human.
            up = max(0, counts.get(UP, 0) - 1)
            down = max(0, counts.get(DOWN, 0) - 1)
            if up == down:
                continue  # no vote, or a tie between colleagues

            ratings.append({
                "story_id": meta["story_id"],
                "at": datetime.now(timezone.utc).isoformat(),
                "vote": 1 if up > down else -1,
                "headline": meta["headline"],
                "sources": meta["sources"],
                "channel": meta["channel"],
                "terms": meta["terms"],
                "via": "discord",
            })
            added += 1
            time.sleep(0.3)  # discord rate limits are tight

    msg_path.write_text(json.dumps(still_tracked, indent=1))
    if added:
        log.info("ingested %d ratings from discord", added)
    return added


def track_message(
    state_dir: Path, message_id: str, story: dict, sid: str
) -> None:
    path = state_dir / "discord_messages.json"
    tracked = _load(path, {})
    tracked[str(message_id)] = {
        "story_id": sid,
        "headline": story.get("headline", ""),
        "sources": [s["name"] for s in story.get("sources", [])],
        "channel": story.get("channel", "r"),
        "terms": terms_of(story),
        "at": datetime.now(timezone.utc).isoformat(),
    }
    path.write_text(json.dumps(tracked, indent=1))


# --------------------------------------------------------------------------
# capture: github issues from the web page
# --------------------------------------------------------------------------

_ISSUE_TITLE = re.compile(r"^rating\s+([+-]1)\s+([0-9a-f]{10})\s*$", re.I)

# A manual "I found this, the filter should have caught it" signal. Open an
# issue titled e.g. "suggest: https://youtube.com/watch?v=... camera control
# rig breakdown" -- treated as a liked example with the same weight as a real
# thumbs-up, so it feeds calibration_examples() and term_bias() exactly like
# any other rating. No new subsystem, just a second title pattern on the same
# issue scan.
_SUGGEST_TITLE = re.compile(r"^suggest:?\s+(.+)$", re.I)
_URL_RE = re.compile(r"https?://\S+")


def ingest_github_issues(state_dir: Path, ratings: list[dict]) -> int:
    token = os.environ.get("GITHUB_TOKEN")
    repo = os.environ.get("GITHUB_REPOSITORY")
    if not token or not repo:
        log.info("github issue ingest not configured, skipping")
        return 0

    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "User-Agent": "ai-vfx-dailies/1.0",
    }
    index = {
        m["story_id"]: m
        for m in _load(state_dir / "discord_messages.json", {}).values()
    }
    # The page also writes a story index so ratings work without Discord.
    index.update(_load(state_dir / "story_index.json", {}))

    added = 0
    with httpx.Client(timeout=20.0, headers=headers) as c:
        # Deliberately not filtering by label. A `labels=` parameter in a
        # new-issue URL is silently dropped if that label doesn't exist yet,
        # which would break ratings on day one with no visible error. The title
        # pattern is strict enough to identify these on its own.
        r = c.get(
            f"https://api.github.com/repos/{repo}/issues",
            params={"state": "open", "per_page": 100},
        )
        r.raise_for_status()
        for issue in r.json():
            if "pull_request" in issue:
                continue
            title = issue.get("title", "").strip()

            if m := _ISSUE_TITLE.match(title):
                vote, sid = (1 if m.group(1) == "+1" else -1), m.group(2)
                meta = index.get(sid, {})
                ratings.append({
                    "story_id": sid,
                    "at": datetime.now(timezone.utc).isoformat(),
                    "vote": vote,
                    "headline": meta.get("headline", title),
                    "sources": meta.get("sources", []),
                    "channel": meta.get("channel", "r"),
                    "terms": meta.get("terms", []),
                    "via": "page",
                })
                added += 1
            elif m := _SUGGEST_TITLE.match(title):
                text = m.group(1).strip()
                # Strip any URL before pulling terms, so a raw link doesn't
                # salt term_bias with junk tokens like "https" or "watch".
                url = _URL_RE.search(text)
                description = (
                    (text[: url.start()] + text[url.end() :]).strip()
                    if url else text
                ).strip(" -:")
                sid = hashlib.sha1(text.encode()).hexdigest()[:10]
                ratings.append({
                    "story_id": sid,
                    "at": datetime.now(timezone.utc).isoformat(),
                    "vote": 1,
                    "headline": text,
                    "sources": [],
                    "channel": "r",
                    "terms": terms_of({"headline": description or text}),
                    "via": "suggestion",
                })
                added += 1
            else:
                continue

            c.patch(
                f"https://api.github.com/repos/{repo}/issues/{issue['number']}",
                json={"state": "closed", "state_reason": "completed"},
            )

    if added:
        log.info("ingested %d ratings from the page", added)
    return added


def index_stories(state_dir: Path, stories: list[dict]) -> None:
    """Record what each story_id was, so page ratings can be attributed."""
    path = state_dir / "story_index.json"
    index = _load(path, {})
    cutoff = datetime.now(timezone.utc) - timedelta(days=MESSAGE_TRACK_DAYS)
    index = {
        k: v for k, v in index.items()
        if datetime.fromisoformat(v["at"]) >= cutoff
    }
    for story in stories:
        index[story_id(story)] = {
            "headline": story.get("headline", ""),
            "sources": [s["name"] for s in story.get("sources", [])],
            "channel": story.get("channel", "r"),
            "terms": terms_of(story),
            "at": datetime.now(timezone.utc).isoformat(),
        }
    path.write_text(json.dumps(index, indent=1))


# --------------------------------------------------------------------------
# derive: what the ratings actually change
# --------------------------------------------------------------------------

def _smoothed(up: int, down: int) -> float:
    """Beta-smoothed hit rate, neutral at 0.5."""
    return (up + PRIOR_STRENGTH * 0.5) / (up + down + PRIOR_STRENGTH)


def source_bias(ratings: list[dict]) -> dict[str, float]:
    """Per-source multiplier in [0.4, 1.8]. Neutral is 1.0."""
    tally: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    for r in ratings:
        for name in r.get("sources", []):
            tally[name][0 if r["vote"] > 0 else 1] += 1

    bias = {}
    for name, (up, down) in tally.items():
        if up + down < 3:
            continue
        bias[name] = round(min(1.8, max(0.4, 2.0 * _smoothed(up, down))), 3)
    return bias


def term_bias(ratings: list[dict]) -> dict[str, float]:
    """Keyword score adjustments discovered from your ratings."""
    tally: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    for r in ratings:
        for term in r.get("terms", []):
            tally[term][0 if r["vote"] > 0 else 1] += 1

    bias = {}
    for term, (up, down) in tally.items():
        if up + down < MIN_TERM_OBSERVATIONS:
            continue
        # Map hit rate onto roughly -2.0 .. +2.0 points of keyword score.
        adjustment = round((_smoothed(up, down) - 0.5) * 4.0, 2)
        if abs(adjustment) >= 0.4:
            bias[term] = adjustment
    return bias


def calibration_examples(ratings: list[dict], n: int = 8) -> dict[str, list[str]]:
    """Recent rated headlines, newest first, for the summariser prompt."""
    ordered = sorted(ratings, key=lambda r: r["at"], reverse=True)
    liked, cut = [], []
    for r in ordered:
        headline = (r.get("headline") or "").strip()
        if not headline:
            continue
        bucket = liked if r["vote"] > 0 else cut
        if len(bucket) < n and headline not in bucket:
            bucket.append(headline)
        if len(liked) >= n and len(cut) >= n:
            break
    return {"liked": liked, "cut": cut}


def summary(ratings: list[dict]) -> dict:
    up = sum(1 for r in ratings if r["vote"] > 0)
    return {
        "total": len(ratings),
        "up": up,
        "down": len(ratings) - up,
        "sources_adjusted": len(source_bias(ratings)),
        "terms_learned": len(term_bias(ratings)),
    }
