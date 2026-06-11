#!/usr/bin/env python3
"""Internship scout.

Runs once per day (via GitHub Actions): pulls fresh internship postings,
asks Claude to score each one against profile.txt, and publishes a ranked
dashboard (docs/index.html) served by GitHub Pages. It keeps a running
archive of every match in matches.json, so the page shows everything found
over time with the newest run highlighted. Postings already seen are
remembered in seen.json so nothing is scored or listed twice.
"""

import hashlib
import html
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path

import requests
import yaml
from anthropic import Anthropic

ROOT = Path(__file__).parent
CONFIG = yaml.safe_load((ROOT / "config.yaml").read_text())
PROFILE = (ROOT / "profile.txt").read_text().strip()
SEEN_PATH = ROOT / "seen.json"
ARCHIVE_PATH = ROOT / "matches.json"        # running list of every match found
DOCS_DIR = ROOT / "docs"                     # GitHub Pages serves from here
PAGE_PATH = DOCS_DIR / "index.html"

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
                            "trust": "verified",
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
                "trust": "lead",
            }
        )
    return postings


def fetch_reed() -> list:
    """Postings from Reed's official UK jobseeker API (strong London coverage)."""
    if not CONFIG["search"].get("reed", {}).get("enabled", False):
        return []
    api_key = os.environ.get("REED_API_KEY")
    if not api_key:
        print("Reed enabled but REED_API_KEY not set - skipping Reed.")
        return []

    cfg = CONFIG["search"]["reed"]
    postings = []
    for query in cfg.get("queries", []):
        for where in cfg.get("locations", [""]) or [""]:
            params = {"keywords": query, "resultsToTake": 30}
            if where:
                params["locationName"] = where
                params["distanceFromLocation"] = 15
            try:
                # Reed uses HTTP basic auth: API key as username, blank password.
                resp = requests.get(
                    "https://www.reed.co.uk/api/1.0/search",
                    params=params,
                    auth=(api_key, ""),
                    timeout=30,
                )
                resp.raise_for_status()
                results = resp.json().get("results", [])
            except Exception as exc:  # noqa: BLE001
                print(f"Reed error ({query!r} / {where!r}): {exc}")
                continue
            for job in results:
                link = job.get("jobUrl")
                if not link:
                    continue
                postings.append(
                    {
                        "id": posting_id(link),
                        "title": clean(job.get("jobTitle", "Untitled")),
                        "company": clean(job.get("employerName", "Unknown")),
                        "location": clean(job.get("locationName", "UK")),
                        "url": link,
                        "snippet": clean(job.get("jobDescription", ""))[:600],
                        "source": "Reed",
                        "trust": "verified",
                    }
                )
    return postings


