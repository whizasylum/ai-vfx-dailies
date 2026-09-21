"""Command line entry point.

    python -m digest.cli run          full pipeline, writes page + posts Discord
    python -m digest.cli run --dry    collect and score only, no API spend
    python -m digest.cli validate     check every source responds
    python -m digest.cli watch --url URL   test the Gemini video watcher on
                                            one or more videos, no state writes
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import yaml

from . import collect, feedback, render, score, summarize, watch, editions, navigation, thumbnails

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
    if fb_cfg.get("enabled", True) and not args.no_feedback and not args.dry:
        n = feedback.ingest_discord(state_dir, ratings)
        n += feedback.ingest_github_issues(state_dir, ratings)
        if n:
            feedback.save_ratings(state_dir, ratings)
    if not args.dry:
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

    # Videos take a separate path entirely: Gemini judges each one on its own
    # merits and either publishes or logs it to state/watched.json, rather
    # than competing for the keyword-scored shortlist. Pulled out here so
    # the two pipelines never see each other's items.
    video_items = [it for it in items if it.extra.get("video")]
    items = [it for it in items if not it.extra.get("video")]

    raw_count = len(items) + len(video_items)
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
        print(
            f"\n{len(candidates)} candidates, {len(video_items)} videos pending "
            f"watch, no API call made.",
            file=sys.stderr,
        )
        return 0

    examples = None
    if stats["total"] >= fb_cfg.get("min_ratings_to_calibrate", 6):
        examples = feedback.calibration_examples(
            ratings, fb_cfg.get("examples_per_side", 8)
        )
    result = summarize.summarize(candidates, config, examples)
    print(f"model returned {len(result.get('stories', []))} stories", file=sys.stderr)

    long_form_channels = {
        s["name"] for s in sources
        if s.get("type") == "youtube" and s.get("long_form_ok")
    }
    video_stories, video_stats = watch.run(
        video_items, config, long_form_channels, state_dir, examples
    )
    print(
        f"videos: {video_stats['watched']} watched, "
        f"{video_stats['published']} published, "
        f"{video_stats['rejected']} rejected, "
        f"{video_stats['deferred']} deferred, "
        f"{video_stats['errors']} errors",
        file=sys.stderr,
    )
    result["candidate_count"] = len(candidates) + video_stats["watched"]
    result["editorial_stories"] = bool(result.get("stories"))
    result["stories"] = result.get("stories", []) + video_stories
    day = datetime.now(ZoneInfo(digest_cfg.get("timezone", "UTC"))).date().isoformat()
    result = editions.merge(ROOT, day, result)
    thumbnails.prepare(result.get("stories", []), config, ROOT, day)
    editions.save(ROOT, day, result)

    # Record what each story was, so a rating from the page can be attributed
    # back to its sources and terms later.
    feedback.index_stories(state_dir, result.get("stories", []))

    page_url = args.page_url or config.get("sharing", {}).get("page_url") or None

    if config.get("delivery", {}).get("html", True):
        render.render_html(
            result, config, ROOT, result["candidate_count"], problems, len(sources), stats
        )

    if config.get("delivery", {}).get("discord", False) and not args.no_discord:
        render.post_discord(result, config, state_dir, page_url)

    watch.mark_delivered(state_dir, video_stories)

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
                "raw": raw_count,
                "candidates": len(candidates),
                "stories": len(result.get("stories", [])),
                "usage": result.get("usage", {}),
                "video_watch": video_stats,
                "problems": problems,
            },
            indent=2,
        )
    )
    return 0


def cmd_watch(args: argparse.Namespace) -> int:
    """Try the watcher on specific videos without touching any state.

    For testing the Gemini call and scoring on real videos -- e.g. one that
    the keyword pipeline would never surface -- before trusting it to run
    unattended. Never writes to state/watched.json and never joins a digest.
    """
    config, _ = _load()
    cfg = config.get("video_watch", {})
    if not os.environ.get("GEMINI_API_KEY"):
        print("GEMINI_API_KEY is not set.", file=sys.stderr)
        return 1

    failed = False
    for url in args.url:
        video_id = watch.video_id_from_url(url)
        if not video_id:
            print(f"\n{url}\n  could not extract a video id, skipping")
            failed = True
            continue

        meta = watch.probe_metadata(video_id)
        print(f"\n{url}  ({video_id})")
        print(f"  metadata: {meta}")

        if meta is None:
            print("  probe failed, skipping the Gemini call")
            failed = True
            continue
        action, reason = watch._gate(meta, cfg, long_form_ok=args.long_form)
        print(f"  mechanical gate: {action}" + (f" -- {reason}" if reason else ""))
        if action != "go" and not args.force:
            print("  (pass --force to call Gemini anyway)")
            failed = True
            continue

        url = f"https://www.youtube.com/watch?v={video_id}"
        try:
            verdict = watch.watch_video(
                url, video_id, args.channel, cfg, meta.get("duration_s"),
            )
            verdict = watch.validate_verdict(verdict, float(cfg.get("publish_threshold", 6)))
        except Exception as exc:  # noqa: BLE001
            print(f"  gemini call failed: {exc}")
            failed = True
            continue

        print(json.dumps(verdict, indent=2))
        threshold = float(cfg.get("publish_threshold", 6))
        topic = float(verdict.get("topic_relevance", 0) or 0)
        craft = float(verdict.get("transferable_craft", 0) or 0)
        published = topic >= threshold or craft >= threshold
        print(f"  => {'PUBLISH' if published else 'cut'} "
              f"(topic={topic:.0f} craft={craft:.0f}, threshold={threshold:.0f})")

    return 1 if failed else 0


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

    wt = sub.add_parser(
        "watch", help="test the Gemini video watcher on specific URLs"
    )
    wt.add_argument("--url", action="append", required=True,
                     help="a YouTube video URL; repeat for more than one")
    wt.add_argument("--channel", default="test",
                     help="channel name to show the model (cosmetic only)")
    wt.add_argument("--long-form", action="store_true",
                     help="treat the video as exempt from the duration cap")
    wt.add_argument("--force", action="store_true",
                     help="call Gemini even if the mechanical gate would skip it")
    wt.set_defaults(func=cmd_watch)

    fb = sub.add_parser("feedback", help="show what your ratings have learned")
    fb.add_argument("--offline", action="store_true",
                    help="show stored ratings without fetching new ones")
    fb.set_defaults(func=cmd_feedback)

    nav = sub.add_parser("refresh-navigation", help="repair all page navigation without model calls")
    nav.set_defaults(func=lambda args: (navigation.refresh(ROOT), 0)[1])
    thumbs = sub.add_parser("refresh-thumbnails", help="refresh offline thumbnail fallbacks without API calls")
    thumbs.set_defaults(func=lambda args: (thumbnails.refresh(ROOT), 0)[1])
    ack = sub.add_parser("ack-feedback", help="close rating issues after state has been committed")
    ack.set_defaults(func=lambda args: feedback.acknowledge_github_issues(ROOT / "state"))

    args = parser.parse_args()
    _setup_logging(args.verbose)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
