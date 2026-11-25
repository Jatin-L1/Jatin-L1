#!/usr/bin/env python3
"""Refresh data/contributions.json and data/repos.json (run daily by the Action).

    GH_TOKEN=... python scripts/fetch_contributions.py [--login Jatin-L1]

The default Actions GITHUB_TOKEN sees *public* contributions only. To include
private ones, add a classic PAT with `read:user` as the repo secret PROFILE_TOKEN
(the workflow prefers it) and enable "Include private contributions on my profile".

Exits non-zero on any failure so the workflow never commits stale or partial data.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from render_cards import FEATURED, LOGIN  # noqa: E402  (single source of truth for repo names)

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"

QUERY = """
query($login: String!, $from: DateTime!, $to: DateTime!) {
  user(login: $login) {
    contributionsCollection(from: $from, to: $to) {
      contributionCalendar {
        totalContributions
        weeks { contributionDays { date contributionCount } }
      }
    }
  }
}
"""


def call(url: str, token: str, body: dict | None = None):
    req = urllib.request.Request(
        url,
        data=json.dumps(body).encode() if body else None,
        headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json",
                 "User-Agent": "profile-cards", "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--login", default=LOGIN)
    args = ap.parse_args()
    token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
    if not token:
        sys.exit("Set GH_TOKEN (or GITHUB_TOKEN).")

    today = dt.datetime.now(dt.timezone.utc).date()
    start = today - dt.timedelta(days=364)  # GraphQL allows at most one year per query
    try:
        res = call("https://api.github.com/graphql", token, {
            "query": QUERY,
            "variables": {"login": args.login, "from": f"{start}T00:00:00Z", "to": f"{today}T23:59:59Z"},
        })
    except urllib.error.HTTPError as e:
        sys.exit(f"GraphQL request failed: HTTP {e.code} {e.reason}")
    if res.get("errors") or not (res.get("data") or {}).get("user"):
        sys.exit(f"GraphQL error: {res.get('errors') or 'user not found'}")

    cal = res["data"]["user"]["contributionsCollection"]["contributionCalendar"]
    days = sorted((d["date"], d["contributionCount"]) for w in cal["weeks"] for d in w["contributionDays"]
                  if start.isoformat() <= d["date"] <= today.isoformat())
    DATA.mkdir(exist_ok=True)
    (DATA / "contributions.json").write_text(json.dumps({
        "login": args.login,
        "from": days[0][0], "to": days[-1][0],
        "total": sum(c for _, c in days),
        "days": days,
    }, separators=(",", ":")) + "\n", encoding="utf-8")
    print(f"contributions.json: {len(days)} days, {sum(c for _, c in days)} contributions")

    repos = {}
    for spec in FEATURED:
        name = spec["repo"]
        try:
            r = call(f"https://api.github.com/repos/{args.login}/{name}", token)
        except urllib.error.HTTPError as e:
            print(f"  warning: {args.login}/{name} -> HTTP {e.code}; card will render without stats")
            continue
        repos[name] = {k: r.get(k) for k in ("language", "stargazers_count", "forks_count", "pushed_at")}
    (DATA / "repos.json").write_text(json.dumps(repos, indent=1) + "\n", encoding="utf-8")
    print(f"repos.json: {len(repos)}/{len(FEATURED)} repos")


if __name__ == "__main__":
    main()
