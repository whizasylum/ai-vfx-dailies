"""Rebuild a bounded, chronological timeline for every retained edition."""

import re
from datetime import datetime
from pathlib import Path

NAV = re.compile(r'<nav class="(?:timeline|edition-nav)"[^>]*>[\s\S]*?</nav>')


def markup(day: str, days: list[str], in_archive: bool) -> str:
    pos = days.index(day)
    prefix = "" if in_archive else "archive/"

    def control(index, label, direction):
        text = "← Older" if direction == "prev" else "Newer →"
        if not 0 <= index < len(days):
            return f'<span class="day-step {direction}" aria-disabled="true">{text}</span>'
        return (f'<a class="day-step {direction}" href="{prefix}{days[index]}.html" '
                f'aria-label="{label} edition" rel="{direction}">{text}</a>')

    pills = []
    for date in days:
        dt = datetime.fromisoformat(date)
        label = f"{dt:%b} {dt.day}"
        if date == day:
            pills.append(f'<span class="current" aria-current="page"><time datetime="{date}">{label}</time></span>')
        else:
            pills.append(f'<a href="{prefix}{date}.html"><time datetime="{date}">{label}</time></a>')
    latest = (f'<a class="day-latest" href="{prefix}{days[-1]}.html">Latest ↗</a>'
              if day != days[-1] else '<span class="day-latest at-latest">Latest edition</span>')
    return (f'<nav class="edition-nav" aria-label="Daily editions" data-edition="{day}">'
            + control(pos - 1, "Older", "prev")
            + '<div class="day-track" tabindex="0" aria-label="Dates, oldest to newest">'
            + "".join(pills) + '</div>' + control(pos + 1, "Newer", "next") + latest + '</nav>')


def patch(html: str, nav: str, css: str, js: str) -> str:
    if NAV.search(html):
        html = NAV.sub(lambda _: nav, html, count=1)
    else:
        html = html.replace('</header>', '</header>\n' + nav, 1)
    # Retire the old listeners before installing one shared implementation.
    html = re.sub(r"  document.querySelectorAll\('\.copy'\)\.forEach[\s\S]*?(?=  var RATING_ENDPOINT)", "", html)
    html = re.sub(r"  /\* daynav:[\s\S]*?(?=</script>)", "", html)
    html = re.sub(r'<style id="edition-navigation">[\s\S]*?</style>\s*', '', html)
    html = re.sub(r'<script id="edition-navigation">[\s\S]*?</script>\s*', '', html)
    html = html.replace('</head>', f'<style id="edition-navigation">{css}</style>\n</head>')
    return html.replace('</body>', f'<script id="edition-navigation">{js}</script>\n</body>')


def refresh(root: Path) -> int:
    docs = root / 'docs'
    paths = sorted((docs / 'archive').glob('????-??-??.html'))
    days = [p.stem for p in paths]
    if not days:
        return 0
    css = (root / 'templates' / 'navigation.css').read_text(encoding='utf-8')
    js = (root / 'templates' / 'navigation.js').read_text(encoding='utf-8')
    count = 0
    for path in paths + [docs / 'index.html']:
        if not path.exists():
            continue
        html = path.read_text(encoding='utf-8')
        # index.html always represents the newest retained edition.
        day = path.stem if path.name != 'index.html' else days[-1]
        updated = patch(html, markup(day, days, path.parent.name == 'archive'), css, js)
        if html != updated:
            path.write_text(updated, encoding='utf-8')
            count += 1
    return count