def fetch_company_boards() -> list:
    """Poll named firms' Greenhouse and Lever boards directly.

    Highest-trust source: these ARE the live application systems, so any
    posting returned is genuinely open. Only internship-ish roles are kept.
    """
    cfg = CONFIG["search"].get("company_boards", {})
    if not cfg.get("enabled", False):
        return []

    keywords = [k.lower() for k in cfg.get("role_keywords", ["intern", "internship"])]
    postings = []

    def relevant(title: str) -> bool:
        t = title.lower()
        return any(k in t for k in keywords)

    # --- Greenhouse: public JSON board per company token ---
    for token in cfg.get("greenhouse", []):
        url = f"https://boards-api.greenhouse.io/v1/boards/{token}/jobs"
        try:
            resp = requests.get(url, params={"content": "true"}, timeout=30)
            resp.raise_for_status()
            jobs = resp.json().get("jobs", [])
        except Exception as exc:  # noqa: BLE001
            print(f"Greenhouse error ({token}): {exc}")
            continue
        for job in jobs:
            title = job.get("title", "")
            if not relevant(title):
                continue
            link = job.get("absolute_url")
            if not link:
                continue
            loc = (job.get("location") or {}).get("name", "?")
            postings.append(
                {
                    "id": posting_id(link),
                    "title": clean(title),
                    "company": token.replace("-", " ").title(),
                    "location": clean(loc),
                    "url": link,
                    "snippet": clean(job.get("content", ""))[:600],
                    "source": f"{token} (Greenhouse)",
                    "trust": "verified",
                }
            )

    # --- Lever: public JSON postings per company token ---
    for token in cfg.get("lever", []):
        url = f"https://api.lever.co/v0/postings/{token}"
        try:
            resp = requests.get(url, params={"mode": "json"}, timeout=30)
            resp.raise_for_status()
            jobs = resp.json()
        except Exception as exc:  # noqa: BLE001
            print(f"Lever error ({token}): {exc}")
            continue
        for job in jobs if isinstance(jobs, list) else []:
            title = job.get("text", "")
            if not relevant(title):
                continue
            link = job.get("hostedUrl")
            if not link:
                continue
            loc = (job.get("categories") or {}).get("location", "?")
            postings.append(
                {
                    "id": posting_id(link),
                    "title": clean(title),
                    "company": token.replace("-", " ").title(),
                    "location": clean(loc),
                    "url": link,
                    "snippet": clean(job.get("descriptionPlain", ""))[:600],
                    "source": f"{token} (Lever)",
                    "trust": "verified",
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

Also identify the REAL hiring company. The "company" field above is sometimes a
job board or recruitment agency (e.g. "eFinancialCareers", "eFC", a staffing
firm) rather than the actual employer. If the title or description reveals the
true hiring firm, return it in "real_company". If the existing company value
already looks like a genuine employer, repeat it. If you genuinely cannot tell,
return null - do NOT guess a famous bank just because it fits the candidate.

Return ONLY a JSON array, no other text:
[{{"id": "...", "score": 0, "reason": "one short sentence", "deadline": "date if mentioned in the text, else null", "real_company": "actual employer or null"}}]"""

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
        # Replace board/agency names with the real employer when the model found one.
        real = s.get("real_company")
        if real and str(real).strip().lower() not in ("null", "none", "unknown", ""):
            p["company"] = str(real).strip()


def score_all(postings: list) -> None:
    for i in range(0, len(postings), 25):
        score_batch(postings[i : i + 25])


# ---------------------------------------------------------------- dashboard

def load_archive() -> list:
    if ARCHIVE_PATH.exists():
        try:
            return json.loads(ARCHIVE_PATH.read_text() or "[]")
        except json.JSONDecodeError:
            return []
    return []


def save_archive(archive: list) -> None:
    ARCHIVE_PATH.write_text(json.dumps(archive, ensure_ascii=False, indent=2) + "\n")


def card_html(p: dict, is_new: bool) -> str:
    score = p.get("score", 0)
    color = "#1D9E75" if score >= 80 else "#BA7517" if score >= 65 else "#5F5E5A"
    new_badge = (
        '<span style="background:#1D9E75;color:#fff;font-size:11px;font-weight:600;'
        'padding:2px 8px;border-radius:10px;margin-left:8px;">NEW</span>'
        if is_new
        else ""
    )
    if p.get("trust") == "lead":
        trust_badge = (
            '<span class="trust lead" title="Found by web search - confirm it is open before applying">'
            "&#9888; lead &mdash; verify</span>"
        )
    else:
        trust_badge = (
            '<span class="trust verified" title="From a live job feed or the firm\'s own application system">'
            "&#10003; verified open</span>"
        )
    deadline = (
        f'<div style="color:#A32D2D;font-size:13px;margin-top:6px;">'
        f"&#9201; Deadline: {html.escape(str(p['deadline']))}</div>"
        if p.get("deadline")
        else ""
    )
    found = html.escape(p.get("found_on", ""))
    return f"""
    <article class="card" data-score="{score}" data-date="{html.escape(p.get('found_on',''))}" data-trust="{html.escape(p.get('trust','verified'))}">
      <div class="cardtop">
        <div class="score" style="color:{color};">{score}<span class="outof">/100</span></div>
        {trust_badge}
      </div>
      <h3><a href="{html.escape(p['url'])}" target="_blank" rel="noopener">{html.escape(p['title'])}{new_badge}</a></h3>
      <div class="meta">{html.escape(p['company'])} &middot; {html.escape(p['location'])}</div>
      <p class="reason">{html.escape(p.get('reason',''))}</p>
      {deadline}
      <div class="foot">via {html.escape(p.get('source',''))} &middot; found {found}</div>
    </article>"""


def build_dashboard(archive: list, new_ids: set, last_run: str, scanned_today: int) -> None:
    ranked = sorted(
        archive,
        key=lambda p: (p.get("found_on", ""), p.get("score", 0)),
        reverse=True,
    )
    cards = "\n".join(card_html(p, p["id"] in new_ids) for p in ranked)
    new_count = len(new_ids)
    total = len(archive)

    page = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex, nofollow">
<title>Blackship &mdash; internship matches</title>
<style>
  :root {{ color-scheme: light dark; }}
  * {{ box-sizing: border-box; }}
  body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Arial, sans-serif;
         max-width: 760px; margin: 0 auto; padding: 28px 20px 60px;
         background: #fafafa; color: #1a1a1a; }}
  @media (prefers-color-scheme: dark) {{
    body {{ background: #121212; color: #ececec; }}
    .card {{ background: #1d1d1d !important; border-color: #333 !important; }}
    .meta, .foot {{ color: #9a9a9a !important; }}
    a {{ color: #6fb1ff !important; }}
    .controls button {{ background: #1d1d1d; color: #ececec; border-color: #333; }}
    .controls button.active {{ background: #2d4a6b; border-color: #3a5f8a; }}
  }}
  header {{ margin-bottom: 6px; }}
  h1 {{ font-size: 26px; margin: 0 0 4px; letter-spacing: -0.5px; }}
  .sub {{ color: #777; font-size: 14px; margin: 0 0 20px; }}
  .controls {{ display: flex; gap: 8px; margin-bottom: 20px; flex-wrap: wrap; }}
  .controls button {{ font-size: 13px; padding: 6px 14px; border-radius: 20px;
        border: 1px solid #ddd; background: #fff; cursor: pointer; }}
  .controls button.active {{ background: #e8f0fb; border-color: #b5d4f4; font-weight: 600; }}
  .card {{ background: #fff; border: 1px solid #e4e4e4; border-radius: 12px;
          padding: 16px 18px; margin-bottom: 14px; }}
  .cardtop {{ display: flex; align-items: center; gap: 10px; margin-bottom: 2px; }}
  .score {{ font-size: 13px; font-weight: 700; }}
  .trust {{ font-size: 11px; font-weight: 600; padding: 2px 8px; border-radius: 10px; }}
  .trust.verified {{ background: #e1f5ee; color: #0f6e56; }}
  .trust.lead {{ background: #faeeda; color: #854f0b; }}
  @media (prefers-color-scheme: dark) {{
    .trust.verified {{ background: #0f3a2e; color: #5dcaa5; }}
    .trust.lead {{ background: #3d2c0a; color: #f0c060; }}
  }}
  .outof {{ font-weight: 400; opacity: 0.6; font-size: 11px; }}
  h3 {{ margin: 0 0 4px; font-size: 17px; line-height: 1.3; }}
  h3 a {{ color: #1a1a1a; text-decoration: none; }}
  h3 a:hover {{ text-decoration: underline; }}
  .meta {{ color: #555; font-size: 14px; }}
  .reason {{ font-size: 14px; margin: 8px 0 0; line-height: 1.5; }}
  .foot {{ color: #aaa; font-size: 12px; margin-top: 8px; }}
  .empty {{ color: #888; padding: 40px 0; text-align: center; }}
</style>
</head>
<body>
<header>
  <h1>Blackship</h1>
  <p class="sub">{total} matches found over time &middot; {new_count} new this run &middot;
     {scanned_today} postings scanned today &middot; last run {html.escape(last_run)}</p>
</header>
<div class="controls">
  <button class="active" onclick="filterCards('all', this)">All</button>
  <button onclick="filterCards('new', this)">New only</button>
  <button onclick="filterCards('verified', this)">Verified open only</button>
  <button onclick="sortCards('score')">Sort by score</button>
  <button onclick="sortCards('date')">Sort by date</button>
</div>
<main id="list">
{cards if ranked else '<p class="empty">No matches yet. The agent will fill this in on its next run.</p>'}
</main>
<script>
  const list = document.getElementById('list');
  const cards = () => Array.from(list.querySelectorAll('.card'));
  function filterCards(mode, btn) {{
    document.querySelectorAll('.controls button').forEach(b => b.classList.remove('active'));
    if (btn) btn.classList.add('active');
    cards().forEach(c => {{
      const isNew = c.querySelector('h3 a').textContent.includes('NEW');
      const isVerified = c.dataset.trust === 'verified';
      let show = true;
      if (mode === 'new') show = isNew;
      else if (mode === 'verified') show = isVerified;
      c.style.display = show ? '' : 'none';
    }});
  }}
  function sortCards(key) {{
    const sorted = cards().sort((a, b) => {{
      if (key === 'score') return b.dataset.score - a.dataset.score;
      return b.dataset.date.localeCompare(a.dataset.date);
    }});
    sorted.forEach(c => list.appendChild(c));
  }}
</script>
</body>
</html>"""
    DOCS_DIR.mkdir(exist_ok=True)
    PAGE_PATH.write_text(page)
    # .nojekyll tells GitHub Pages to serve the file as-is, no processing
    (DOCS_DIR / ".nojekyll").write_text("")


# ---------------------------------------------------------------- main

def main() -> None:
    seen = load_seen()
    archive = load_archive()
    run_stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    postings = (
        fetch_adzuna()
        + fetch_reed()
        + fetch_company_boards()
        + fetch_claude_discovery()
    )

    fresh, batch_ids = [], set()
    for p in postings:
        if p["id"] in seen or p["id"] in batch_ids:
            continue
        batch_ids.add(p["id"])
        fresh.append(p)
    print(f"Fetched {len(postings)} postings, {len(fresh)} new.")

    new_ids = set()
    if fresh:
        score_all(fresh)
        min_score = CONFIG["scoring"].get("min_score", 55)
        for p in fresh:
            if p["score"] >= min_score:
                p["found_on"] = run_stamp
                archive.append(p)
                new_ids.add(p["id"])
        print(f"{len(new_ids)} of {len(fresh)} new postings cleared min_score={min_score}.")
    else:
        print("Nothing new today.")

    # Trim the archive so the page never grows without bound.
    cap = CONFIG["dashboard"].get("max_items", 300)
    archive = sorted(archive, key=lambda p: p.get("found_on", ""), reverse=True)[:cap]

    build_dashboard(archive, new_ids, run_stamp, len(fresh))
    save_archive(archive)
    seen |= batch_ids
    save_seen(seen)
    print(f"Dashboard rebuilt: {len(archive)} matches shown, {len(new_ids)} flagged new.")


if __name__ == "__main__":
    main()
