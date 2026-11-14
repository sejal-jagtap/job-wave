"""Fetch postings from live sources and normalize them to one schema."""
from __future__ import annotations

import html
import logging
import re
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

import requests

log = logging.getLogger("jobwave")
UA = {"User-Agent": "JobWave/1.0 (personal job watcher)"}
BROWSER_UA = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) "
                            "Chrome/130.0 Safari/537.36", "Accept-Language": "en-US,en;q=0.9"}
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


JOBRIGHT_URL = "https://raw.githubusercontent.com/jobright-ai/{repo}/master/README.md"
MONTHS = {m: i for i, m in enumerate("Jan Feb Mar Apr May Jun Jul Aug Sep Oct Nov Dec".split(), 1)}
_MD_LINK = re.compile(r"\[([^\]]+)\]\(([^)]+)\)")


def _month_day(txt: str) -> float | None:
    m = re.match(r"\s*([A-Z][a-z]{2})\s+(\d{1,2})", txt or "")
    if not m or m.group(1) not in MONTHS:
        return None
    now = datetime.now()
    d = datetime(now.year, MONTHS[m.group(1)], int(m.group(2)), 12)
    if d.timestamp() > now.timestamp() + 2 * 86400:   # "Dec 30" seen in January -> last year
        d = d.replace(year=now.year - 1)
    return d.timestamp()


def parse_jobright(repo: str, md: str) -> list[dict]:
    """Jobright's daily new-grad lists on GitHub: a markdown table, '↳' rows repeat the company above."""
    out, company = [], ""
    for line in md.splitlines():
        if not line.startswith("| ") or line.startswith("| Company") or line.startswith("| ---"):
            continue
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) < 5:
            continue
        c_cell, t_cell, loc, model, date = cells[:5]
        if c_cell != "↳":
            m = _MD_LINK.search(c_cell)
            company = (m.group(1) if m else c_cell).strip("* ")
        m = _MD_LINK.search(t_cell)
        if not m:
            continue
        title, url = m.group(1).strip("* "), m.group(2)
        jid = re.search(r"/jobs/info/([0-9a-f]+)", url)
        locs = [l.strip() for l in re.split(r";|\s/\s", loc) if l.strip()]
        locs = [re.sub(r",\s*United States$", "", l) for l in locs]
        out.append(job(
            id=f"jr:{jid.group(1) if jid else url}", source="jobright", company=company, title=title, url=url,
            locations=locs, remote="remote" in model.lower(), posted_at=_month_day(date),
            employment_type="new-grad",
        ))
    return out


def workday_parts(site_url: str):
    """https://nvidia.wd5.myworkdayjobs.com/en-US/NVIDIAExternalCareerSite -> (host, tenant, site)"""
    m = re.match(r"https?://([a-z0-9-]+)\.(wd\d+)\.(myworkdayjobs|myworkdaysite)\.com/(?:[a-z]{2}-[A-Z]{2}/)?([^/?#]+)",
                 site_url.strip())
    if not m:
        raise ValueError(f"not a Workday careers URL: {site_url}")
    return f"{m.group(1)}.{m.group(2)}.{m.group(3)}.com", m.group(1), m.group(4)


def _workday_age(posted_on: str) -> float | None:
    t = (posted_on or "").lower()
    now = time.time()
    if "today" in t:
        return now - 3600
    if "yesterday" in t:
        return now - 86400
    m = re.search(r"(\d+)\+?\s*days?", t)
    return now - int(m.group(1)) * 86400 if m else None


def parse_workday(entry: str, pages: list[dict]) -> list[dict]:
    name, _, site_url = entry.rpartition("|")
    host, tenant, site = workday_parts(site_url)
    company = name.strip() or tenant.replace("-", " ").title()
    out = []
    for data in pages:
        for j in data.get("jobPostings") or []:
            path = j.get("externalPath", "")
            if not path:
                continue
            loc = j.get("locationsText", "")
            out.append(job(
                id=f"wd:{tenant}:{path.rsplit('_', 1)[-1] if '_' in path else path}", source="workday",
                company=company, title=j.get("title", "").strip(), url=f"https://{host}/{site}{path}",
                locations=[loc] if loc else [], remote="remote" in loc.lower(),
                posted_at=_workday_age(j.get("postedOn")),
            ))
    return out


