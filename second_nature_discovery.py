#!/usr/bin/env python3
"""
second_nature_discovery.py

Second Nature (secondnature.ai) doesn't publish public API docs, so before
we build the real "email the Lane 2 manager a progress recap" automation,
this script answers three questions:

  1. Does their API even live at one of the guessable hosts?
  2. Which auth scheme does SECOND_NATURE_API_KEY expect (Bearer, an
     X-Api-Key header, HTTP Basic like Close uses, etc.)?
  3. Do any endpoints return something that looks like module/session
     completion or progress data -- vs. a flat 404 or dead host?

This is a ONE-OFF DIAGNOSTIC, not a real integration. Everything in
CANDIDATE_HOSTS / CANDIDATE_PATHS below is a guess. If nothing here comes
back alive, the answer isn't "guess harder" -- it's to ask Second Nature
support for the real base URL and auth format. Treat a fully-dead report
as a confirmed result, not a failure of this script.

Run via the paired GitHub Actions workflow (workflow_dispatch only --
deliberately not scheduled, this doesn't need to run more than a
handful of times). Results land in second_nature_discovery_report.md
(uploaded as a build artifact) and in the job summary.
"""

import os
import sys
import time
from datetime import datetime, timezone

import requests

API_KEY = os.environ.get("SECOND_NATURE_API_KEY")
if not API_KEY:
    print("ERROR: SECOND_NATURE_API_KEY is not set.", flush=True)
    sys.exit(1)

TIMEOUT = 10                # seconds per request
SLEEP_BETWEEN_CALLS = 0.75  # we're guessing at someone else's API -- be polite
MAX_TOTAL_REQUESTS = 120    # hard safety cap so a bug here can't hammer their servers
SNIPPET_LEN = 300           # truncate response bodies -- we don't want real
                             # trainee data sitting in a GitHub Actions log/artifact

REPORT_PATH = "second_nature_discovery_report.md"

# ─────────────────────────────────────────────
# Candidates -- all guesses. None of this is confirmed against real docs.
# ─────────────────────────────────────────────

CANDIDATE_HOSTS = [
    "https://api.secondnature.ai",
    "https://api.secondnature.ai/v1",
    "https://app.secondnature.ai/api",
    "https://app.secondnature.ai/api/v1",
    "https://secondnature.ai/api",
    "https://secondnature.ai/api/v1",
    "https://public-api.secondnature.ai",
]

# name -> function(api_key) -> headers dict to merge in ("basic" is handled
# separately below since it's requests' auth=(user, pass) tuple, not a header)
HEADER_AUTH_SCHEMES = {
    "bearer":    lambda k: {"Authorization": f"Bearer {k}"},
    "x-api-key": lambda k: {"X-Api-Key": k},
    "api-key":   lambda k: {"Api-Key": k},
    "token":     lambda k: {"Authorization": f"Token {k}"},
}

# Endpoints worth checking once a host looks alive. Named after the data we
# actually need (module/session completion + progress), plus a couple of
# generic endpoints ("/me") that most REST APIs expose for auth validation.
CANDIDATE_PATHS = [
    "/me",
    "/users",
    "/reports",
    "/sessions",
    "/completions",
    "/progress",
]

request_count = 0


def log(msg: str) -> None:
    print(msg, flush=True)


def probe(url: str, headers: dict | None = None, auth: tuple | None = None) -> dict:
    """Single guarded GET. Never raises -- every outcome (including DNS/
    connection failure) is captured in the returned dict so the discovery
    loop can keep going."""
    global request_count
    if request_count >= MAX_TOTAL_REQUESTS:
        return {"skipped": True}

    request_count += 1
    time.sleep(SLEEP_BETWEEN_CALLS)

    result = {
        "url": url,
        "headers_used": list((headers or {}).keys()) or (["Basic auth"] if auth else ["none"]),
    }
    try:
        resp = requests.get(url, headers=headers, auth=auth, timeout=TIMEOUT)
        content_type = resp.headers.get("Content-Type", "")
        text = resp.text or ""
        result.update(
            {
                "reachable": True,
                "status_code": resp.status_code,
                "content_type": content_type,
                "looks_like_html": "<html" in text[:200].lower() or "text/html" in content_type,
                "snippet": text[:SNIPPET_LEN].replace("\n", " "),
            }
        )
    except requests.exceptions.RequestException as e:
        result.update({"reachable": False, "error": type(e).__name__ + ": " + str(e)[:200]})
    return result


