# Dailies

A daily digest of AI news relevant to VFX and image/video generation. Scrapes
~44 sources, dedupes and scores them, has Claude cluster and write up what
survives, then publishes a web page and posts the top stories to Discord.

Designed to be read in under ten minutes and then closed.

## How it works

```
sources.yaml ──▶ collect ──▶ dedupe ──▶ keyword score ──▶ shortlist (60)
                                                              │
                                                              ▼
                    docs/index.html  ◀── render ◀── Claude (cluster + write)
                    Discord webhook                            │
                                                        state/seen.json
```

The keyword pass exists to control cost. Sixty sources produce several hundred
items a day; only the top 60 ever reach the API, which is roughly 25-30k input
tokens per run. At current Sonnet pricing that's a few cents a day.

## Setup

**1. Create the repo**

Push this directory to a new GitHub repo. Then in Settings → Pages, set the
source to **GitHub Actions**.

**2. Add secrets**

Settings → Secrets and variables → Actions:

| Secret | Where to get it |
| --- | --- |
| `ANTHROPIC_API_KEY` | console.anthropic.com → API keys |
| `DISCORD_BOT_TOKEN` | discord.com/developers → New Application → Bot → Reset Token |
| `DISCORD_CHANNEL_ID` | Discord → enable Developer Mode → right-click channel → Copy Channel ID |
| `DISCORD_WEBHOOK_URL` | Optional fallback. Channel settings → Integrations → Webhooks |

`GITHUB_TOKEN` is provided automatically.

The bot needs **Send Messages**, **Add Reactions**, **Read Message History**, and
**Embed Links** in that channel. Invite it via the OAuth2 URL generator with the
`bot` scope. If you skip the bot and use only a webhook, everything still works
except reaction ratings — the page thumbs remain.

**3. Validate the sources before you trust anything**

I wrote the source list from research, but feed URLs rot and I had no network
access to test them when building this. Run this first:

```bash
pip install -r requirements.txt
python -m digest.cli validate
```

It reports each source as healthy, silent, or failed. Expect a handful of
failures on the first run — vendor blogs move their feeds constantly. Fix or
delete those lines in `sources.yaml`. The `html` type sources (Runway, Luma,
Lightricks) are the most fragile because they're scraping markup that changes.

**4. Dry run**

```bash
export ANTHROPIC_API_KEY=...
python -m digest.cli run --dry
```

Prints the scored shortlist without spending anything. This is the fastest way
to tune `config.yaml` — if the top of the list is junk, adjust weights and
keywords and run it again.

**5. Go live**

Actions → Daily digest → Run workflow. After it succeeds the page is at
`https://<you>.github.io/<repo>/`.

## Rating and how it learns

Every story can be rated two ways:

- **Discord** — tap the thumb. Both reactions are pre-seeded so it's one tap.
  The bot reads them back on the next run.
- **The page** — tap Useful / Not useful directly. A Cloudflare Worker
  (`cloudflare/rating-worker.js`) files the GitHub issue on your behalf so
  reading isn't interrupted by a GitHub page — set `sharing.rating_endpoint`
  in `config.yaml` to its URL to turn this on. The workflow reads the issue,
  applies it, and closes it, same as before.

Ratings feed three things, weakest to strongest:

| | What it does | Ratings needed |
| --- | --- | --- |
| Source weights | Multiplier per source, 0.4× to 1.8×, on top of `sources.yaml` | ~50 |
| Learned terms | Keywords discovered from what you liked, fed into the prefilter | ~40 |
| Prompt calibration | Your recent rated headlines shown to Claude as worked examples | ~10 |

The third does most of the work. It shows the model what you actually valued
rather than what you said you wanted, and it overrides the `profile` text where
the two disagree.

Everything is smoothed and bounded, so a bad week can't bury a good source and
one stray tap can't swing the digest. Nothing steers anything until you have at
least six ratings.

See what it has learned:

```bash
python -m digest.cli feedback
```

