#!/usr/bin/env python3
"""Internship scout.

Runs once per day (via GitHub Actions): pulls fresh internship postings,
asks Claude to score each one against profile.txt, and emails a ranked
digest of everything that clears the bar. Postings it has already alerted
on are remembered in seen.json so nothing is sent twice.
"""

import hashlib
import html
import json
import os
import re
import smtplib
from datetime import datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

import requests
import yaml
from anthropic import Anthropic

ROOT = Path(__file__).parent
CONFIG = yaml.safe_load((ROOT / "config.yaml").read_text())
PROFILE = (ROOT / "profile.txt").read_text().strip()
SEEN_PATH = ROOT / "seen.json"

TAG_RE = re.compile(r"<[^>]+>")

_client = None


def claude() -> Anthropic:
    """Lazy client so importing this module never needs an API key."""
    global _client
    if _client is None:
        _client = Anthropic()  # reads ANTHROPIC_API_KEY from the environment
    return _client


# ---------------------------------------------------------------- helpers

def clean(text: str) -> str:
    return html.unescape(TAG_RE.sub("", text or "")).strip()


def posting_id(url: str) -> str:
    return hashlib.sha1(url.encode()).hexdigest()[:16]


def parse_json_array(text: str) -> list:
    """Pull the first JSON array out of a model response, fences and all."""
    text = re.sub(r"```(?:json)?", "", text)
    start, end = text.find("["), text.rfind("]")
    if start == -1 or end == -1:
        return []
    try:
        data = json.loads(text[start : end + 1])
    except json.JSONDecodeError:
        return []
    return data if isinstance(data, list) else []


def load_seen() -> set:
    if SEEN_PATH.exists():
        return set(json.loads(SEEN_PATH.read_text() or "[]"))
    return set()


def save_seen(seen: set) -> None:
    SEEN_PATH.write_text(json.dumps(sorted(seen), indent=2) + "\n")


# ---------------------------------------------------------------- sources

def fetch_adzuna() -> list:
    """Structured postings from Adzuna's official job-search API."""
    app_id = os.environ.get("ADZUNA_APP_ID")
    app_key = os.environ.get("ADZUNA_APP_KEY")
    if not (app_id and app_key):
        print("Adzuna keys not set - skipping Adzuna.")
        return []

    cfg = CONFIG["search"]["adzuna"]
    postings = []
    for country, places in cfg["countries"].items():
        for query in cfg["queries"]:
            for where in places or [""]:
                params = {
                    "app_id": app_id,
                    "app_key": app_key,
                    "what": query,
                    "results_per_page": 30,
                    "max_days_old": cfg.get("max_days_old", 2),
                    "sort_by": "date",
                }
                if where:
                    params["where"] = where
                url = f"https://api.adzuna.com/v1/api/jobs/{country}/search/1"
                try:
                    resp = requests.get(url, params=params, timeout=30)
                    resp.raise_for_status()
                    results = resp.json().get("results", [])
                except Exception as exc:  # noqa: BLE001 - one bad query shouldn't kill the run
                    print(f"Adzuna error ({country} / {query!r}): {exc}")
                    continue
                for job in results:
                    link = job.get("redirect_url")
                    if not link:
                        continue
                    postings.append(
                        {
                            "id": posting_id(link),
                            "title": clean(job.get("title", "Untitled")),
                            "company": (job.get("company") or {}).get("display_name", "Unknown"),
                            "location": (job.get("location") or {}).get("display_name", country.upper()),
                            "url": link,
                            "snippet": clean(job.get("description", ""))[:600],
                            "source": "Adzuna",
                        }
                    )
    return postings


def fetch_claude_discovery() -> list:
    """Optional extra pass: Claude searches the web itself.

    Catches the postings aggregators miss - spring weeks and insight
    programmes often live only on the firm's own careers page.
    """
    if not CONFIG["search"].get("claude_discovery", False):
        return []

    prompt = f"""Search the web for business / finance internships, spring weeks,
insight programmes, off-cycle internships and summer analyst roles that are
CURRENTLY OPEN for applications, in the locations this candidate targets.
Prefer official company career pages and reputable boards. Use at most 4 searches.

CANDIDATE PROFILE (use it to decide what is relevant):
{PROFILE}

Return ONLY a JSON array, no other text. Each item:
{{"title": "...", "company": "...", "location": "...", "url": "..."}}
At most 10 items. Only include postings with a real, direct URL."""

    try:
        resp = claude().messages.create(
            model=CONFIG["scoring"]["model"],
            max_tokens=3000,
            messages=[{"role": "user", "content": prompt}],
            tools=[{"type": "web_search_20250305", "name": "web_search"}],
        )
    except Exception as exc:  # noqa: BLE001
        print(f"Claude discovery failed (continuing without it): {exc}")
        return []

    text = "".join(block.text for block in resp.content if block.type == "text")
    postings = []
    for item in parse_json_array(text):
        url = str(item.get("url") or "").strip()
        if not url.startswith("http"):
            continue
        postings.append(
            {
                "id": posting_id(url),
                "title": str(item.get("title", "Untitled")),
                "company": str(item.get("company", "Unknown")),
                "location": str(item.get("location", "?")),
                "url": url,
                "snippet": "",
                "source": "Claude web search",
            }
        )
    return postings


# ---------------------------------------------------------------- scoring

