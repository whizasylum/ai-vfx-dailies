"""Fetch raw items from every configured source.

Every source type normalises down to the same Item shape, so everything
downstream is source-agnostic.
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from dataclasses import dataclass, asdict, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urljoin, urlparse, urlunparse, parse_qsl, urlencode

import feedparser
import httpx

log = logging.getLogger(__name__)

USER_AGENT = "ai-vfx-dailies/1.0 (+https://github.com/)"
TIMEOUT = httpx.Timeout(20.0, connect=10.0)

# Query params that are pure tracking noise; stripped so the same article from
# two feeds dedupes correctly.
TRACKING_PARAMS = re.compile(
    r"^(utm_|fbclid|gclid|mc_cid|mc_eid|ref|ref_src|source|igshid|_hsenc|_hsmi)",
    re.I,
)


@dataclass
class Item:
    title: str
    url: str
    source: str
    published: str            # ISO 8601 UTC
    summary: str = ""
    weight: float = 1.0
    channel: str = "r"
    extra: dict[str, Any] = field(default_factory=dict)
    score: float = 0.0

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------

def canonical_url(url: str) -> str:
    """Strip tracking params and trailing slashes so URLs dedupe reliably."""
    try:
        p = urlparse(url)
    except ValueError:
        return url
    kept = [(k, v) for k, v in parse_qsl(p.query) if not TRACKING_PARAMS.match(k)]
    path = p.path.rstrip("/") or "/"
    return urlunparse((p.scheme, p.netloc.lower(), path, "", urlencode(kept), ""))


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).replace(microsecond=0).isoformat()


def _parse_struct_time(st) -> datetime | None:
    if not st:
        return None
    try:
        return datetime(*st[:6], tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


def _clean(text: str, limit: int = 800) -> str:
    """Strip HTML tags and collapse whitespace."""
    text = re.sub(r"<[^>]+>", " ", text or "")
    text = re.sub(r"&[a-z]+;|&#\d+;", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:limit]


def _client() -> httpx.Client:
    return httpx.Client(
        timeout=TIMEOUT,
        follow_redirects=True,
        headers={"User-Agent": USER_AGENT},
    )


# --------------------------------------------------------------------------
# per-type collectors
# --------------------------------------------------------------------------

_IMG_TAG_RE = re.compile(r'<img[^>]+src=["\']([^"\']+)["\']', re.I)

# Some feeds put something in the thumbnail slot that is never a usable hero
# image for a story card. Hugging Face's papers feed is the big one: it hands
# back the *authors'* profile pictures.
_IMG_REJECT_RE = re.compile(
    r"cdn-avatars\.huggingface\.co|huggingface\.co/avatars/", re.I
)

# Feeds serve whatever derivative their CMS generates by default, which is
# often far smaller than what the same host will happily serve. Both of these
# were checked against the live hosts; if a rewritten URL ever 404s the page
# falls back to the original rather than showing a broken image.
_IMG_UPGRADES = (
    (re.compile(r"(i\d?\.ytimg\.com/vi/[\w-]+/)hqdefault\.jpg"), r"\1maxresdefault.jpg"),
    (
        re.compile(r"(awn\.com/sites/default/files/styles/)small_featured(/)"),
        r"\1large_featured\2",
    ),
)


def _clean_image(url: str | None) -> str | None:
    """Drop junk thumbnails, and upgrade known-small ones to a larger variant."""
    if not url or _IMG_REJECT_RE.search(url):
        return None
    for pattern, replacement in _IMG_UPGRADES:
        url = pattern.sub(replacement, url)
    return url


def _entry_image(e, raw_html: str = "") -> str | None:
    """Best-effort thumbnail for an RSS/Atom entry.

    Tries the Media RSS namespace first (what YouTube and most blogging
    platforms use), then an enclosure link, then falls back to the first
    <img> in whatever HTML the feed embedded. Feeds that offer nothing
    usable just get no image -- the page already handles that gracefully.
    """
    if thumbs := getattr(e, "media_thumbnail", None):
        if url := _clean_image(thumbs[0].get("url")):
            return url
    if media := getattr(e, "media_content", None):
        for m in media:
            if m.get("medium") == "image" or (m.get("type") or "").startswith("image/"):
                if url := _clean_image(m.get("url")):
                    return url
    for link in getattr(e, "links", None) or []:
        if link.get("rel") == "enclosure" and (link.get("type") or "").startswith("image/"):
            if url := _clean_image(link.get("href")):
                return url
    if m := _IMG_TAG_RE.search(raw_html):
        return _clean_image(m.group(1))
    return None


def from_rss(src: dict, cutoff: datetime) -> list[Item]:
    parsed = feedparser.parse(src["url"], agent=USER_AGENT)
    if parsed.bozo and not parsed.entries:
        raise RuntimeError(f"unparseable feed: {parsed.get('bozo_exception')}")

    items: list[Item] = []
    for e in parsed.entries:
        published = (
            _parse_struct_time(getattr(e, "published_parsed", None))
            or _parse_struct_time(getattr(e, "updated_parsed", None))
        )
        # No date is common on vendor feeds; treat as "now" so we don't lose
        # genuine releases, and let the seen-cache stop repeats.
        if published is None:
            published = datetime.now(timezone.utc)
        if published < cutoff:
            continue
        link = getattr(e, "link", None)
        if not link:
            continue
        raw_summary = getattr(e, "summary", "") or getattr(e, "description", "")
        extra = {}
        if image := _entry_image(e, raw_summary):
            extra["image"] = image
        items.append(
            Item(
                title=_clean(getattr(e, "title", ""), 300),
                url=canonical_url(link),
                source=src["name"],
                published=_iso(published),
                summary=_clean(raw_summary),
                weight=float(src.get("weight", 1.0)),
                channel=src.get("channel", "r"),
                extra=extra,
            )
        )
    return items


def from_html(src: dict, cutoff: datetime) -> list[Item]:
    """Scrape headline links off a vendor page that has no feed.

    Deliberately dumb: pull anchors matching a selector-ish substring, use the
    link text as the title. Vendors change markup constantly, so this is
    best-effort and `validate` will tell you when it stops returning anything.
    """
    from bs4 import BeautifulSoup

    with _client() as c:
        r = c.get(src["url"])
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "html.parser")

    selector = src.get("link_selector", "a")
    seen: set[str] = set()
    items: list[Item] = []
    now = datetime.now(timezone.utc)

    for a in soup.select(selector):
        href = a.get("href")
        text = _clean(a.get_text(" "), 300)
        if not href or len(text) < 15:
            continue
        url = canonical_url(urljoin(src["url"], href))
        if url in seen:
            continue
        seen.add(url)

        # A listing page's thumbnail usually isn't inside the headline <a>
        # itself, but a couple of ancestors up in the same card -- look
        # nearby rather than only inside the exact matched tag.
        extra = {"undated": True}
        img = a.find("img")
        if img is None and (card := a.find_parent(["article", "li", "div"])):
            img = card.find("img")
        if img is not None:
            img_src = img.get("src") or img.get("data-src")
            if img_src and (cleaned := _clean_image(urljoin(src["url"], img_src))):
                extra["image"] = cleaned

        items.append(
            Item(
                title=text,
                url=url,
                source=src["name"],
                published=_iso(now),
                weight=float(src.get("weight", 1.0)),
                channel=src.get("channel", "r"),
                extra=extra,
            )
        )
    return items[:25]


def from_github_releases(src: dict, cutoff: datetime) -> list[Item]:
    repo = src["repo"]
    headers = {"User-Agent": USER_AGENT, "Accept": "application/vnd.github+json"}
    if token := os.environ.get("GITHUB_TOKEN"):
        headers["Authorization"] = f"Bearer {token}"

    with _client() as c:
        r = c.get(
            f"https://api.github.com/repos/{repo}/releases",
            headers=headers,
            params={"per_page": 10},
        )
        r.raise_for_status()
        releases = r.json()

    items: list[Item] = []
    for rel in releases:
        if rel.get("draft"):
            continue
        published = datetime.fromisoformat(
            rel["published_at"].replace("Z", "+00:00")
        )
        if published < cutoff:
            continue
        name = rel.get("name") or rel.get("tag_name") or "release"
        items.append(
            Item(
                title=f"{repo.split('/')[-1]} {name}",
                url=canonical_url(rel["html_url"]),
                source=src["name"],
                published=_iso(published),
                summary=_clean(rel.get("body", ""), 1200),
                weight=float(src.get("weight", 1.0)),
                channel=src.get("channel", "r"),
                extra={"prerelease": rel.get("prerelease", False)},
            )
        )
    return items


def from_arxiv(src: dict, cutoff: datetime) -> list[Item]:
    with _client() as c:
        r = c.get(
            "http://export.arxiv.org/api/query",
            params={
                "search_query": src["query"],
                "sortBy": "submittedDate",
                "sortOrder": "descending",
                "max_results": 40,
            },
        )
        r.raise_for_status()
        parsed = feedparser.parse(r.text)

    items: list[Item] = []
    for e in parsed.entries:
        published = _parse_struct_time(getattr(e, "published_parsed", None))
        if published is None or published < cutoff:
            continue
        items.append(
            Item(
                title=_clean(e.title, 300),
                url=canonical_url(e.link),
                source=src["name"],
                published=_iso(published),
                summary=_clean(getattr(e, "summary", ""), 1200),
                weight=float(src.get("weight", 1.0)),
                channel=src.get("channel", "g"),
                extra={
                    "authors": [a.get("name") for a in getattr(e, "authors", [])][:4]
                },
            )
        )
    return items


def from_reddit(src: dict, cutoff: datetime) -> list[Item]:
    sub = src["subreddit"]
    min_score = int(src.get("min_score", 100))
    with _client() as c:
        r = c.get(
            f"https://www.reddit.com/r/{sub}/top.json",
            params={"t": "day", "limit": 40},
        )
        r.raise_for_status()
        payload = r.json()

    items: list[Item] = []
    for child in payload.get("data", {}).get("children", []):
        d = child.get("data", {})
        if d.get("score", 0) < min_score or d.get("stickied"):
            continue
        published = datetime.fromtimestamp(d.get("created_utc", 0), tz=timezone.utc)
        if published < cutoff:
            continue
        # Prefer the linked article over the reddit comment thread.
        url = d.get("url_overridden_by_dest") or f"https://reddit.com{d['permalink']}"
        extra = {
            "score": d.get("score"),
            "discussion": f"https://reddit.com{d['permalink']}",
        }
        # "self", "default", "nsfw", "spoiler" etc are placeholder values,
        # not URLs -- only trust it if it actually looks like an image link.
        thumb = d.get("thumbnail", "")
        if thumb.startswith("http") and (cleaned := _clean_image(thumb)):
            extra["image"] = cleaned
        items.append(
            Item(
                title=_clean(d.get("title", ""), 300),
                url=canonical_url(url),
                source=src["name"],
                published=_iso(published),
                summary=_clean(d.get("selftext", ""), 800),
                weight=float(src.get("weight", 1.0)),
                channel=src.get("channel", "r"),
                extra=extra,
            )
        )
    return items


def from_hn(src: dict, cutoff: datetime) -> list[Item]:
    min_points = int(src.get("min_points", 100))
    with _client() as c:
        r = c.get(
            "https://hn.algolia.com/api/v1/search_by_date",
            params={
                "query": src["query"],
                "tags": "story",
                "numericFilters": (
                    f"points>{min_points},created_at_i>{int(cutoff.timestamp())}"
                ),
                "hitsPerPage": 30,
            },
        )
        r.raise_for_status()
        payload = r.json()

    items: list[Item] = []
    for hit in payload.get("hits", []):
        url = hit.get("url") or f"https://news.ycombinator.com/item?id={hit['objectID']}"
        published = datetime.fromtimestamp(hit["created_at_i"], tz=timezone.utc)
        items.append(
            Item(
                title=_clean(hit.get("title", ""), 300),
                url=canonical_url(url),
                source=src["name"],
                published=_iso(published),
                weight=float(src.get("weight", 1.0)),
                channel=src.get("channel", "g"),
                extra={
                    "points": hit.get("points"),
                    "discussion": (
                        f"https://news.ycombinator.com/item?id={hit['objectID']}"
                    ),
                },
            )
        )
    return items


def _resolve_youtube_handle(handle: str, cache: dict) -> str | None:
    """Turn @handle into a channel_id, caching the result forever."""
    if handle in cache:
        return cache[handle]
    with _client() as c:
        r = c.get(f"https://www.youtube.com/{handle}")
        r.raise_for_status()
        m = re.search(r'"channelId":"(UC[\w-]{22})"', r.text)
        if not m:
            # YouTube's page JSON now carries the id under externalId instead.
            m = re.search(r'"externalId":"(UC[\w-]{22})"', r.text)
    if not m:
        return None
    cache[handle] = m.group(1)
    return cache[handle]


def from_youtube(src: dict, cutoff: datetime, cache: dict) -> list[Item]:
    channel_id = src.get("channel_id")
    if not channel_id:
        channel_id = _resolve_youtube_handle(src["handle"], cache)
    if not channel_id:
        raise RuntimeError(f"could not resolve handle {src.get('handle')}")

    feed_src = {
        **src,
        "url": (
            "https://www.youtube.com/feeds/videos.xml"
            f"?channel_id={channel_id}"
        ),
    }
    items = from_rss(feed_src, cutoff)
    for it in items:
        it.extra["video"] = True
    return items


COLLECTORS = {
    "rss": from_rss,
    "html": from_html,
    "github_releases": from_github_releases,
    "arxiv": from_arxiv,
    "reddit": from_reddit,
    "hn": from_hn,
}


# --------------------------------------------------------------------------
# entry point
# --------------------------------------------------------------------------

def collect_all(
    sources: list[dict],
    max_age_hours: int,
    state_dir: Path,
) -> tuple[list[Item], list[dict]]:
    """Returns (items, problems). Never raises on a single bad source."""
    cutoff = datetime.now(timezone.utc) - timedelta(hours=max_age_hours)

    yt_cache_path = state_dir / "youtube_channels.json"
    yt_cache = (
        json.loads(yt_cache_path.read_text()) if yt_cache_path.exists() else {}
    )

    items: list[Item] = []
    problems: list[dict] = []

    for src in sources:
        stype = src.get("type", "rss")
        name = src.get("name", src.get("url", "?"))
        try:
            if stype == "youtube":
                got = from_youtube(src, cutoff, yt_cache)
            else:
                collector = COLLECTORS.get(stype)
                if collector is None:
                    raise RuntimeError(f"unknown source type '{stype}'")
                got = collector(src, cutoff)
            log.info("%-32s %2d items", name, len(got))
            items.extend(got)
        except Exception as exc:  # noqa: BLE001 - one bad feed must not kill the run
            log.warning("%-32s FAILED: %s", name, exc)
            problems.append({"source": name, "type": stype, "error": str(exc)})
        time.sleep(0.4)  # be polite

    yt_cache_path.write_text(json.dumps(yt_cache, indent=2))
    return items, problems