def main() -> None:
    started = datetime.now(timezone.utc).isoformat()
    log(f"Second Nature API discovery -- started {started}")
    log(f"Trying {len(CANDIDATE_HOSTS)} candidate hosts, request cap {MAX_TOTAL_REQUESTS}.\n")

    host_liveness = {}
    alive_api_hosts = []

    # ── Phase 1: is anything even at these hosts? No auth yet -- we just
    # want to separate "DNS/connection dead" from "something answered".
    log("Phase 1: host liveness check (no auth)")
    for host in CANDIDATE_HOSTS:
        r = probe(host)
        host_liveness[host] = r
        if r.get("reachable"):
            tag = "HTML (probably the marketing site, not an API)" if r["looks_like_html"] else "non-HTML response"
            log(f"  [ALIVE] {host} -> HTTP {r['status_code']} ({tag})")
            if not r["looks_like_html"]:
                alive_api_hosts.append(host)
        else:
            log(f"  [DEAD]  {host} -> {r.get('error')}")

    if not alive_api_hosts:
        log(
            "\nNo candidate host resolved to anything that looks like an API "
            "(only dead hosts and/or HTML marketing pages). Skipping Phase 2 -- "
            "auth scheme is moot if there's no real host to test it against."
        )

    # ── Phase 2: for hosts that look like real APIs, cross auth scheme x path
    combo_results = []
    if alive_api_hosts:
        log("\nPhase 2: auth scheme x endpoint path, on hosts that looked alive")
        for host in alive_api_hosts:
            for scheme_name, header_fn in HEADER_AUTH_SCHEMES.items():
                for path in CANDIDATE_PATHS:
                    if request_count >= MAX_TOTAL_REQUESTS:
                        log("  Hit MAX_TOTAL_REQUESTS cap -- stopping early.")
                        break
                    url = host + path
                    r = probe(url, headers=header_fn(API_KEY))
                    r["auth_scheme"] = scheme_name
                    combo_results.append(r)
                    if r.get("reachable") and r["status_code"] not in (404,):
                        log(f"  [{scheme_name:10s}] {url} -> HTTP {r['status_code']}")

            # Also try HTTP Basic (key as username, blank password) --
            # this is exactly how the Close CRM API in the sibling repo works.
            for path in CANDIDATE_PATHS:
                if request_count >= MAX_TOTAL_REQUESTS:
                    break
                url = host + path
                r = probe(url, auth=(API_KEY, ""))
                r["auth_scheme"] = "basic"
                combo_results.append(r)
                if r.get("reachable") and r["status_code"] not in (404,):
                    log(f"  [{'basic':10s}] {url} -> HTTP {r['status_code']}")

    # ── Report ──────────────────────────────────────────────────────────
    promising = [
        r for r in combo_results
        if r.get("reachable") and r["status_code"] not in (404,) and not r.get("looks_like_html")
    ]
    # 401/403 still count as "promising" -- that means we found the real
    # host and just have the wrong auth format or a scoped/expired key.

    lines = []
    lines.append("# Second Nature API Discovery Report")
    lines.append("")
    lines.append(f"Run at {started} · {request_count} requests made (cap {MAX_TOTAL_REQUESTS})")
    lines.append("")
    lines.append(
        "This is a diagnostic run against **guessed** hosts/paths -- Second Nature has no "
        "public API docs. Nothing here is confirmed until a response actually contains "
        "real data (a 200 with a JSON body that looks like user/completion records)."
    )
    lines.append("")

    lines.append("## Phase 1 — host liveness")
    lines.append("")
    lines.append("| Host | Result |")
    lines.append("|---|---|")
    for host, r in host_liveness.items():
        if r.get("reachable"):
            tag = "HTML (marketing site)" if r["looks_like_html"] else f"HTTP {r['status_code']}"
            lines.append(f"| `{host}` | {tag} |")
        else:
            lines.append(f"| `{host}` | dead — {r.get('error')} |")
    lines.append("")

    if combo_results:
        lines.append("## Phase 2 — auth scheme × endpoint results")
        lines.append("")
        lines.append("| Host | Path | Auth | Status | Notes |")
        lines.append("|---|---|---|---|---|")
        for r in combo_results:
            if not r.get("reachable"):
                continue
            url = r["url"]
            host, _, path = url.partition("/api") if "/api" in url else (url, "", "")
            status = r["status_code"]
            note = "404" if status == 404 else (r["snippet"] or "(empty body)")
            lines.append(f"| `{url}` | | {r['auth_scheme']} | {status} | {note} |")
        lines.append("")

    lines.append("## Verdict")
    lines.append("")
    if promising:
        lines.append(
            f"{len(promising)} response(s) came back neither dead nor a flat 404 — "
            "check the table above. A 401/403 means we likely found the real host and "
            "just have the wrong auth header/format or a key without the right scope. "
            "A 200 means look at the snippet (truncated to "
            f"{SNIPPET_LEN} chars here on purpose) and pull the full response manually "
            "to confirm it actually contains module/session completion + progress fields, "
            "not just account/profile info."
        )
    else:
        lines.append(
            "**Nothing promising.** Every candidate host was either unreachable or only "
            "returned the marketing site / flat 404s. This confirms — rather than "
            "fails to find — that the API isn't at a guessable URL. **Next step: ask "
            "Second Nature support directly for the base URL and auth header format** "
            "(and confirm module-level completion/progress data is actually exposed, "
            "not just aggregate scores) before writing the real recap automation."
        )
    lines.append("")

    report = "\n".join(lines)
    with open(REPORT_PATH, "w") as f:
        f.write(report)

    log(f"\nReport written to {REPORT_PATH}")

    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary_path:
        with open(summary_path, "a") as f:
            f.write(report)


if __name__ == "__main__":
    main()
