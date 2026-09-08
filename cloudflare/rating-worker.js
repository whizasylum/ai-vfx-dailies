/**
 * Cloudflare Worker: records a digest rating as a GitHub issue, so the page
 * can send a single tap instead of routing through GitHub's own "new issue"
 * UI. This is the only place a GitHub credential lives in the rating flow --
 * the page itself carries no token, same principle the original GitHub-issue
 * design was built on, just moved server-side instead of relying on the
 * visitor's own GitHub session.
 *
 * The issue title format ("rating +1 <sid>" / "rating -1 <sid>") must match
 * digest/feedback.py's _ISSUE_TITLE regex exactly -- that's what the next
 * digest run scans for and ingests as a rating, then closes.
 *
 * Deploy: Cloudflare dashboard -> Workers & Pages -> Create -> Create Worker
 * -> paste this file's contents -> Deploy. Then Settings -> Variables ->
 * add an encrypted secret named GITHUB_TOKEN (a fine-grained PAT scoped to
 * Issues: read and write on just this one repo -- nothing broader).
 */

const REPO = "whizasylum/ai-vfx-dailies";
const ALLOWED_ORIGIN = "https://whizasylum.github.io";
const SID_RE = /^[0-9a-f]{10}$/;

function withCors(resp) {
  resp.headers.set("Access-Control-Allow-Origin", ALLOWED_ORIGIN);
  resp.headers.set("Vary", "Origin");
  return resp;
}

export default {
  async fetch(request, env) {
    if (request.method === "OPTIONS") {
      return withCors(
        new Response(null, {
          headers: {
            "Access-Control-Allow-Methods": "POST, OPTIONS",
            "Access-Control-Allow-Headers": "Content-Type",
          },
        })
      );
    }

    if (request.method !== "POST") {
      return withCors(new Response("method not allowed", { status: 405 }));
    }

    let body;
    try {
      body = await request.json();
    } catch {
      return withCors(new Response("bad request", { status: 400 }));
    }

    const sid = body && body.sid;
    const vote = body && body.vote;
    if (!SID_RE.test(sid || "") || (vote !== "+1" && vote !== "-1")) {
      return withCors(new Response("bad request", { status: 400 }));
    }

    const gh = await fetch(`https://api.github.com/repos/${REPO}/issues`, {
      method: "POST",
      headers: {
        Authorization: `Bearer ${env.GITHUB_TOKEN}`,
        Accept: "application/vnd.github+json",
        "User-Agent": "ai-vfx-dailies-rating-worker",
        "Content-Type": "application/json",
      },
      body: JSON.stringify({
        title: `rating ${vote} ${sid}`,
        body: "Submitted from the digest page via the rating worker. Closes automatically on the next run.",
      }),
    });

    if (!gh.ok) {
      const text = await gh.text();
      return withCors(
        new Response(`upstream error: ${text.slice(0, 200)}`, { status: 502 })
      );
    }

    return withCors(
      new Response(JSON.stringify({ ok: true }), {
        headers: { "Content-Type": "application/json" },
      })
    );
  },
};