def parse_amazon(data: dict) -> list[dict]:
    """amazon.jobs/en/search.json (also covers AWS, Prime Video, Alexa...)."""
    out = []
    for j in data.get("jobs") or []:
        jid = str(j.get("id_icims") or j.get("id") or "")
        if not jid:
            continue
        posted = None
        try:
            posted = datetime.strptime(j.get("posted_date", ""), "%B %d, %Y").replace(hour=12).timestamp()
        except ValueError:
            pass
        loc = j.get("normalized_location") or j.get("location") or ""
        desc = "\n".join(html_to_text(j.get(k)) for k in ("description", "basic_qualifications", "preferred_qualifications"))
        out.append(job(
            id=f"amz:{jid}", source="amazon", company=(j.get("company_name") or "Amazon").split(" ")[0].replace(".com", ""),
            title=j.get("title", "").strip(), url="https://www.amazon.jobs" + j.get("job_path", f"/en/jobs/{jid}"),
            locations=[loc] if loc else [], remote="virtual" in loc.lower() or "remote" in loc.lower(),
            posted_at=posted, team=j.get("business_category") or j.get("job_category", ""), description=desc,
        ))
    return out


def parse_smartrecruiters(company: str, data: dict) -> list[dict]:
    out = []
    for j in data.get("content") or []:
        loc = j.get("location") or {}
        place = ", ".join(x for x in (loc.get("city"), (loc.get("region") or "").upper() or None) if x)
        if (loc.get("country") or "").lower() not in ("us", "usa", ""):
            place = f"{place}, {loc.get('country', '').upper()}".strip(", ")
        out.append(job(
            id=f"sr:{company}:{j['id']}", source="smartrecruiters", company=(j.get("company") or {}).get("name") or company,
            title=j.get("name", "").strip(), url=f"https://jobs.smartrecruiters.com/{company}/{j['id']}",
            locations=[place] if place else [], remote=bool(loc.get("remote")), posted_at=_ts(j.get("releasedDate")),
            employment_type=(j.get("typeOfEmployment") or {}).get("label", ""),
            team=(j.get("experienceLevel") or {}).get("label", ""),
        ))
    return out


def _amazon_fetch(terms: list[str], pages=2):
    data = {"jobs": []}
    for term in terms or ["data"]:
        for page in range(pages):
            r = requests.get("https://www.amazon.jobs/en/search.json", headers={**BROWSER_UA, "Accept": "application/json"},
                             params={"base_query": term, "result_limit": 100, "offset": page * 100, "sort": "recent",
                                     "normalized_country_code[]": "USA"}, timeout=30)
            r.raise_for_status()
            batch = r.json().get("jobs") or []
            data["jobs"] += batch
            if len(batch) < 100:
                break
    return list({j["id"]: j for j in parse_amazon(data)}.values())


def _smartrecruiters_fetch(company: str, terms: list[str]):
    pages = []
    for term in terms or [""]:
        r = requests.get(f"https://api.smartrecruiters.com/v1/companies/{company}/postings",
                         params={"q": term, "limit": 100, "country": "us"}, headers=BROWSER_UA, timeout=30)
        r.raise_for_status()
        pages.append(r.json())
    jobs = {j["id"]: j for p in pages for j in parse_smartrecruiters(company, p)}.values()
    from .filters import FAMILIES
    fam = re.compile("|".join(FAMILIES.values()), re.I)
    out, n = [], 0
    for j in jobs:   # descriptions only for plausible, recent roles
        if fam.search(j["title"]) and j["posted_at"] and time.time() - j["posted_at"] < 4 * 86400 and n < 25:
            n += 1
            try:
                d = requests.get(f"https://api.smartrecruiters.com/v1/companies/{company}/postings/{j['id'].rsplit(':', 1)[1]}",
                                 headers=BROWSER_UA, timeout=20).json()
                secs = (d.get("jobAd") or {}).get("sections") or {}
                j["description"] = "\n".join(html_to_text((secs.get(k) or {}).get("text"))
                                             for k in ("jobDescription", "qualifications", "additionalInformation"))[:6000]
            except Exception:
                pass
        out.append(j)
    return out


BOARD_PATTERNS = [
    ("greenhouse", r"(?:boards|job-boards)(?:\.eu)?\.greenhouse\.io/(?:embed/job_board\?for=)?([A-Za-z0-9_-]+)"),
    ("greenhouse", r"boards-api\.greenhouse\.io/v1/boards/([A-Za-z0-9_-]+)"),
    ("lever", r"jobs\.lever\.co/([A-Za-z0-9_.-]+)"),
    ("ashby", r"jobs\.ashbyhq\.com/([A-Za-z0-9_.%-]+)"),
    ("workday", r"(https?://[a-z0-9-]+\.wd\d+\.myworkday(?:jobs|site)\.com/(?:[a-z]{2}-[A-Z]{2}/)?[A-Za-z0-9_-]+)"),
    ("smartrecruiters", r"(?:jobs|careers)\.smartrecruiters\.com/([A-Za-z0-9_-]+)"),
]
_NOT_SLUGS = {"embed", "v1", "jobs", "api", "job_board", "search", "en-US"}


