"""Render the digest to a web page and push it to Discord."""

from __future__ import annotations

import logging
import os
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import quote
from zoneinfo import ZoneInfo

import httpx
from jinja2 import Environment, FileSystemLoader, select_autoescape

from . import feedback

log = logging.getLogger(__name__)

CHANNELS = {
    "r": {"label": "Models & tools", "color": "#d9484a", "discord": 0xD9484A},
    "g": {"label": "Research", "color": "#5ba86b", "discord": 0x5BA86B},
    "b": {"label": "Industry", "color": "#4e7fd0", "discord": 0x4E7FD0},
    "a": {"label": "Tutorials", "color": "#cfcbc4", "discord": 0xCFCBC4},
}
ARCHIVE_KEEP = 30
DISCORD_API = "https://discord.com/api/v10"


def _env(root: Path) -> Environment:
    return Environment(
        loader=FileSystemLoader(root / "templates"),
        autoescape=select_autoescape(["html"]),
        trim_blocks=True,
        lstrip_blocks=True,
    )


def render_html(
    result: dict,
    config: dict,
    root: Path,
    candidate_count: int,
    problems: list[dict],
    sources_total: int,
    rating_stats: dict | None = None,
) -> Path:
    digest_cfg = config.get("digest", {})
    share_cfg = config.get("sharing", {})
    tz = ZoneInfo(digest_cfg.get("timezone", "UTC"))
    now = datetime.now(tz)
    rating_endpoint = share_cfg.get("rating_endpoint") or ""

    stories = []
    for s in result.get("stories", []):
        ch = s.get("channel", "r")
        if ch not in CHANNELS:
            ch = "r"
        sid = feedback.story_id(s)
        stories.append({
            **s,
            "channel": ch,
            "color": CHANNELS[ch]["color"],
            "sid": sid,
        })

    channels = [
        {
            "key": key,
            "label": meta["label"],
            "color": meta["color"],
            "count": sum(1 for s in stories if s["channel"] == key),
        }
        for key, meta in CHANNELS.items()
    ]

    docs = root / "docs"
    archive_dir = docs / "archive"
    archive_dir.mkdir(parents=True, exist_ok=True)

    existing = sorted(archive_dir.glob("*.html"), reverse=True)[: ARCHIVE_KEEP - 1]
    archive = [
        {
            "href": f"archive/{p.name}",
            "label": datetime.strptime(p.stem, "%Y-%m-%d").strftime("%b %-d"),
        }
        for p in existing[:14]
    ]

    html = _env(root).get_template("page.html").render(
        title=digest_cfg.get("title", "Dailies"),
        subtitle=digest_cfg.get("subtitle", ""),
        date_long=now.strftime("%A %-d %B %Y"),
        date_iso=now.strftime("%Y-%m-%d"),
        generated_at=now.strftime("%Y-%m-%d %H:%M %Z"),
        verdict=result.get("verdict", ""),
        stories=stories,
        story_count=len(stories),
        candidate_count=candidate_count,
        channels=channels,
        problems=problems,
        sources_ok=sources_total - len(problems),
        sources_total=sources_total,
        archive=archive,
        rating_stats=rating_stats or {},
        can_rate=bool(rating_endpoint),
        rating_endpoint=rating_endpoint,
        noindex=share_cfg.get("noindex", True),
        page_url=(share_cfg.get("page_url") or "").rstrip("/"),
    )

    index = docs / "index.html"
    index.write_text(html, encoding="utf-8")

    dated = archive_dir / f"{now.strftime('%Y-%m-%d')}.html"
    dated.write_text(html.replace('href="archive/', 'href="'), encoding="utf-8")

    for old in sorted(archive_dir.glob("*.html"), reverse=True)[ARCHIVE_KEEP:]:
        old.unlink()

    (docs / ".nojekyll").touch()
    robots = docs / "robots.txt"
    if share_cfg.get("noindex", True):
        robots.write_text("User-agent: *\nDisallow: /\n")
    elif robots.exists():
        robots.unlink()

    log.info("wrote %s and %s", index, dated)
    return index


def _truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[: limit - 1].rsplit(" ", 1)[0] + "\N{HORIZONTAL ELLIPSIS}"


