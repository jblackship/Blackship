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


def fetch_muse() -> list:
    """Postings from The Muse public API. No key needed (key just raises the
    rate limit). Filters by category + location, newest first."""
    cfg = CONFIG["search"].get("muse", {})
    if not cfg.get("enabled", False):
        return []
    api_key = os.environ.get("MUSE_API_KEY")  # optional
    postings = []
    for category in cfg.get("categories", []):
        for location in cfg.get("locations", [""]) or [""]:
            params = {"category": category, "page": 1, "descending": "true"}
            if location:
                params["location"] = location
            if api_key:
                params["api_key"] = api_key
            try:
                resp = requests.get(
                    "https://www.themuse.com/api/public/jobs",
                    params=params,
                    timeout=30,
                )
                resp.raise_for_status()
                results = resp.json().get("results", [])
            except Exception as exc:  # noqa: BLE001
                print(f"Muse error ({category!r} / {location!r}): {exc}")
                continue
            for job in results:
                link = (job.get("refs") or {}).get("landing_page")
                if not link:
                    continue
                locs = ", ".join(l.get("name", "") for l in job.get("locations", []) if l.get("name"))
                postings.append(
                    {
                        "id": posting_id(link),
                        "title": clean(job.get("name", "Untitled")),
                        "company": clean((job.get("company") or {}).get("name", "Unknown")),
                        "location": clean(locs or "?"),
                        "url": link,
                        "snippet": clean(job.get("contents", ""))[:600],
                        "source": "The Muse",
                        "trust": "verified",
                    }
                )
    return postings


