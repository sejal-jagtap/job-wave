"""Fetch postings from live sources and normalize them to one schema."""
from __future__ import annotations

import html
import logging
import re
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

import requests

log = logging.getLogger("jobwave")
UA = {"User-Agent": "JobWave/1.0 (personal job watcher)"}
SIMPLIFY_URL = "https://raw.githubusercontent.com/SimplifyJobs/New-Grad-Positions/dev/.github/scripts/listings.json"


def _ts(s) -> float | None:
    if not s:
        return None
    if isinstance(s, (int, float)):
        return s / 1000 if s > 1e11 else float(s)
    try:
        return datetime.fromisoformat(str(s).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def html_to_text(s: str | None) -> str:
    if not s:
        return ""
    s = html.unescape(html.unescape(s))  # Greenhouse double-encodes
    s = re.sub(r"<(br|/p|/li|/h\d)[^>]*>", "\n", s, flags=re.I)
    s = re.sub(r"<[^>]+>", " ", s)
    return re.sub(r"[ \t\xa0]+", " ", s).strip()


def job(**kw) -> dict:
    base = dict(id="", source="", company="", title="", url="", locations=[], remote=False,
                posted_at=None, team="", employment_type="", description="", salary_min=None,
                salary_max=None, degrees=[], sponsorship="", category="")
    base.update(kw)
    base["description"] = (base["description"] or "")[:6000]
    return base


def _get(url: str, timeout=30):
    r = requests.get(url, headers=UA, timeout=timeout)
    r.raise_for_status()
    return r.json()


# ---------- parsers (pure; unit-tested) ----------
def parse_greenhouse(token: str, data: dict) -> list[dict]:
    out = []
    for j in data.get("jobs", []):
        loc = (j.get("location") or {}).get("name", "")
        desc = html_to_text(j.get("content"))
        out.append(job(
            id=f"gh:{token}:{j['id']}", source="greenhouse", company=token.title(),
            title=j.get("title", "").strip(), url=j.get("absolute_url", ""),
            locations=[l.strip() for l in re.split(r";|\||/", loc) if l.strip()] or [loc],
            remote="remote" in loc.lower(),
            posted_at=_ts(j.get("first_published")) or _ts(j.get("updated_at")),
            team=", ".join(d.get("name", "") for d in j.get("departments") or []),
            description=desc,
        ))
    return out


def parse_lever(company: str, data: list) -> list[dict]:
    out = []
    for j in data:
        cat = j.get("categories") or {}
        locs = cat.get("allLocations") or [cat.get("location", "")]
        out.append(job(
            id=f"lv:{company}:{j['id']}", source="lever", company=company.title(),
            title=j.get("text", "").strip(), url=j.get("hostedUrl", ""),
            locations=[l for l in locs if l], remote=(j.get("workplaceType") == "remote"),
            posted_at=_ts(j.get("createdAt")), team=cat.get("team", ""),
            employment_type=cat.get("commitment", ""),
            description=j.get("descriptionPlain", "") + "\n" + (j.get("additionalPlain") or ""),
            salary_min=(j.get("salaryRange") or {}).get("min"),
            salary_max=(j.get("salaryRange") or {}).get("max"),
        ))
    return out


def parse_ashby(org: str, data: dict) -> list[dict]:
    out = []
    for j in data.get("jobs", []):
        if j.get("isListed") is False:
            continue
        locs = [j.get("location", "")] + [s.get("location", "") for s in j.get("secondaryLocations") or []]
        smin = smax = None
        for c in ((j.get("compensation") or {}).get("summaryComponents") or []):
            if c.get("compensationType") == "Salary":
                smin, smax = c.get("minValue"), c.get("maxValue")
        out.append(job(
            id=f"ab:{org}:{j['id']}", source="ashby", company=org.title(),
            title=j.get("title", "").strip(), url=j.get("jobUrl", ""),
            locations=[l for l in locs if l], remote=bool(j.get("isRemote")) or j.get("workplaceType") == "Remote",
            posted_at=_ts(j.get("publishedAt")), team=j.get("team") or j.get("department", ""),
            employment_type=j.get("employmentType", ""),
            description=j.get("descriptionPlain") or html_to_text(j.get("descriptionHtml")),
            salary_min=smin, salary_max=smax,
        ))
    return out


def parse_simplify(data: list) -> list[dict]:
    out = []
    for j in data:
        if not j.get("active") or not j.get("is_visible", True):
            continue
        locs = j.get("locations") or []
        out.append(job(
            id=f"sp:{j['id']}", source="simplify", company=j.get("company_name", ""),
            title=j.get("title", "").strip(), url=j.get("url", ""), locations=locs,
            remote=any("remote" in l.lower() for l in locs), posted_at=_ts(j.get("date_posted")),
            degrees=j.get("degrees") or [], sponsorship=j.get("sponsorship", ""),
            category=j.get("category", ""), employment_type="new-grad",
        ))
    return out


# ---------- fetchers ----------
def _fetch_one(kind: str, slug: str):
    try:
        if kind == "greenhouse":
            return parse_greenhouse(slug, _get(f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=true"))
        if kind == "lever":
            return parse_lever(slug, _get(f"https://api.lever.co/v0/postings/{slug}?mode=json"))
        if kind == "ashby":
            return parse_ashby(slug, _get(f"https://api.ashbyhq.com/posting-api/job-board/{slug}?includeCompensation=true"))
        if kind == "simplify":
            return parse_simplify(_get(SIMPLIFY_URL, timeout=90))
    except Exception as e:  # one bad board must not kill the run
        log.warning("source %s:%s failed: %s", kind, slug, str(e)[:160])
        return None
    return []


def fetch_all(cfg: dict) -> tuple[list[dict], dict]:
    src = cfg.get("sources", {})
    tasks = [(k, s) for k in ("greenhouse", "lever", "ashby") for s in src.get(k) or []]
    if src.get("simplify_newgrad", True):
        tasks.append(("simplify", "newgrad"))
    jobs, health = [], {}
    with ThreadPoolExecutor(max_workers=12) as ex:
        for (k, s), res in zip(tasks, ex.map(lambda t: _fetch_one(*t), tasks)):
            health[f"{k}:{s}"] = "error" if res is None else len(res)
            jobs.extend(res or [])
    return jobs, health