def _embed(story: dict, page_url: str | None) -> dict:
    ch = CHANNELS.get(story.get("channel", "r"), CHANNELS["r"])
    links = " \N{MIDDLE DOT} ".join(
        f"[{src['name']}]({src['url']})" for src in story.get("sources", [])[:4]
    )
    description = _truncate(story.get("body", ""), 900)
    if story.get("why"):
        description += f"\n\n*{_truncate(story['why'], 200)}*"
    if links:
        description += f"\n\n{links}"

    embed = {
        "title": _truncate(story.get("headline", "Untitled"), 250),
        "description": description,
        "color": ch["discord"],
        "footer": {"text": f"{ch['label']} \N{MIDDLE DOT} react to rate"},
    }
    if page_url:
        embed["url"] = f"{page_url.rstrip('/')}/#{feedback.story_id(story)}"
    return embed


def _post_via_bot(payloads: list[dict], state_dir: Path, stories: list[dict]) -> bool:
    """One message per story, so a reaction attributes to exactly one story."""
    token = os.environ.get("DISCORD_BOT_TOKEN")
    channel_id = os.environ.get("DISCORD_CHANNEL_ID")
    if not token or not channel_id:
        return False

    headers = {"Authorization": f"Bot {token}", "User-Agent": "ai-vfx-dailies/1.0"}
    posted = 0
    with httpx.Client(timeout=25.0, headers=headers) as c:
        header, story_payloads = payloads[0], payloads[1:]
        r = c.post(f"{DISCORD_API}/channels/{channel_id}/messages", json=header)
        if r.status_code >= 400:
            log.error("discord header %s: %s", r.status_code, r.text[:300])
            return False
        time.sleep(0.4)

        for payload, story in zip(story_payloads, stories):
            r = c.post(f"{DISCORD_API}/channels/{channel_id}/messages", json=payload)
            if r.status_code >= 400:
                log.error("discord post %s: %s", r.status_code, r.text[:300])
                continue
            message_id = r.json()["id"]
            feedback.track_message(state_dir, message_id, story, feedback.story_id(story))
            # Seed both reactions so rating is one tap, not two.
            for emoji in (feedback.UP, feedback.DOWN):
                c.put(
                    f"{DISCORD_API}/channels/{channel_id}/messages/"
                    f"{message_id}/reactions/{quote(emoji)}/@me"
                )
                time.sleep(0.35)
            posted += 1
            time.sleep(0.4)

    log.info("posted %d stories via bot", posted)
    return True


def _post_via_webhook(payloads: list[dict]) -> bool:
    webhook = os.environ.get("DISCORD_WEBHOOK_URL")
    if not webhook:
        return False
    merged = {
        "content": payloads[0]["content"],
        "embeds": [p["embeds"][0] for p in payloads[1:]][:10],
        "allowed_mentions": payloads[0].get("allowed_mentions", {"parse": []}),
    }
    with httpx.Client(timeout=20.0) as c:
        r = c.post(webhook, json=merged)
        if r.status_code >= 400:
            log.error("discord %s: %s", r.status_code, r.text[:400])
            r.raise_for_status()
    log.info("posted %d embeds via webhook", len(merged["embeds"]))
    return True


def post_discord(
    result: dict,
    config: dict,
    state_dir: Path,
    page_url: str | None = None,
) -> None:
    digest_cfg = config.get("digest", {})
    delivery = config.get("delivery", {})
    share_cfg = config.get("sharing", {})
    min_rank = int(delivery.get("discord_min_rank", 3))
    tz = ZoneInfo(digest_cfg.get("timezone", "UTC"))
    now = datetime.now(tz)

    all_stories = result.get("stories", [])
    stories = all_stories[:min_rank]

    mention = str(share_cfg.get("discord_mention", "")).strip()
    header = f"**{digest_cfg.get('title', 'Dailies')}** \N{EM DASH} {now:%A %-d %B}"
    if mention:
        header = f"{mention} {header}"
    if verdict := result.get("verdict", ""):
        header += f"\n{verdict}"
    if not stories:
        header += "\nNothing cleared the filter today."

    remaining = len(all_stories) - len(stories)
    if page_url:
        header += (
            f"\n\n{remaining} more on the page: {page_url}"
            if remaining > 0
            else f"\n\n{page_url}"
        )

    payloads = [{
        "content": _truncate(header, 1900),
        "allowed_mentions": {"parse": ["roles"]} if mention else {"parse": []},
    }]
    payloads += [
        {"embeds": [_embed(s, page_url)], "allowed_mentions": {"parse": []}}
        for s in stories
    ]

    mode = delivery.get("discord_mode", "bot")
    if mode == "bot" and _post_via_bot(payloads, state_dir, stories):
        return
    if _post_via_webhook(payloads):
        if mode == "bot":
            log.warning(
                "fell back to webhook; reaction ratings need DISCORD_BOT_TOKEN "
                "and DISCORD_CHANNEL_ID"
            )
        return
    log.warning("no discord credentials configured, skipping")
