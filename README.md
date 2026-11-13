# JobWave — job alert agent for new-grad Data & AI roles

Every 30 minutes JobWave checks company career pages, keeps only entry-level **Data Science, ML/AI, Data
Engineering, Analytics and Research** roles open to the **class of 2027**, and alerts you the moment a new one
appears: a push to your phone with an **Apply now** button, plus email / Slack / Discord if you want them.

**What it watches (public data, no keys needed):**
- Company career pages via their job-board APIs: Greenhouse, Lever, Ashby (companies listed in `config.yaml`)
- [SimplifyJobs New-Grad list](https://github.com/SimplifyJobs/New-Grad-Positions): covers companies on Workday/iCIMS
  (banks, big enterprises) whose career sites have no public API. Turn off with `simplify_newgrad: false`.

## Setup (≈10 min, free)

1. **Push this folder to a private GitHub repo.** The default schedule (every 30 min ≈ 1,440 Actions minutes/month)
   stays inside GitHub Free's 2,000 private-repo minutes, so it costs $0. With GitHub Pro (3,000 min) you can use `*/20`.
2. **Phone alerts:** install the **ntfy** app (iOS / Android), subscribe to a long random topic name
   (e.g. 20+ random characters; treat it like a password), then in GitHub:
   Settings → Secrets and variables → Actions → *New repository secret* → `NTFY_TOPIC` = that name.
3. **Optional channels** (same place):
   | Channel | Secrets |
   |---|---|
   | Email | `SMTP_HOST`=`smtp.gmail.com`, `SMTP_PORT`=`587`, `SMTP_USER`=your Gmail, `SMTP_PASS`=a Gmail [app password](https://myaccount.google.com/apppasswords), `ALERT_EMAIL_TO` |
   | Slack / Discord | `SLACK_WEBHOOK_URL` / `DISCORD_WEBHOOK_URL` |
4. **Start:** Actions tab → *JobWave watch* → *Run workflow*. The first run silently records every role already
   posted; from then on you only hear about roles that are new.

## Privacy: who gets alerts
- Alerts go only to the channels in this repo's **Actions secrets**. Secrets are never shown in code, logs, or to
  collaborators, and forks/copies don't receive them.
- To share for grading: Settings → Collaborators → add reviewers with the **Read** role. They can read the code and
  run history but can't see secrets, change settings, or trigger runs.
- Keep the ntfy topic name out of the repo, screenshots and write-ups: anyone who knows it can subscribe.
  Email alerts go only to `ALERT_EMAIL_TO`.

## What an alert looks like
- **1–5 new roles:** one push each — `Databricks: Data Scientist, New Grad` · `Databricks · San Francisco, CA · $120K–$140K · posted 12m ago` · `Data Science · mentions 2027 · skills: python, sql, spark` · **[Apply now]**
- **More than 5 at once:** one digest, best matches first.
- **A career page breaks** (company renamed its board / switched systems): one heads-up after ~2 hours of failures.

## Tuning
Edit `config.yaml` and push; the next run uses it.

```bash
pip install -r requirements.txt
python -m jobwave run --no-alert      # dry run
python -m jobwave explain databricks  # why each Databricks role was kept / dropped
NTFY_TOPIC=... python -m jobwave test-alert
python -m pytest -q tests
```

**Add a company:** open its careers page. If the URL or page source contains `boards.greenhouse.io/<x>`,
`jobs.lever.co/<x>` or `jobs.ashbyhq.com/<x>`, add `<x>` under that source.

## How filtering works
- **Role family**: title keywords per family (`jobwave/filters.py → FAMILIES`).
- **Level**: kept if labeled entry-level (new grad, early career, associate, I, 2027…), or unlabeled but asking ≤ `max_years_experience`.
- **Class year**: dropped if the posting names only earlier grad years (e.g. "New Grad 2026").
- **US only, degree, internships, citizenship/clearance, sponsorship, min pay, keywords, blocklist**: see `config.yaml`.
- **Order inside a digest**: skill matches, preferred locations, class-of-2027 mention, freshness.

## Limits
- GitHub can start scheduled runs a few minutes late at busy times. For tighter timing, run
  `python -m jobwave run` from cron on any always-on machine (same `state/` folder).
- Alerts fire only for roles posted in the last `alerts.max_age_hours` (default 72), so re-listed old roles stay quiet.
