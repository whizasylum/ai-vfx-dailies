"""Cluster the shortlist into stories and write the digest.

One API call per run. The model is doing editorial work here -- grouping the
six articles about one model launch, deciding what leads, and cutting the
filler -- not just paraphrasing.
"""

from __future__ import annotations

import json
import logging
import os
import re
from typing import Any

import anthropic

from .collect import Item

log = logging.getLogger(__name__)

SYSTEM = """You are the editor of a daily industry digest for one specific reader.

Your job is triage, not coverage. The reader has ten minutes. A short digest of
things that matter beats a complete one. If the day is quiet, say so and return
few stories -- padding is a failure, not a safe default.

READER PROFILE
{profile}

TASK
You receive candidate items scraped from feeds. You must:

1. CLUSTER. Group items covering the same underlying event into one story.
   Six articles about one model release is one story with six sources.
2. CUT. Drop anything that fails the reader profile, is pure marketing, or is
   a rehash of something with no new information. Cutting most of the input is
   normal and correct.
3. RANK. Order by how much this should change what the reader does this week.
   A plugin that lands in Houdini outranks a better benchmark score.
4. WRITE. For each story:
   - headline: plain, specific, factual. Say what happened and what it is.
     No hype verbs, no "game-changing", no colons-then-reveal.
   - body: 50-90 words. Lead with the concrete fact. Include the details this
     reader needs to decide whether to look further: licence, VRAM, price,
     availability, whether it's open weights, what it integrates with. If a
     claim is vendor-supplied and unverified, say so plainly.
   - why: one sentence, max 20 words, on the practical consequence for a
     working VFX pipeline. Be concrete. If there isn't one, the story should
     probably have been cut.
   - channel: exactly one of "r" (models & tools), "g" (research &
     techniques), "b" (industry, business, legal), "a" (tutorials & workflow).
   - confidence: "high" if from a primary source or vendor announcement,
     "medium" if trade press, "low" if a single community post or rumour.

STYLE
Write plainly, as a knowledgeable colleague would. Short declarative sentences.
No em-dash asides, no "not just X but Y" constructions, no rhetorical questions.
Never invent facts, versions, prices, or capabilities not present in the input.
If the input is too thin to write a body confidently, cut the story.

OUTPUT
Return ONLY a JSON object, no prose, no markdown fences:

{{
  "verdict": "one sentence characterising the day overall",
  "stories": [
    {{
      "headline": "...",
      "body": "...",
      "why": "...",
      "channel": "r",
      "confidence": "high",
      "source_ids": [3, 11, 14]
    }}
  ]
}}

source_ids are the integer ids of the input items that make up the story, most
authoritative first. Return at most {max_stories} stories."""

CALIBRATION = """

CALIBRATION FROM THE READER'S OWN RATINGS
These are real stories this reader marked after reading them. They override the
profile above where the two disagree -- the profile is what he said he wants,
this is what he actually valued.

Stories he marked useful:
{liked}

Stories he marked not useful:
{cut}

Infer the pattern rather than matching keywords. If the "not useful" list is
full of a certain shape of story, cut that shape today even when it scores well."""


def _build_payload(items: list[Item]) -> str:
    rows = []
    for idx, it in enumerate(items):
        row: dict[str, Any] = {
            "id": idx,
            "title": it.title,
            "source": it.source,
            "url": it.url,
            "published": it.published,
            "channel_hint": it.channel,
        }
        if it.summary:
            row["summary"] = it.summary[:700]
        if also := it.extra.get("also_covered_by"):
            row["also_covered_by"] = sorted(set(also))[:6]
        for key in ("score", "points", "video", "prerelease"):
            if key in it.extra:
                row[key] = it.extra[key]
        rows.append(row)
    return json.dumps(rows, ensure_ascii=False, indent=1)


def _extract_json(text: str) -> dict:
    text = text.strip()
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.M).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        # Model occasionally wraps in prose; grab the outermost object.
        start, end = text.find("{"), text.rfind("}")
        if start == -1 or end == -1:
            raise
        return json.loads(text[start : end + 1])


def summarize(
    items: list[Item],
    config: dict,
    examples: dict[str, list[str]] | None = None,
) -> dict:
    if not items:
        return {"verdict": "No new items cleared the filter today.", "stories": []}

    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    model_cfg = config.get("model", {})
    digest_cfg = config.get("digest", {})

    system = SYSTEM.format(
        profile=config.get("profile", "").strip(),
        max_stories=digest_cfg.get("max_stories", 10),
    )

    # Fewer than a handful of ratings is noise, not signal -- don't steer on it.
    examples = examples or {}
    liked, cut = examples.get("liked", []), examples.get("cut", [])
    if len(liked) + len(cut) >= 6:
        system += CALIBRATION.format(
            liked="\n".join(f"  + {h}" for h in liked) or "  (none yet)",
            cut="\n".join(f"  - {h}" for h in cut) or "  (none yet)",
        )
        log.info("calibrating on %d liked / %d cut examples", len(liked), len(cut))

    resp = client.messages.create(
        model=model_cfg.get("name", "claude-sonnet-5"),
        max_tokens=model_cfg.get("max_tokens", 8000),
        system=system,
        messages=[
            {
                "role": "user",
                "content": (
                    f"Today's candidates ({len(items)} items):\n\n"
                    f"{_build_payload(items)}"
                ),
            },
            # Prefill the assistant turn so the model continues straight into
            # JSON with no room for a prose preamble -- cheaper and far more
            # reliable than hoping "no prose" in the system prompt holds, and
            # it stops a long analytical response from burning the token
            # budget on reasoning-in-text before ever reaching the JSON.
            {"role": "assistant", "content": "{"},
        ],
    )

    usage = resp.usage
    log.info(
        "model: %d in / %d out tokens, stop_reason=%s, blocks=%s",
        usage.input_tokens, usage.output_tokens, resp.stop_reason,
        [b.type for b in resp.content],
    )

    text = "{" + "".join(b.text for b in resp.content if b.type == "text")
    try:
        result = _extract_json(text)
    except json.JSONDecodeError as exc:
        raise RuntimeError(
            f"model did not return parseable JSON "
            f"(stop_reason={resp.stop_reason}, output_tokens={usage.output_tokens}, "
            f"text_len={len(text)}). First 500 chars: {text[:500]!r}"
        ) from exc

    # Re-attach real source records; never trust the model to echo URLs back.
    for story in result.get("stories", []):
        sources = []
        for sid in story.get("source_ids", []):
            if isinstance(sid, int) and 0 <= sid < len(items):
                it = items[sid]
                sources.append(
                    {
                        "name": it.source,
                        "url": it.url,
                        "discussion": it.extra.get("discussion"),
                        "video": it.extra.get("video", False),
                    }
                )
        story["sources"] = sources
        story.pop("source_ids", None)

    result["stories"] = [s for s in result.get("stories", []) if s.get("sources")]
    result["usage"] = {
        "input_tokens": usage.input_tokens,
        "output_tokens": usage.output_tokens,
    }
    return result
