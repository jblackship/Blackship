# Internship scout

A small agent that wakes up every morning, pulls fresh business/finance internship
postings for London and a few EU finance hubs, asks Claude to score each one against
your profile (0-100, with a one-line reason and any visible deadline), and emails you
a ranked digest of everything that clears your bar. It remembers what it has already
sent you in `seen.json`, so you're never pinged twice about the same role.

What's inside:

| File | What it does |
|---|---|
| `agent.py` | The whole pipeline: fetch → dedupe → score → email |
| `profile.txt` | Who you are. **The matching quality lives in this file.** |
| `config.yaml` | Cities, search queries, score threshold, email settings |
| `.github/workflows/daily.yml` | The morning schedule (GitHub Actions) |
| `seen.json` | The agent's memory |

## Setup (~15 minutes)

**1. Put it on GitHub.** Create a new **private** repository and upload everything in
this folder — keep the hidden `.github` folder, it contains the schedule. (Private
repos get plenty of free Actions minutes for a two-minute daily job.)

**2. Collect three credentials.**
- **Anthropic API key** — console.anthropic.com → API keys. Add a few euros of credit;
  scoring runs on a small model and costs cents per month.
- **Adzuna app ID + key** — developer.adzuna.com → free signup, instant keys.
- **Gmail app password** — turn on 2-step verification, then create one at
  myaccount.google.com/apppasswords. This is *not* your normal password. (Any other
  SMTP provider works too — change `smtp_host`/`smtp_port` in `config.yaml`.)

**3. Add the secrets.** In the repo: Settings → Secrets and variables → Actions →
New repository secret. Create these six:

| Secret name | Value |
|---|---|
| `ANTHROPIC_API_KEY` | from console.anthropic.com |
| `ADZUNA_APP_ID` | from developer.adzuna.com |
| `ADZUNA_APP_KEY` | from developer.adzuna.com |
| `SMTP_USER` | your Gmail address |
| `SMTP_PASS` | the app password |
| `EMAIL_TO` | where the digest should land (can equal SMTP_USER) |

**4. Make it yours.** Edit `profile.txt` and replace every `[bracketed]` placeholder.
Be specific about right-to-work status, graduation year, and exclusions — the scorer
weighs these heavily, and for London roles visa sponsorship is often the deciding
factor. Adjust cities and queries in `config.yaml` if you want.

**5. Test it.** Repo → Actions tab → "Daily internship scout" → Run workflow.
Watch the log, then check your inbox (and spam, the first time — mark it as
not-spam and you're set).

That's it. It now runs by itself at 07:00 London time every morning.

## Tuning

- **Too much noise** → raise `min_score` (try 65–70). **Too quiet** → lower it,
  add queries, or add cities.
- **`claude_discovery`** is the extra pass where Claude searches the web itself.
  It's what catches spring weeks and insight programmes that only appear on firm
  career pages. Costs a few extra cents per run; set to `false` to disable.
- **Sharper judgement** → set `model: claude-sonnet-4-6` in `config.yaml`.
- **`profile.txt` is the highest-leverage file.** Every improvement there improves
  every score. Update it as your CV grows.
- **Different time** → edit the cron line in `daily.yml` (note: it's in UTC).

## Costs

GitHub Actions: covered by the free tier. Adzuna and Gmail: free. Claude: cents per
month on the default model; the web-search discovery pass adds a small per-search fee
(current pricing: docs.claude.com). Realistically the whole thing runs for pocket change.

## Troubleshooting

- **Workflow green but no email** → that day's log will say either "Nothing new today"
  or "none cleared min_score" — both are normal. Otherwise check spam.
- **SMTP authentication error** → you used your normal Gmail password. It must be an
  app password, which requires 2-step verification to be on.
- **Adzuna errors in the log** → check both Adzuna secrets are set; a single failing
  query is logged and skipped, the run continues.
- **Want to start over** → reset `seen.json` to `[]` and the agent forgets everything.
