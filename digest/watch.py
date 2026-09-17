"""Watch YouTube videos with Gemini and decide whether the method is worth
stealing, even when the title gives no reason to think so.

The keyword/Claude pipeline only ever sees feed metadata -- title,
description, channel -- which misses videos where the creator wasn't making
anything VFX-related but the workflow underneath is (a game devlog that
happens to chain Astra 6 -> Blender -> Unity, say). This module hands the
actual video to a model -- frames and audio, via Gemini's native YouTube
ingestion, no download -- and asks it to judge the technique, not the title.

Pipeline: mechanical gate (free, deterministic) -> Gemini watch (one call per
surviving video) -> score on two axes -> publish into the digest or log the
rejection. Every video that clears the mechanical gate ends up in
state/watched.json either way, so nothing gets watched twice and every
rejection stays inspectable.
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

import httpx

from .collect import Item, TIMEOUT, USER_AGENT

log = logging.getLogger(__name__)

WATCHED_RETENTION_DAYS = 120

_VIDEO_ID_RE = re.compile(r"^[\w-]{11}$")
_API_KEY_RE = re.compile(r'"INNERTUBE_API_KEY":"([^"]+)"')

CATEGORY_CHANNEL = {
    "tool_release": "r",
    "research": "g",
    "industry": "b",
    "tutorial": "a",
    "workflow": "a",
}

PROMPT = """You are watching this video on behalf of a VFX technical artist working in
a USD-based pipeline (Houdini, Maya, Nuke, ZBrush). He does not care what the
creator was building -- game, ad, personal project, hobby film. He cares
about method.

Extract: asset creation workflows; how tools were chained together; where AI
generation was used versus hand-authored, and how the two were reconciled;
anything he could test this week.

If the video is a devlog, a product demo, or a tutorial for a different
discipline, still extract the method. Judge it on whether a working VFX TD
could act on it today, not on how exciting or well-produced it sounds.

Return ONLY this JSON object, no prose, no markdown fences:

{{
  "video_id": "{video_id}",
  "channel": "{channel}",
  "tools_and_platforms": [{{"name": "...", "used_for": "..."}}],
  "whats_new": "...",
  "whats_transferable": ["concrete technique, not 'it was interesting'"],
  "pipeline_notes": "where this would sit in a USD/Houdini workflow, or why it wouldn't",
  "visual_quality_notes": "...",
  "category": "tool_release | research | industry | tutorial | workflow",
  "topic_relevance": 0,
  "transferable_craft": 0,
  "reason_if_low": "...",
  "summary_bullets": ["...", "..."]
}}"""

# Same framing as summarize.py's CALIBRATION block, adapted for a per-video
# judgement rather than a clustering pass -- infer the pattern, don't
# keyword-match.
CALIBRATION = """

CALIBRATION FROM THE READER'S OWN RATINGS
Stories from this same digest he has rated after the fact, most recent first.
Use them to calibrate what "transferable" means to him, not as a checklist.

Marked useful:
{liked}

