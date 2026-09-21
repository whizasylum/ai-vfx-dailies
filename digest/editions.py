"""Keep complete daily editions so retries can add stories without erasing them."""

import json
import re
from pathlib import Path

from bs4 import BeautifulSoup

from .feedback import story_id


def read_html(path: Path) -> dict:
    """Recover older editions before JSON snapshots existed."""
    soup = BeautifulSoup(path.read_text(encoding="utf-8"), "html.parser")
    stories = []
    for card in soup.select("article.story"):
        def text(selector):
            node = card.select_one(selector)
            return node.get_text(" ", strip=True) if node else ""

        sources = []
        for link in card.select(".sources a"):
            if "talk" in link.get("class", []):
                if sources:
                    sources[-1]["discussion"] = link["href"]
                continue
            sources.append({"name": link.get_text(strip=True), "url": link["href"],
                            "video": "video" in link.get("class", [])})
        img = card.select_one(".thumb img:not(.thumbnail-art)")
        stories.append({
            "headline": text(".headline"), "body": text(".body-text"),
            "why": text(".why"), "channel": text(".tag b, .rail b").lower(),
            "confidence": "low" if card.select_one(".low-confidence") else "medium",
            "sources": sources, "image": img.get("src", "").removeprefix("../") if img else None,
            "image_kind": img.get("data-kind", "source") if img else None,
            "watched": bool(card.select_one(".watched-badge")),
            "tools_and_platforms": [{"name": n.get_text(strip=True),
                                      "used_for": n.get("title", "")}
                                     for n in card.select(".tools li")],
            "whats_transferable": [n.get_text(" ", strip=True)
                                    for n in card.select(".transferable li")],
        })
    verdict = soup.select_one(".verdict")
    stamp = soup.select_one('.stamp')
    count = re.search(r'from (\d+) items', stamp.get_text() if stamp else '')
    return {"verdict": verdict.get_text(" ", strip=True) if verdict else "", "stories": stories,
            "candidate_count": int(count.group(1)) if count else len(stories)}


def merge(root: Path, day: str, result: dict) -> dict:
    path = root / "state" / "editions" / f"{day}.json"
    archive = root / "docs" / "archive" / f"{day}.html"
    previous = (json.loads(path.read_text(encoding="utf-8")) if path.exists()
                else read_html(archive) if archive.exists() else {})
    stories = {story_id(s): s for s in previous.get("stories", [])}
    stories.update({story_id(s): s for s in result.get("stories", [])})
    combined = {**result, "stories": list(stories.values()),
                "candidate_count": previous.get("candidate_count", 0) + result.get("candidate_count", 0)}
    if previous.get("stories"):
        # A new summariser verdict only describes this run's additions.
        combined["verdict"] = (previous.get("verdict", "") if not result.get("stories")
                               else f"{len(stories)} stories collected across today's updates.")
    elif combined["stories"] and not result.get("editorial_stories", True):
        combined["verdict"] = f"{len(stories)} video workflows worth a closer look."
    save(root, day, combined)
    return combined


def save(root: Path, day: str, edition: dict) -> None:
    path = root / "state" / "editions" / f"{day}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(edition, indent=2, ensure_ascii=False), encoding="utf-8")
    temporary.replace(path)