def fetch_jooble() -> list:
    """Postings from Jooble's REST API (POST with JSON body). Needs a free key.

    Jooble's free tier is a hard cap of 500 requests total, and each run costs
    one request per query. To preserve the budget, this only runs at the UTC
    hours listed in config 'run_at_hours' (e.g. [8, 20] = twice a day)."""
    cfg = CONFIG["search"].get("jooble", {})
    if not cfg.get("enabled", False):
        return []

    run_hours = cfg.get("run_at_hours")
    if run_hours is not None:
        current_hour = datetime.now(timezone.utc).hour
        if current_hour not in run_hours:
            print(f"Jooble: skipping this run (hour {current_hour} not in {run_hours}); preserving request budget.")
            return []

    api_key = os.environ.get("JOOBLE_API_KEY")
    if not api_key:
        print("Jooble enabled but JOOBLE_API_KEY not set - skipping Jooble.")
        return []

    postings = []
    for query in cfg.get("queries", []):
        for location in cfg.get("locations", [""]) or [""]:
            body = {"keywords": query, "page": "1"}
            if location:
                body["location"] = location
            try:
                resp = requests.post(
                    f"https://jooble.org/api/{api_key}",
                    json=body,
                    timeout=30,
                )
                resp.raise_for_status()
                jobs = resp.json().get("jobs", [])
            except Exception as exc:  # noqa: BLE001
                print(f"Jooble error ({query!r} / {location!r}): {exc}")
                continue
            for job in jobs:
                link = job.get("link")
                if not link:
                    continue
                postings.append(
                    {
                        "id": posting_id(link),
                        "title": clean(job.get("title", "Untitled")),
                        "company": clean(job.get("company", "") or "Unknown"),
                        "location": clean(job.get("location", "") or "?"),
                        "url": link,
                        "snippet": clean(job.get("snippet", ""))[:600],
                        "source": "Jooble",
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

    keep_locations = [l.lower() for l in cfg.get("only_locations", [])]

    def relevant(title: str) -> bool:
        t = title.lower()
        return any(k in t for k in keywords)

    def location_ok(loc: str) -> bool:
        if not keep_locations:
            return True
        loc_l = (loc or "").lower()
        return any(k in loc_l for k in keep_locations)

    # --- Greenhouse: public JSON board per company token (region-agnostic API) ---
    for token in cfg.get("greenhouse", []):
        url = f"https://boards-api.greenhouse.io/v1/boards/{token}/jobs"
        try:
            resp = requests.get(url, params={"content": "true"}, timeout=30)
            resp.raise_for_status()
            jobs = resp.json().get("jobs", [])
        except Exception as exc:  # noqa: BLE001
            print(f"Greenhouse error ({token}): {exc}")
            continue
        kept = 0
        for job in jobs:
            title = job.get("title", "")
            if not relevant(title):
                continue
            loc = (job.get("location") or {}).get("name", "?")
            if not location_ok(loc):
                continue
            link = job.get("absolute_url")
            if not link:
                continue
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
            kept += 1
        print(f"Greenhouse {token}: {kept} relevant internship postings.")

    # --- Lever: public JSON postings; try US host then EU host ---
    for token in cfg.get("lever", []):
        jobs = None
        for host in ("api.lever.co", "api.eu.lever.co"):
            try:
                resp = requests.get(
                    f"https://{host}/v0/postings/{token}",
                    params={"mode": "json"},
                    timeout=30,
                )
                resp.raise_for_status()
                data = resp.json()
                if isinstance(data, list) and data:
                    jobs = data
                    break
            except Exception:  # noqa: BLE001 - try the next host before giving up
                continue
        if jobs is None:
            print(f"Lever error ({token}): no postings on either US or EU host.")
            continue
        kept = 0
        for job in jobs:
            title = job.get("text", "")
            if not relevant(title):
                continue
            loc = (job.get("categories") or {}).get("location", "?")
            if not location_ok(loc):
                continue
            link = job.get("hostedUrl")
            if not link:
                continue
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
            kept += 1
        print(f"Lever {token}: {kept} relevant internship postings.")
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

For EACH posting also provide, judged against THIS candidate specifically:
- "pros": up to 3 short bullet phrases (max ~8 words each) on why it fits them.
  Be specific to the role/firm, not generic. Fewer than 3 is fine.
- "cons": up to 3 short bullet phrases on risks or poor-fit aspects (e.g.
  location mismatch, visa/eligibility doubt, seniority, vague posting). Fewer
  than 3 is fine; use [] only if there are genuinely none.
- "analysis": 2-4 sentences of plain-prose reasoning a candidate could act on -
  why the score is what it is, what stands out, what to check before applying.
  Write it TO the candidate. No markdown, no headings.

Return ONLY a JSON array, no other text:
[{{"id": "...", "score": 0, "reason": "one short sentence", "deadline": "date or null", "real_company": "employer or null", "pros": ["...","..."], "cons": ["...","..."], "analysis": "..."}}]"""

    resp = claude().messages.create(
        model=CONFIG["scoring"]["model"],
        max_tokens=8000,
        messages=[{"role": "user", "content": prompt}],
    )
    text = "".join(block.text for block in resp.content if block.type == "text")
    scores = {s["id"]: s for s in parse_json_array(text) if isinstance(s, dict) and "id" in s}

    def clean_list(val, cap=3):
        if not isinstance(val, list):
            return []
        out = [str(x).strip() for x in val if str(x).strip()]
        return out[:cap]

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
        p["pros"] = clean_list(s.get("pros"))
        p["cons"] = clean_list(s.get("cons"))
        p["analysis"] = str(s.get("analysis") or "").strip() or p["reason"]


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


def analysis_page_html(p: dict) -> str:
    """Standalone page with the deeper why-it-fits analysis for one posting."""
    score = p.get("score", 0)
    color = "#1D9E75" if score >= 80 else "#BA7517" if score >= 65 else "#5F5E5A"
    pros = "".join(f"<li>{html.escape(x)}</li>" for x in p.get("pros", [])) or "<li>None noted.</li>"
    cons = "".join(f"<li>{html.escape(x)}</li>" for x in p.get("cons", [])) or "<li>None noted.</li>"
    deadline = (
        f'<p class="deadline">Deadline: {html.escape(str(p["deadline"]))}</p>'
        if p.get("deadline") else ""
    )
    return f"""<!DOCTYPE html>
<html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex, nofollow">
<title>{html.escape(p['title'])} &mdash; analysis</title>
<style>
  :root {{ color-scheme: light dark; }}
  body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Arial, sans-serif;
         max-width: 680px; margin: 0 auto; padding: 28px 20px 60px;
         background: #fafafa; color: #1a1a1a; line-height: 1.55; }}
  @media (prefers-color-scheme: dark) {{ body {{ background: #121212; color: #ececec; }} a {{ color: #6fb1ff; }} }}
  a.back {{ font-size: 14px; text-decoration: none; }}
  h1 {{ font-size: 22px; margin: 14px 0 2px; }}
  .meta {{ color: #777; font-size: 15px; margin-bottom: 4px; }}
  .score {{ font-weight: 700; color: {color}; font-size: 15px; }}
  .found {{ color: #999; font-size: 13px; margin: 6px 0 18px; }}
  h2 {{ font-size: 15px; margin: 22px 0 6px; text-transform: uppercase; letter-spacing: 0.5px; color: #888; }}
  ul {{ margin: 0; padding-left: 20px; }} li {{ margin: 3px 0; }}
  .analysis {{ font-size: 16px; }}
  .apply {{ display: inline-block; margin-top: 22px; padding: 10px 18px; background: #2d4a6b;
           color: #fff; border-radius: 8px; text-decoration: none; font-weight: 600; }}
  .deadline {{ color: #A32D2D; font-weight: 600; }}
</style></head><body>
<a class="back" href="../index.html">&larr; Back to Blackship</a>
<h1>{html.escape(p['title'])}</h1>
<div class="meta">{html.escape(p['company'])} &middot; {html.escape(p['location'])}</div>
<div class="score">Fit score: {score}/100</div>
<div class="found">Found {html.escape(p.get('found_on',''))} &middot; via {html.escape(p.get('source',''))}</div>
{deadline}
<h2>Why it fits</h2>
<ul>{pros}</ul>
<h2>Watch-outs</h2>
<ul>{cons}</ul>
<h2>Analysis</h2>
<p class="analysis">{html.escape(p.get('analysis',''))}</p>
<a class="apply" href="{html.escape(p['url'])}" target="_blank" rel="noopener">Open application &rarr;</a>
</body></html>"""


def card_html(p: dict) -> str:
    score = p.get("score", 0)
    color = "#1D9E75" if score >= 80 else "#BA7517" if score >= 65 else "#5F5E5A"
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
        f'<span class="deadline">&#9201; {html.escape(str(p["deadline"]))}</span>'
        if p.get("deadline") else ""
    )
    pros = "".join(f"<li class='pro'>{html.escape(x)}</li>" for x in p.get("pros", []))
    cons = "".join(f"<li class='con'>{html.escape(x)}</li>" for x in p.get("cons", []))
    proscons = (
        f"<ul class='proscons'>{pros}{cons}</ul>" if (pros or cons)
        else f"<p class='reason'>{html.escape(p.get('reason',''))}</p>"
    )
    found = html.escape(p.get("found_on", ""))
    pid = html.escape(p["id"])
    analysis_link = f"jobs/{pid}.html"
    return f"""
    <article class="card" data-id="{pid}" data-score="{score}" data-found="{found}" data-trust="{html.escape(p.get('trust','verified'))}">
      <div class="cardtop">
        <div class="score" style="color:{color};">{score}<span class="outof">/100</span></div>
        {trust_badge}
        <span class="newbadge" data-found="{found}"></span>
        <label class="hidebox"><input type="checkbox" onchange="toggleHide('{pid}')"> hide</label>
      </div>
      <h3><a href="{html.escape(p['url'])}" target="_blank" rel="noopener">{html.escape(p['title'])}</a></h3>
      <div class="meta">{html.escape(p['company'])} &middot; {html.escape(p['location'])}</div>
      {proscons}
      <div class="cardfoot">
        <span class="found">Found {found}</span>
        {deadline}
        <a class="analysis-link" href="{analysis_link}">Full analysis &rarr;</a>
      </div>
    </article>"""


def build_dashboard(archive: list, new_ids: set, last_run: str, scanned_today: int) -> None:
    # Newest first by default ("New openings" view).
    ranked = sorted(
        archive,
        key=lambda p: (p.get("found_on", ""), p.get("score", 0)),
        reverse=True,
    )

    # Write one analysis page per posting.
    jobs_dir = DOCS_DIR / "jobs"
    jobs_dir.mkdir(parents=True, exist_ok=True)
    for p in ranked:
        (jobs_dir / f"{p['id']}.html").write_text(analysis_page_html(p))

    cards = "\n".join(card_html(p) for p in ranked)
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
    .meta, .cardfoot, .found {{ color: #9a9a9a !important; }}
    a {{ color: #6fb1ff !important; }}
    .tabs button {{ background: #1d1d1d; color: #ececec; border-color: #333; }}
    .tabs button.active {{ background: #2d4a6b; border-color: #3a5f8a; }}
  }}
  header {{ margin-bottom: 6px; }}
  h1 {{ font-size: 26px; margin: 0 0 4px; letter-spacing: -0.5px; }}
  .sub {{ color: #777; font-size: 14px; margin: 0 0 18px; }}
  .tabs {{ display: flex; gap: 8px; margin-bottom: 18px; flex-wrap: wrap; }}
  .tabs button {{ font-size: 13px; padding: 6px 14px; border-radius: 20px;
        border: 1px solid #ddd; background: #fff; cursor: pointer; }}
  .tabs button.active {{ background: #e8f0fb; border-color: #b5d4f4; font-weight: 600; }}
  .card {{ background: #fff; border: 1px solid #e4e4e4; border-radius: 12px;
          padding: 16px 18px; margin-bottom: 14px; }}
  .cardtop {{ display: flex; align-items: center; gap: 10px; margin-bottom: 4px; }}
  .score {{ font-size: 13px; font-weight: 700; }}
  .outof {{ font-weight: 400; opacity: 0.6; font-size: 11px; }}
  .trust {{ font-size: 11px; font-weight: 600; padding: 2px 8px; border-radius: 10px; }}
  .trust.verified {{ background: #e1f5ee; color: #0f6e56; }}
  .trust.lead {{ background: #faeeda; color: #854f0b; }}
  @media (prefers-color-scheme: dark) {{
    .trust.verified {{ background: #0f3a2e; color: #5dcaa5; }}
    .trust.lead {{ background: #3d2c0a; color: #f0c060; }}
  }}
  .newbadge.show {{ background: #1D9E75; color: #fff; font-size: 11px; font-weight: 600;
        padding: 2px 8px; border-radius: 10px; }}
  .hidebox {{ margin-left: auto; font-size: 12px; color: #999; cursor: pointer; user-select: none; }}
  h3 {{ margin: 2px 0 4px; font-size: 17px; line-height: 1.3; }}
  h3 a {{ color: #1a1a1a; text-decoration: none; }}
  h3 a:hover {{ text-decoration: underline; }}
  .meta {{ color: #555; font-size: 14px; margin-bottom: 8px; }}
  .proscons {{ list-style: none; margin: 8px 0 0; padding: 0; font-size: 14px; }}
  .proscons li {{ padding: 1px 0 1px 20px; position: relative; line-height: 1.45; }}
  .proscons li.pro::before {{ content: "+"; position: absolute; left: 4px; color: #1D9E75; font-weight: 700; }}
  .proscons li.con::before {{ content: "\\2212"; position: absolute; left: 4px; color: #BA7517; font-weight: 700; }}
  .reason {{ font-size: 14px; margin: 8px 0 0; line-height: 1.5; }}
  .cardfoot {{ display: flex; align-items: center; gap: 14px; flex-wrap: wrap;
        margin-top: 12px; font-size: 13px; color: #888; }}
  .found {{ font-weight: 600; color: #555; }}
  .deadline {{ color: #A32D2D; font-weight: 600; }}
  .analysis-link {{ margin-left: auto; font-weight: 600; text-decoration: none; }}
  .empty {{ color: #888; padding: 40px 0; text-align: center; }}
</style>
</head>
<body>
<header>
  <h1>Blackship</h1>
  <p class="sub">{total} matches &middot; {new_count} new this run &middot;
     {scanned_today} scanned today &middot; last run {html.escape(last_run)}</p>
</header>
<div class="tabs">
  <button id="tab-new" class="active" onclick="setView('new', this)">New openings</button>
  <button id="tab-best" onclick="setView('best', this)">Best fits</button>
  <button id="tab-hidden" onclick="setView('hidden', this)">Hidden (<span id="hidden-count">0</span>)</button>
</div>
<main id="list">
{cards if ranked else '<p class="empty">No matches yet. The agent will fill this in on its next run.</p>'}
</main>
<script>
  const HIDE_KEY = 'blackship_hidden_v1';
  const list = document.getElementById('list');
  const cards = () => Array.from(list.querySelectorAll('.card'));

  function getHidden() {{
    try {{ return new Set(JSON.parse(localStorage.getItem(HIDE_KEY) || '[]')); }}
    catch (e) {{ return new Set(); }}
  }}
  function saveHidden(set) {{
    try {{ localStorage.setItem(HIDE_KEY, JSON.stringify([...set])); }} catch (e) {{}}
  }}
  function toggleHide(id) {{
    const h = getHidden();
    if (h.has(id)) h.delete(id); else h.add(id);
    saveHidden(h);
    render();
  }}

  // Expire the NEW badge 24h after a posting was found.
  function markNew() {{
    const now = Date.now();
    cards().forEach(c => {{
      const badge = c.querySelector('.newbadge');
      const foundStr = (c.dataset.found || '').replace(' UTC', 'Z').replace(' ', 'T');
      const t = Date.parse(foundStr);
      if (!isNaN(t) && (now - t) < 24*60*60*1000) {{ badge.textContent = 'NEW'; badge.classList.add('show'); }}
      else {{ badge.textContent = ''; badge.classList.remove('show'); }}
    }});
  }}

  let currentView = 'new';
  function setView(view, btn) {{
    currentView = view;
    document.querySelectorAll('.tabs button').forEach(b => b.classList.remove('active'));
    if (btn) btn.classList.add('active');
    render();
  }}

  function render() {{
    const hidden = getHidden();
    document.getElementById('hidden-count').textContent = hidden.size;
    // sync each checkbox to stored state
    cards().forEach(c => {{
      const box = c.querySelector('.hidebox input');
      if (box) box.checked = hidden.has(c.dataset.id);
    }});
    // sort
    const sorted = cards().sort((a, b) => {{
      if (currentView === 'best') return b.dataset.score - a.dataset.score;
      return (b.dataset.found || '').localeCompare(a.dataset.found || '');
    }});
    sorted.forEach(c => list.appendChild(c));
    // filter by view
    let shown = 0;
    cards().forEach(c => {{
      const isHidden = hidden.has(c.dataset.id);
      const show = (currentView === 'hidden') ? isHidden : !isHidden;
      c.style.display = show ? '' : 'none';
      if (show) shown++;
    }});
  }}

  markNew();
  render();
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
        + fetch_muse()
        + fetch_jooble()
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