Marked not useful:
{cut}"""


# --------------------------------------------------------------------------
# state
# --------------------------------------------------------------------------

def _empty_state() -> dict:
    return {"videos": {}, "quota": {"date": "", "minutes": 0.0}}


def load_state(path: Path) -> dict:
    if not path.exists():
        return _empty_state()
    try:
        data = json.loads(path.read_text())
    except json.JSONDecodeError:
        log.warning("watched.json corrupt, starting fresh")
        return _empty_state()
    data.setdefault("videos", {})
    data.setdefault("quota", {"date": "", "minutes": 0.0})
    return data


def save_state(path: Path, state: dict) -> None:
    cutoff = datetime.now(timezone.utc) - timedelta(days=WATCHED_RETENTION_DAYS)
    kept = {
        vid: rec
        for vid, rec in state["videos"].items()
        if _safe_parse(rec.get("at")) is None or _safe_parse(rec.get("at")) >= cutoff
    }
    dropped = len(state["videos"]) - len(kept)
    path.write_text(json.dumps({**state, "videos": kept}, indent=1))
    log.info("watched cache: %d videos (pruned %d)", len(kept), dropped)


def _safe_parse(iso: str | None) -> datetime | None:
    if not iso:
        return None
    try:
        return datetime.fromisoformat(iso)
    except ValueError:
        return None


def _today_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _quota_used(state: dict) -> float:
    quota = state["quota"]
    if quota.get("date") != _today_utc():
        quota["date"] = _today_utc()
        quota["minutes"] = 0.0
    return quota["minutes"]


def _spend_quota(state: dict, minutes: float) -> None:
    state["quota"]["date"] = _today_utc()
    state["quota"]["minutes"] = state["quota"].get("minutes", 0.0) + minutes


# --------------------------------------------------------------------------
# mechanical gate
# --------------------------------------------------------------------------

def video_id_from_url(url: str) -> str | None:
    p = urlparse(url)
    if p.netloc.endswith("youtu.be"):
        vid = p.path.lstrip("/")
    elif vs := parse_qs(p.query).get("v"):
        vid = vs[0]
    else:
        # /shorts/<id>, /embed/<id>, /live/<id> carry the id as a path
        # segment rather than a query param.
        parts = [seg for seg in p.path.split("/") if seg]
        vid = parts[-1] if parts and parts[0] in ("shorts", "embed", "live") else None
    return vid if vid and _VIDEO_ID_RE.match(vid) else None


_INNERTUBE_URL = "https://www.youtube.com/youtubei/v1/player"
_INNERTUBE_CONTEXT = {"client": {"clientName": "WEB", "clientVersion": "2.20240101.00.00"}}


def _client() -> httpx.Client:
    return httpx.Client(
        timeout=TIMEOUT, follow_redirects=True, headers={"User-Agent": USER_AGENT}
    )


def probe_metadata(video_id: str) -> dict[str, Any] | None:
    """Duration and live/upcoming status, via YouTube's own internal player
    API -- the same endpoint the web player itself calls to hydrate.

    This repo originally scraped the watch page's embedded JSON directly
    (the same ytInitialPlayerResponse blob collect.py's channel-id resolver
    reads). That turned out to be unreliable specifically from GitHub
    Actions runners: a same-size, entirely normal-looking 200 response came
    back with videoDetails missing lengthSeconds, for a video that resolves
    fine from a residential connection -- evidently a different server-side
    rendering variant for that vantage point, not a hard block (the page's
    own INNERTUBE_API_KEY was still right there in it). This endpoint
    returns the full structured object directly regardless of that variant,
    at the cost of one extra request to pull the (public, unauthenticated,
    embedded-in-every-page) API key.

    playabilityStatus is deliberately not the signal used here: a bare WEB
    client context routinely comes back "UNPLAYABLE" for videos that stream
    fine in a real browser, because actual playback needs a signature/bot
    check this context doesn't satisfy. videoDetails itself is still
    populated correctly regardless -- it's genuinely absent only when the
    video really is gone, which is the check this function relies on.
    """
    try:
        with _client() as c:
            r = c.get(f"https://www.youtube.com/watch?v={video_id}")
            r.raise_for_status()
            m = _API_KEY_RE.search(r.text)
            if not m:
                log.warning("probe %s: no INNERTUBE_API_KEY on the watch page", video_id)
                return None

            r2 = c.post(
                _INNERTUBE_URL,
                params={"key": m.group(1)},
                json={"videoId": video_id, "context": _INNERTUBE_CONTEXT},
            )
            r2.raise_for_status()
            data = r2.json()
    except httpx.HTTPError as exc:
        log.warning("probe %s: %s", video_id, exc)
        return None

    video_details = data.get("videoDetails") or {}
    if not video_details:
        reason = data.get("playabilityStatus", {}).get("reason", "no videoDetails")
        log.info("probe %s: video unavailable (%s)", video_id, reason)
        return {"duration_s": None, "live": False, "upcoming": False}

    live = bool(video_details.get("isLive"))
    upcoming = bool(video_details.get("isUpcoming"))
    length = video_details.get("lengthSeconds")
    # A currently-live broadcast reports lengthSeconds "0" -- not a real
    # duration, so treat live/upcoming as duration-unknown rather than 0s.
    duration_s = int(length) if length and not (live or upcoming) else None

    if duration_s is None and not live and not upcoming:
        log.warning(
            "probe %s: videoDetails present but no usable lengthSeconds (%r)",
            video_id, length,
        )

    return {"duration_s": duration_s, "live": live, "upcoming": upcoming}


def _gate(
    meta: dict[str, Any] | None,
    cfg: dict,
    long_form_ok: bool,
) -> tuple[str, str]:
    """Returns (action, reason). action is one of:
    'go'      -- proceed to Gemini.
    'reject'  -- mechanically disqualified; log to watched.json, no API spend.
    'defer'   -- leave unmarked, try again next run.
    """
    if meta is None:
        return "defer", "metadata probe failed"
    if meta["upcoming"]:
        return "defer", "upcoming, not published yet"
    if meta["live"]:
        return "defer", "currently live"

    duration_s = meta["duration_s"]
    if duration_s is None:
        return "defer", "duration unknown"

    min_s = int(cfg.get("min_duration_seconds", 90))
    if duration_s < min_s:
        return "reject", f"under {min_s}s ({duration_s}s)"

    max_min = int(cfg.get("max_duration_minutes", 40))
    if duration_s > max_min * 60 and not long_form_ok:
        return "reject", f"over {max_min}min cap ({duration_s // 60}min)"

    return "go", ""


# --------------------------------------------------------------------------
# the Gemini call
# --------------------------------------------------------------------------

def _extract_json(text: str) -> dict:
    """Same defensive strategy as summarize._extract_json: strip fences,
    fall back to the outermost {...} if the model wrapped the answer in
    prose, tolerate stray control characters in string values."""
    text = text.strip()
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.M).strip()
    try:
        return json.loads(text, strict=False)
    except json.JSONDecodeError:
        start, end = text.find("{"), text.rfind("}")
        if start == -1 or end == -1:
            raise
        return json.loads(text[start : end + 1], strict=False)


def watch_video(
    video_url: str,
    video_id: str,
    channel: str,
    model_cfg: dict,
    duration_s: int | None,
    examples: dict[str, list[str]] | None = None,
) -> dict:
    """One Gemini call. Raises on transport/API failure -- the caller decides
    whether that means 'retry next run' (fresh video) or 'log and move on'."""
    from google import genai
    from google.genai import types

    prompt = PROMPT.format(video_id=video_id, channel=channel)
    liked = (examples or {}).get("liked", [])
    cut = (examples or {}).get("cut", [])
    if liked or cut:
        prompt += CALIBRATION.format(
            liked="\n".join(f"  + {h}" for h in liked) or "  (none yet)",
            cut="\n".join(f"  - {h}" for h in cut) or "  (none yet)",
        )

    client = genai.Client()  # reads GEMINI_API_KEY
    low_res_after = int(model_cfg.get("media_resolution_low_after_minutes", 20)) * 60
    video_part = types.Part(file_data=types.FileData(file_uri=video_url))
    if duration_s and duration_s > low_res_after:
        # Best-effort: cuts token cost on long videos. Verified against
        # google-genai's Part.media_resolution field as of this build; if a
        # future SDK renames or removes it, fall back to a full-resolution
        # call rather than failing the whole video over a cost optimisation.
        try:
            video_part = types.Part(
                file_data=types.FileData(file_uri=video_url),
                media_resolution=types.PartMediaResolution(
                    level=types.PartMediaResolutionLevel.MEDIA_RESOLUTION_LOW
                ),
            )
        except (AttributeError, TypeError, ValueError) as exc:
            log.debug("media_resolution not supported by this SDK version: %s", exc)

    resp = client.models.generate_content(
        model=model_cfg.get("model", "models/gemini-3-flash-preview"),
        contents=types.Content(parts=[video_part, types.Part(text=prompt)]),
    )
    if usage := getattr(resp, "usage_metadata", None):
        log.info(
            "gemini %s: %s in / %s out tokens",
            video_id, usage.prompt_token_count, usage.candidates_token_count,
        )
    text = getattr(resp, "text", None) or "".join(
        p.text for c in resp.candidates for p in c.content.parts if getattr(p, "text", None)
    )
    return _extract_json(text)


# --------------------------------------------------------------------------
# story shaping
# --------------------------------------------------------------------------

def build_story(result: dict, item: Item) -> dict:
    """Shape a Gemini verdict into the same story dict summarize.py produces,
    so render.py and the template need no separate code path -- just extra
    fields the template shows only when s.watched is set."""
    category = result.get("category", "workflow")
    channel = CATEGORY_CHANNEL.get(category, "a")
    return {
        "headline": item.title,
        "body": (result.get("whats_new") or "").strip(),
        "why": (result.get("pipeline_notes") or "").strip(),
        "channel": channel,
        "confidence": "high",  # Gemini watched the actual video, not a rumour.
        "sources": [
            {
                "name": item.source,
                "url": item.url,
                "discussion": None,
                "video": True,
            }
        ],
        "image": item.extra.get("image"),
        "watched": True,
        "tools_and_platforms": result.get("tools_and_platforms", []),
        "whats_transferable": result.get("whats_transferable", []),
        "topic_relevance": result.get("topic_relevance", 0),
        "transferable_craft": result.get("transferable_craft", 0),
    }


# --------------------------------------------------------------------------
# entry point
# --------------------------------------------------------------------------

def run(
    items: list[Item],
    config: dict,
    long_form_channels: set[str],
    state_dir: Path,
    examples: dict[str, list[str]] | None = None,
) -> tuple[list[dict], dict]:
    """Watch every eligible video item. Returns (stories, stats).

    Never raises -- a bad video, a quota exhaustion, or a missing API key all
    degrade to "watch nothing this run" rather than killing the digest.
    """
    cfg = config.get("video_watch", {})
    stats = {
        "candidates": len(items), "watched": 0, "published": 0,
        "rejected": 0, "deferred": 0, "errors": 0,
    }
    if not cfg.get("enabled", True):
        return [], stats
    if not os.environ.get("GEMINI_API_KEY"):
        log.info("GEMINI_API_KEY not set, skipping video watch")
        return [], stats

    state_path = state_dir / "watched.json"
    state = load_state(state_path)
    daily_quota = float(cfg.get("daily_quota_minutes", 480))
    threshold = float(cfg.get("publish_threshold", 6))

    stories: list[dict] = []
    dirty = False

    for item in items:
        video_id = video_id_from_url(item.url)
        if not video_id:
            log.warning("could not extract video id from %s", item.url)
            continue
        if video_id in state["videos"]:
            continue  # already watched or already rejected -- never retried

        meta = probe_metadata(video_id)
        action, reason = _gate(meta, cfg, item.source in long_form_channels)

        if action == "defer":
            stats["deferred"] += 1
            log.info("defer  %s: %s", video_id, reason)
            continue

        now = datetime.now(timezone.utc).isoformat()
        if action == "reject":
            state["videos"][video_id] = {
                "title": item.title, "channel": item.source, "url": item.url,
                "at": now, "duration_s": meta["duration_s"],
                "published": False, "reason_if_low": reason,
            }
            dirty = True
            stats["rejected"] += 1
            log.info("reject %s: %s", video_id, reason)
            continue

        # action == "go" -- spend quota and call Gemini.
        minutes = meta["duration_s"] / 60.0
        if _quota_used(state) + minutes > daily_quota:
            stats["deferred"] += 1
            log.warning(
                "daily Gemini video quota (%.0fmin) would be exceeded, "
                "deferring %s and the rest of this run's videos",
                daily_quota, video_id,
            )
            break

        try:
            verdict = watch_video(
                item.url, video_id, item.source,
                {**cfg, "model": cfg.get("model", "models/gemini-3-flash-preview")},
                meta["duration_s"], examples,
            )
        except Exception as exc:  # noqa: BLE001 - one bad video must not kill the run
            # Fresh videos in particular aren't retrievable yet -- leave
            # unmarked so the next run tries again rather than losing it.
            stats["errors"] += 1
            log.warning("gemini watch %s failed, will retry: %s", video_id, exc)
            continue

        _spend_quota(state, minutes)
        dirty = True
        stats["watched"] += 1

        topic = float(verdict.get("topic_relevance", 0) or 0)
        craft = float(verdict.get("transferable_craft", 0) or 0)
        published = topic >= threshold or craft >= threshold

        state["videos"][video_id] = {
            "title": item.title, "channel": item.source, "url": item.url,
            "at": now, "duration_s": meta["duration_s"],
            "topic_relevance": topic, "transferable_craft": craft,
            "category": verdict.get("category"),
            "published": published,
            "reason_if_low": verdict.get("reason_if_low", ""),
        }

        if published:
            stories.append(build_story(verdict, item))
            stats["published"] += 1
            log.info(
                "publish %s (%s): topic=%.0f craft=%.0f",
                video_id, item.title[:60], topic, craft,
            )
        else:
            log.info(
                "cut     %s (%s): topic=%.0f craft=%.0f -- %s",
                video_id, item.title[:60], topic, craft,
                verdict.get("reason_if_low", ""),
            )

        time.sleep(0.5)  # be polite to the API

    if dirty:
        save_state(state_path, state)
    return stories, stats
