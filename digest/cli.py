"""Command line entry point.

    python -m digest.cli run          full pipeline, writes page + posts Discord
    python -m digest.cli run --dry    collect and score only, no API spend
    python -m digest.cli validate     check every source responds
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import yaml

from . import collect, feedback, render, score, summarize

ROOT = Path(__file__).resolve().parent.parent


def _setup_logging(verbose: bool) -> None:
    # Scraped titles carry arbitrary Unicode (arrows, curly quotes, emoji from
    # icon fonts). Windows consoles default stdout/stderr to the system
    # codepage, which chokes on those -- force UTF-8 so `run --dry` doesn't
    # crash on whatever a source happened to publish today.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(levelname)-7s %(message)s",
        stream=sys.stderr,
    )


def _load() -> tuple[dict, list[dict]]:
    config = yaml.safe_load((ROOT / "config.yaml").read_text())
    sources = yaml.safe_load((ROOT / "sources.yaml").read_text())
    return config, sources


def cmd_validate(args: argparse.Namespace) -> int:
    config, sources = _load()
    state_dir = ROOT / "state"
    state_dir.mkdir(exist_ok=True)

    # Wide window so a slow-publishing source still shows as alive.
    items, problems = collect.collect_all(sources, 24 * 14, state_dir)

    by_source: dict[str, int] = {}
    for it in items:
        by_source[it.source] = by_source.get(it.source, 0) + 1

    failed = {p["source"] for p in problems}
    silent, ok = [], []
    for src in sources:
        name = src["name"]
        if name in failed:
            continue
        (ok if by_source.get(name) else silent).append(name)

    print(f"\n  {len(ok)} healthy, {len(silent)} silent, {len(problems)} failed\n")
    if silent:
        print("  SILENT (no items in 14 days — may be fine, may be dead):")
        for name in silent:
            print(f"    {name}")
        print()
    if problems:
        print("  FAILED:")
        for p in problems:
            print(f"    {p['source']}: {p['error'][:110]}")
        print()
    return 1 if problems else 0


def cmd_run(args: argparse.Namespace) -> int:
    config, sources = _load()
    digest_cfg = config.get("digest", {})
    state_dir = ROOT / "state"
    state_dir.mkdir(exist_ok=True)
    seen_path = state_dir / "seen.json"

    # Pull in yesterday's ratings before scoring, so they apply today.
    fb_cfg = config.get("feedback", {})
    ratings = feedback.load_ratings(state_dir)
    if fb_cfg.get("enabled", True) and not args.no_feedback:
        n = feedback.ingest_discord(state_dir, ratings)
        n += feedback.ingest_github_issues(state_dir, ratings)
        if n:
            feedback.save_ratings(state_dir, ratings)
    stats = feedback.summary(ratings)
    print(
        f"ratings: {stats['total']} ({stats['up']}+ / {stats['down']}-)",
        file=sys.stderr,
    )

    items, problems = collect.collect_all(
        sources, digest_cfg.get("max_age_hours", 36), state_dir
    )
    print(f"collected {len(items)} raw items", file=sys.stderr)

    seen = score.load_seen(seen_path)
    items = score.drop_seen(items, seen)
    items = score.dedupe(items)
    items = score.score(
        items,
        config.get("keywords", {}),
        source_bias=feedback.source_bias(ratings),
        term_bias=feedback.term_bias(ratings),
    )
    candidates = score.shortlist(items, digest_cfg.get("max_candidates", 60))

    if args.dry:
        for it in candidates[:40]:
            hits = ",".join(it.extra.get("keyword_hits", [])[:4])
            print(f"{it.score:7.2f}  [{it.channel}] {it.source[:22]:22}  "
                  f"{it.title[:70]}  ({hits})")
        print(f"\n{len(candidates)} candidates, no API call made.", file=sys.stderr)
        return 0

    examples = None
    if stats["total"] >= fb_cfg.get("min_ratings_to_calibrate", 6):
        examples = feedback.calibration_examples(
            ratings, fb_cfg.get("examples_per_side", 8)
        )
    result = summarize.summarize(candidates, config, examples)
    print(f"model returned {len(result.get('stories', []))} stories", file=sys.stderr)

    # Record what each story was, so a rating from the page can be attributed
    # back to its sources and terms later.
    feedback.index_stories(state_dir, result.get("stories", []))

    page_url = args.page_url or config.get("sharing", {}).get("page_url") or None

    if config.get("delivery", {}).get("html", True):
        render.render_html(
            result, config, ROOT, len(candidates), problems, len(sources), stats
        )

    if config.get("delivery", {}).get("discord", False) and not args.no_discord:
        render.post_discord(result, config, state_dir, page_url)

    # Only mark items seen after a successful run, so a crash doesn't silently
    # swallow a day of news.
    now = datetime.now(timezone.utc).isoformat()
    for it in candidates:
        seen.setdefault(it.url, now)
    for it in items:
        seen.setdefault(it.url, now)
    score.save_seen(seen_path, seen)

    (state_dir / "last_run.json").write_text(
        json.dumps(
            {
                "at": now,
                "ratings": stats,
                "raw": len(items),
                "candidates": len(candidates),
                "stories": len(result.get("stories", [])),
                "usage": result.get("usage", {}),
                "problems": problems,
            },
            indent=2,
        )
    )
    return 0


def cmd_feedback(args: argparse.Namespace) -> int:
    config, _ = _load()
    state_dir = ROOT / "state"
    state_dir.mkdir(exist_ok=True)
    ratings = feedback.load_ratings(state_dir)

    if not args.offline:
        n = feedback.ingest_discord(state_dir, ratings)
        n += feedback.ingest_github_issues(state_dir, ratings)
        if n:
            feedback.save_ratings(state_dir, ratings)

    stats = feedback.summary(ratings)
    print(f"\n  {stats['total']} ratings  "
          f"({stats['up']} useful / {stats['down']} not useful)\n")

    bias = feedback.source_bias(ratings)
    if bias:
        print("  SOURCE WEIGHTS (1.0 = neutral)")
        for name, val in sorted(bias.items(), key=lambda kv: -kv[1]):
            bar = "+" if val > 1 else "-"
            print(f"    {val:5.2f} {bar}  {name}")
        print()

    terms = feedback.term_bias(ratings)
    if terms:
        print("  TERMS LEARNED")
        for term, val in sorted(terms.items(), key=lambda kv: -kv[1])[:24]:
            print(f"    {val:+5.2f}  {term}")
        print()

    ex = feedback.calibration_examples(ratings, 5)
    min_needed = config.get("feedback", {}).get("min_ratings_to_calibrate", 6)
    if stats["total"] < min_needed:
        print(f"  Need {min_needed - stats['total']} more ratings before these "
              f"steer the summariser.\n")
    else:
        print("  CALIBRATING THE SUMMARISER ON")
        for h in ex["liked"]:
            print(f"    +  {h[:68]}")
        for h in ex["cut"]:
            print(f"    -  {h[:68]}")
        print()
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(prog="digest")
    parser.add_argument("-v", "--verbose", action="store_true")
    sub = parser.add_subparsers(dest="cmd", required=True)

    run = sub.add_parser("run", help="build and publish today's digest")
    run.add_argument("--dry", action="store_true",
                     help="score and print candidates, skip the API call")
    run.add_argument("--no-discord", action="store_true")
    run.add_argument("--no-feedback", action="store_true",
                     help="skip rating ingest (useful when testing locally)")
    run.add_argument("--page-url", default=None,
                     help="public URL of the page, linked from Discord")
    run.set_defaults(func=cmd_run)

    val = sub.add_parser("validate", help="check every source still responds")
    val.set_defaults(func=cmd_validate)

    fb = sub.add_parser("feedback", help="show what your ratings have learned")
    fb.add_argument("--offline", action="store_true",
                    help="show stored ratings without fetching new ones")
    fb.set_defaults(func=cmd_feedback)

    args = parser.parse_args()
    _setup_logging(args.verbose)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
