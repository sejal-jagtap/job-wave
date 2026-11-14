"""Read job pages to find the graduating class (e.g. 'Class of 2027', 'graduating May 2027', '2027 start').

The new-grad lists (Jobright, SimplifyJobs) only give titles, so for the strict 2027 filter JobWave opens each
candidate's job page once. Results are cached by job id, so each page is read only one time.
"""
from __future__ import annotations

import html
import json
import logging
import re
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import requests

from .sources import UA, html_to_text

log = logging.getLogger("jobwave")

CONTEXT = re.compile(r"class of|graduat\w*|\bgrads?\b|new grad|degree|start(?:ing)? date|start(?:ing)? in|"
                     r"commenc\w*|anticipated|expected", re.I)
YEAR_FIRST = re.compile(r"\b(20[2-3]\d)\s*(?:new |university |college )?(?:grads?|graduates?|start|class|cohort)|"
                        r"\b(?:summer|fall|autumn|winter|spring)\s+(20[2-3]\d)\s+(?:start|graduat|grad|cohort|class)",
                        re.I)
YEAR = re.compile(r"\b(20[2-3]\d)\b")


def years_in(text: str) -> list[int]:
    """Years stated near graduation / start-date wording, e.g. 'graduating Dec 2026 – June 2027' -> [2026, 2027]."""
    text = text or ""
    ys = set()
    for m in CONTEXT.finditer(text):
        window = re.split(r"[.\n<>]", text[m.start(): m.start() + 140], maxsplit=1)[0]
        ys.update(int(y) for y in YEAR.findall(window))
    for m in YEAR_FIRST.finditer(text):
        ys.add(int(m.group(1) or m.group(2)))
    return sorted(ys)


def page_text(url: str) -> str | None:
    """Description text for a job URL: official APIs for Greenhouse / Lever / Workday, raw page otherwise."""
    try:
        if m := re.search(r"greenhouse\.io/([^/?#]+)/jobs/(\d+)", url):
            r = requests.get(f"https://boards-api.greenhouse.io/v1/boards/{m.group(1)}/jobs/{m.group(2)}",
                             headers=UA, timeout=15)
            if r.ok:
                return m.group(0) + "\n" + html_to_text(r.json().get("content"))
        if m := re.search(r"jobs\.lever\.co/([^/?#]+)/([0-9a-f-]{36})", url):
            r = requests.get(f"https://api.lever.co/v0/postings/{m.group(1)}/{m.group(2)}", headers=UA, timeout=15)
            if r.ok:
                d = r.json()
                lists = " ".join(f"{x.get('text', '')} {html_to_text(x.get('content'))}" for x in d.get("lists") or [])
                return f"{d.get('text', '')}\n{d.get('descriptionPlain', '')}\n{lists}\n{d.get('additionalPlain', '')}"
        if m := re.match(r"(https://([a-z0-9-]+)\.wd\d+\.myworkday(?:jobs|site)\.com)/(?:[a-z]{2}-[A-Z]{2}/)?([^/]+)(/job/.+)",
                         url):
            r = requests.get(f"{m.group(1)}/wday/cxs/{m.group(2)}/{m.group(3)}{m.group(4).split('?')[0]}",
                             headers={**UA, "Accept": "application/json"}, timeout=15)
            if r.ok:
                info = r.json().get("jobPostingInfo") or {}
                return f"{info.get('title', '')}\n{html_to_text(info.get('jobDescription'))}"
        r = requests.get(url, headers={**UA, "Accept": "text/html"}, timeout=15)   # Jobright, Ashby, others
        if r.ok and r.text:
            raw = html.unescape(r.text)
            raw = raw.replace("\\u003c", "<").replace("\\n", "\n").replace('\\"', '"')
            return html_to_text(raw)
    except Exception as e:
        log.debug("page read failed %s: %s", url, e)
    return None


def enrich(jobs: list[dict], cache_path: Path, max_fetch: int = 200) -> dict:
    """Set j['page_years'] (list of years, [] = read but none stated) or leave it unset (page unreadable)."""
    try:
        cache = json.loads(cache_path.read_text())
    except Exception:
        cache = {}
    todo = [j for j in jobs if j["id"] not in cache][:max_fetch]

    def work(j):
        text = page_text(j["url"])
        return j["id"], (None if text is None or len(text) < 200 else years_in(text))

    with ThreadPoolExecutor(max_workers=16) as ex:
        for jid, ys in ex.map(work, todo):
            cache[jid] = ys
    for j in jobs:
        if cache.get(j["id"]) is not None:
            j["page_years"] = cache[j["id"]]
    live = {j["id"] for j in jobs}
    cache = {k: v for k, v in cache.items() if k in live}   # forget jobs that are gone
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_text(json.dumps(cache))
    stats = {"read": sum(1 for j in jobs if "page_years" in j),
             "mention_year": sum(1 for j in jobs if j.get("page_years")),
             "fetched_now": len(todo)}
    log.info("job pages: %d read (%d fetched this run), %d state a class year", stats["read"], len(todo),
             stats["mention_year"])
    return stats