def detect_board(url: str, fetch: bool = True) -> tuple[str, str] | None:
    """Work out which job system a careers page uses. Checks the URL, then (optionally) the page's HTML."""
    texts = [url]
    if fetch and not any(re.search(rx, url) for _, rx in BOARD_PATTERNS):
        try:
            texts.append(requests.get(url, headers=UA, timeout=20).text)
        except Exception as e:
            log.warning("could not open %s: %s", url, e)
    for text in texts:
        for kind, rx in BOARD_PATTERNS:
            for m in re.finditer(rx, text):
                slug = m.group(1)
                if slug not in _NOT_SLUGS:
                    return kind, slug
    return None


# ---------- fetchers ----------
def _workday_fetch(entry: str, terms: list[str], max_pages=2):
    _, _, site_url = entry.rpartition("|")
    host, tenant, site = workday_parts(site_url)
    api = f"https://{host}/wday/cxs/{tenant}/{site}/jobs"
    pages = []
    for term in terms or [""]:
        for page in range(max_pages):
            r = requests.post(api, json={"appliedFacets": {}, "limit": 20, "offset": page * 20, "searchText": term},
                              headers={**BROWSER_UA, "Accept": "application/json", "Content-Type": "application/json"}, timeout=30)
            r.raise_for_status()
            data = r.json()
            pages.append(data)
            if len(data.get("jobPostings") or []) < 20:
                break
    jobs = {j["id"]: j for j in parse_workday(entry, pages)}.values()   # same role found by several terms
    # The list API has no descriptions; fetch them for plausible Data/AI roles so the
    # experience / citizenship / class-year filters can work.
    from .filters import FAMILIES
    fam = re.compile("|".join(FAMILIES.values()), re.I)
    senior = re.compile(r"\b(staff|principal|lead|manager|director|head)\b", re.I)
    out, n_detail = [], 0
    for j in jobs:
        fresh = j["posted_at"] and time.time() - j["posted_at"] < 4 * 86400   # keeps each run under a minute
        if fresh and fam.search(j["title"]) and not senior.search(j["title"]) and n_detail < 25:
            n_detail += 1
            try:
                info = requests.get(f"https://{host}/wday/cxs/{tenant}/{site}{j['url'].split(site, 1)[1]}",
                                    headers={**BROWSER_UA, "Accept": "application/json"}, timeout=20).json()
                info = info.get("jobPostingInfo") or {}
                j["description"] = html_to_text(info.get("jobDescription"))[:6000]
                j["employment_type"] = info.get("timeType", "")
                j["posted_at"] = _ts(info.get("startDate")) or j["posted_at"]
                j["remote"] = j["remote"] or "remote" in (info.get("remoteType") or "").lower()
            except Exception:
                pass
        out.append(j)
    return out


def _fetch_one(kind: str, slug: str, cfg: dict | None = None):
    src = (cfg or {}).get("sources", {})
    try:
        if kind == "greenhouse":
            return parse_greenhouse(slug, _get(f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=true"))
        if kind == "lever":
            return parse_lever(slug, _get(f"https://api.lever.co/v0/postings/{slug}?mode=json"))
        if kind == "ashby":
            return parse_ashby(slug, _get(f"https://api.ashbyhq.com/posting-api/job-board/{slug}?includeCompensation=true"))
        if kind == "workday":
            return _workday_fetch(slug, src.get("search_terms") or src.get("workday_search")
                                  or ["data", "machine learning", "AI", "scientist"])
        if kind == "smartrecruiters":
            return _smartrecruiters_fetch(slug, src.get("search_terms") or src.get("workday_search"))
        if kind == "amazon":
            return _amazon_fetch(src.get("search_terms") or src.get("workday_search"))
        if kind == "jobright":
            r = requests.get(JOBRIGHT_URL.format(repo=slug), headers=UA, timeout=60)
            r.raise_for_status()
            return parse_jobright(slug, r.text)
        if kind == "simplify":
            return parse_simplify(_get(SIMPLIFY_URL, timeout=90))
    except Exception as e:  # one bad board must not kill the run
        log.warning("source %s:%s failed: %s", kind, slug, str(e)[:160])
        return None
    return []


def source_tasks(cfg: dict) -> list[tuple[str, str]]:
    src = cfg.get("sources", {})
    tasks = [(k, s) for k in ("greenhouse", "lever", "ashby", "workday", "smartrecruiters", "jobright")
             for s in src.get(k) or []]
    if src.get("amazon"):
        tasks.append(("amazon", "amazon.jobs"))
    if src.get("simplify_newgrad", True):
        tasks.append(("simplify", "newgrad"))
    return tasks


def fetch_all(cfg: dict) -> tuple[list[dict], dict]:
    tasks = source_tasks(cfg)
    jobs, health = [], {}
    with ThreadPoolExecutor(max_workers=12) as ex:
        for (k, s), res in zip(tasks, ex.map(lambda t: _fetch_one(*t, cfg), tasks)):
            label = s.rpartition("|")[0].strip() or s if k == "workday" else s
            health[f"{k}:{label}"] = "error" if res is None else len(res)
            jobs.extend(res or [])
    return jobs, health