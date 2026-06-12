# Blackship

An agent that scans for business/finance internships every hour, scores each one
against your profile with Claude, and publishes a ranked dashboard you open in your
browser. It keeps a running list of everything found over time.

## The files

| File | What it does |
|---|---|
| `agent.py` | The whole pipeline: fetch -> dedupe -> score -> publish dashboard |
| `profile.txt` | Who you are. **Matching quality lives in this file.** |
| `config.yaml` | Sources, search queries, score threshold, firm watchlists |
| `.github/workflows/daily.yml` | The hourly schedule (GitHub Actions) |
| `seen.json` | Memory of what's been scored |
| `matches.json` | The running archive of every match |
| `docs/` | The dashboard + per-job analysis pages (created on first run) |

## Sources (all free, all verified-live)

- **Adzuna** and **Reed** - official UK/EU job-search APIs (need free keys)
- **The Muse** - public jobs API (no key needed)
- **Jooble** - aggregator API (free key; capped to 2 runs/day to protect the 500-request budget)
- **Greenhouse + Lever company boards** - polls ~14 named firms' own application
  systems directly, so every posting is genuinely open. Edit the token lists in
  `config.yaml` to add firms.
- **Claude web search** - currently disabled (`claude_discovery: false`); was a
  lead-finder, can be re-enabled later.

## Secrets (repo Settings -> Secrets and variables -> Actions)

| Secret | Needed for | Where |
|---|---|---|
| `ANTHROPIC_API_KEY` | scoring (required) | console.anthropic.com |
| `ADZUNA_APP_ID` / `ADZUNA_APP_KEY` | Adzuna | developer.adzuna.com |
| `REED_API_KEY` | Reed | reed.co.uk/developers |
| `JOOBLE_API_KEY` | Jooble | jooble.org/api/about |
| `MUSE_API_KEY` | optional, higher Muse rate limit | themuse.com/developers |

Any missing optional key just makes that one source sit out; the rest still run.

## The dashboard

- **New openings** tab - sorted by time found. **Best fits** tab - sorted by score.
  **Hidden** tab - things you've hidden.
- Each card shows a fit score, verified/lead badge, up to 3 pros and 3 cons, the
  time found, any deadline, and a link to a full per-job analysis page.
- **NEW** badge appears for 24 hours after a posting is found, then disappears.
- **Hide** checkbox removes a card to the Hidden tab. Hiding is per-device (stored
  in your browser), so it persists on that device across rebuilds but doesn't sync
  across devices.

## Setup recap

1. Add the secrets above.
2. Fill in `profile.txt` (replace every `[bracketed]` part; be precise about
   right-to-work and graduation year - the scorer weighs these heavily).
3. Actions tab -> Run workflow for a first run (creates `docs/`).
4. Settings -> Pages -> Deploy from branch -> `main` / `/docs`. Bookmark the URL.
5. It then runs itself hourly.

## Tuning

- Noisy dashboard -> raise `min_score` in `config.yaml` (try 65-70).
- Add firms -> drop their Greenhouse/Lever token into `config.yaml`. Find the token
  in a firm's careers URL: `greenhouse.io/TOKEN` or `lever.co/TOKEN`.
- Sharper judgement -> set `model: claude-sonnet-4-6`.
- Change frequency -> edit the cron line in `daily.yml` (UTC).
- `profile.txt` is the highest-leverage file - update it as your CV grows.

## Notes

- The repo is public (required for free GitHub Pages). Secrets stay private; keep
  real personal details out of `profile.txt` to be safe.
- Costs: GitHub Actions + Pages free; job APIs free; Claude scoring is cents/month
  on the default Haiku model.