That prints current source multipliers, learned terms, and the exact examples
being fed to the model. If the digest starts drifting somewhere odd, this is
where you look first. Ratings older than 180 days age out, so the filter tracks
your interests as they move rather than being anchored to last year.

## Sharing

**Discord is your real access control.** Channel permissions decide who sees the
digest, invites are revocable, and it's per-person. Set `discord_mention` in
`config.yaml` to a role ID (`"<@&123...>"`) to ping a group. Note that anyone
who can react in the channel can also rate, so their taps train your filter —
usually fine for a small trusted group, worth knowing about.

**The web page cannot be access-controlled, and I'd rather say so than fake it.**
GitHub Pages serves publicly on Free and Pro accounts; private Pages needs
Enterprise. Anything I could add in the page — a passphrase prompt, obfuscation —
is client-side and trivially bypassed, so it would be security theatre.

What's actually in place:

- `noindex: true` writes a robots meta tag and a `robots.txt`, so the page stays
  out of search results. The link only spreads to people you send it to.
- Every story has a stable anchor and a **Copy link** button, so you can send one
  colleague one story rather than the whole page.
- Rating no longer requires a GitHub account or repo access -- the Worker files
  the issue itself, so anyone who can load the page (or the Worker URL
  directly) can submit a rating. That trade was made deliberately for a
  one-tap experience; the existing smoothing (`PRIOR_STRENGTH`,
  `min_ratings_to_calibrate`) is what keeps a handful of stray or bad-faith
  taps from swinging the filter. Same trust model as the Discord reactions
  above, just without even needing channel membership.

If you genuinely need the page restricted, the two real options are Cloudflare
Access in front of a custom domain (free tier, proper auth, needs a domain), or
keeping the repo private and treating Discord as the only delivery. Given the
content is curated public links and short summaries, I'd just make it public and
control distribution through the Discord invite.

## Tuning

`config.yaml` is the file you'll actually edit. Two levers:

- **`profile`** — plain-language editorial direction handed to Claude. This
  decides what leads and what gets cut. Rewrite it like you're briefing a
  researcher. It matters more than anything else in this repo.
- **`keywords`** — the cheap prefilter that decides what Claude even sees. If
  something good never makes the digest, check `--dry` output first: it may be
  getting cut here, before the model ever sees it.

Source `weight` values in `sources.yaml` are a third lever. Drop a source's
weight when it keeps surfacing noise; you rarely need to delete it outright.

## Reading the page

Stories are ranked, not chronological. The bar at the top is the shape of the
day — mostly red means a tool-release day, mostly blue means industry news.

| | |
| --- | --- |
| **R** | Models & tools |
| **G** | Research & techniques |
| **B** | Industry, business, legal |
| **A** | Tutorials & workflow |

Anything marked as a single unverified source is flagged on the page. The model
is instructed to say when a claim is vendor-supplied rather than tested.

## Schedule

Cron runs at 18:00 UTC, which is 06:00 in Wellington during NZST and 07:00
during NZDT. GitHub cron doesn't follow DST, so it shifts by an hour twice a
year. Edit the cron line in `.github/workflows/digest.yml` if that's annoying.

Source validation runs automatically every Monday and shows up as a failed job
in the Actions tab if feeds have died. That's deliberate — feeds fail silently
otherwise and you won't notice the digest quietly getting thinner.

## Known limits

- **No X/Twitter.** There's no usable free API and the scraping workarounds
  break weekly. A lot of genuine first-word-of-a-release happens there. The
  honest workaround is a Discord or RSS bridge you maintain yourself.
- **`html` sources are brittle** by nature. Treat them as best-effort.
- **The model can be wrong.** It's summarising feed blurbs, not reading the
  articles. The confidence flag helps, but the digest is a triage layer that
  points you at things, not a substitute for reading the source.
- **Ratings are unattributed.** A thumb from a colleague in Discord counts the
  same as yours. Keep the channel small or accept the blend.
- **Undated feeds** get stamped with the fetch time, so a vendor feed that
  republishes old posts can resurface them once. `seen.json` stops repeats
  after the first occurrence.