def score_batch(postings: list) -> None:
    listing = [
        {
            "id": p["id"],
            "title": p["title"],
            "company": p["company"],
            "location": p["location"],
            "description": p["snippet"][:500],
        }
        for p in postings
    ]
    prompt = f"""You are screening internship postings for one specific candidate.

CANDIDATE PROFILE:
{PROFILE}

POSTINGS (JSON):
{json.dumps(listing, ensure_ascii=False)}

Score EVERY posting 0-100 for fit:
- 80+  strong fit: right field, right level, right location, candidate eligible
- 50-79 plausible fit with caveats
- <50  wrong field, wrong seniority, wrong location, or candidate likely ineligible
Weigh hard constraints heavily (excluded locations, right-to-work / visa,
graduation year, language requirements). Penalise non-internship roles,
unpaid roles, and anything that smells like spam or a fake listing.

Return ONLY a JSON array, no other text:
[{{"id": "...", "score": 0, "reason": "one short sentence", "deadline": "date if mentioned in the text, else null"}}]"""

    resp = claude().messages.create(
        model=CONFIG["scoring"]["model"],
        max_tokens=4000,
        messages=[{"role": "user", "content": prompt}],
    )
    text = "".join(block.text for block in resp.content if block.type == "text")
    scores = {s["id"]: s for s in parse_json_array(text) if isinstance(s, dict) and "id" in s}

    for p in postings:
        s = scores.get(p["id"], {})
        try:
            p["score"] = int(s.get("score", 0) or 0)
        except (TypeError, ValueError):
            p["score"] = 0
        p["reason"] = s.get("reason") or "No assessment returned."
        deadline = s.get("deadline")
        p["deadline"] = None if (not deadline or str(deadline).lower() == "null") else str(deadline)


def score_all(postings: list) -> None:
    for i in range(0, len(postings), 25):
        score_batch(postings[i : i + 25])


# ---------------------------------------------------------------- email

def render_email(matches: list, total_new: int) -> str:
    rows = []
    for p in matches:
        color = "#1D9E75" if p["score"] >= 80 else "#BA7517" if p["score"] >= 65 else "#5F5E5A"
        deadline = (
            f'<div style="color:#A32D2D;font-size:13px;margin-top:4px;">'
            f"Deadline: {html.escape(p['deadline'])}</div>"
            if p.get("deadline")
            else ""
        )
        rows.append(
            f"""
      <div style="border:1px solid #e4e4e4;border-radius:8px;padding:14px 16px;margin:0 0 12px;">
        <div style="font-size:13px;font-weight:bold;color:{color};margin-bottom:2px;">{p['score']}/100</div>
        <a href="{html.escape(p['url'])}" style="font-size:16px;font-weight:bold;color:#1a1a1a;">{html.escape(p['title'])}</a>
        <div style="color:#555;font-size:14px;margin-top:2px;">{html.escape(p['company'])} &middot; {html.escape(p['location'])}</div>
        <div style="font-size:14px;margin-top:6px;">{html.escape(p['reason'])}</div>
        {deadline}
        <div style="color:#999;font-size:12px;margin-top:6px;">via {html.escape(p['source'])}</div>
      </div>"""
        )
    return f"""<div style="font-family:Arial,Helvetica,sans-serif;max-width:640px;margin:auto;">
    <h2 style="font-weight:600;">Internship matches &mdash; {datetime.now():%d %b %Y}</h2>
    <p style="color:#555;">Scanned {total_new} new postings today; these {len(matches)} cleared your bar.</p>
    {''.join(rows)}
    <p style="color:#999;font-size:12px;">Sent by your internship scout.
    Edit profile.txt and config.yaml in the repo to change what gets through.</p>
  </div>"""


def send_email(matches: list, total_new: int) -> None:
    em = CONFIG["email"]
    user = os.environ["SMTP_USER"]
    password = os.environ["SMTP_PASS"]
    to_addr = os.environ.get("EMAIL_TO") or user

    n = len(matches)
    msg = MIMEMultipart("alternative")
    msg["Subject"] = f"{n} internship match{'es' if n != 1 else ''} today (top score {matches[0]['score']})"
    msg["From"] = f"{em.get('from_name', 'Internship scout')} <{user}>"
    msg["To"] = to_addr
    msg.attach(MIMEText(render_email(matches, total_new), "html"))

    with smtplib.SMTP(em.get("smtp_host", "smtp.gmail.com"), em.get("smtp_port", 587)) as server:
        server.starttls()
        server.login(user, password)
        server.sendmail(user, [to_addr], msg.as_string())


# ---------------------------------------------------------------- main

def main() -> None:
    seen = load_seen()

    postings = fetch_adzuna() + fetch_claude_discovery()

    fresh, batch_ids = [], set()
    for p in postings:
        if p["id"] in seen or p["id"] in batch_ids:
            continue
        batch_ids.add(p["id"])
        fresh.append(p)
    print(f"Fetched {len(postings)} postings, {len(fresh)} new.")

    if not fresh:
        print("Nothing new today.")
        return

    score_all(fresh)

    min_score = CONFIG["scoring"].get("min_score", 55)
    max_alerts = CONFIG["scoring"].get("max_alerts", 12)
    matches = sorted(
        (p for p in fresh if p["score"] >= min_score),
        key=lambda p: p["score"],
        reverse=True,
    )[:max_alerts]

    if matches:
        send_email(matches, len(fresh))
        print(f"Emailed {len(matches)} matches (top score {matches[0]['score']}).")
    else:
        print(f"{len(fresh)} new postings, but none cleared min_score={min_score}.")

    seen |= batch_ids
    save_seen(seen)


if __name__ == "__main__":
    main()
