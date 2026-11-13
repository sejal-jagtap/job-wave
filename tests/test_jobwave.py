"""Parser + filter tests. Fixtures follow the documented public API shapes."""
import time
from pathlib import Path

import yaml

from jobwave.filters import evaluate, is_us, min_yoe
from jobwave.sources import parse_ashby, parse_greenhouse, parse_lever, parse_simplify

CFG = yaml.safe_load((Path(__file__).parent.parent / "config.yaml").read_text())
NOW = time.time()

GH = {"jobs": [{
    "id": 4012345, "title": "Data Scientist, New Grad (2027)", "updated_at": "2026-11-10T10:00:00-05:00",
    "first_published": "2026-11-10T09:00:00-05:00", "location": {"name": "New York, NY; Remote - US"},
    "absolute_url": "https://boards.greenhouse.io/acme/jobs/4012345",
    "content": "&lt;p&gt;Graduating in 2027 with a BS/MS. 0-2 years of experience with Python and SQL. "
               "Salary $120,000 - $140,000.&lt;/p&gt;",
    "departments": [{"name": "Data"}]}]}
LV = [{"id": "abc", "text": "Machine Learning Engineer I", "createdAt": int(NOW * 1000) - 3600_000,
       "hostedUrl": "https://jobs.lever.co/acme/abc", "workplaceType": "remote",
       "categories": {"location": "Austin, TX", "team": "ML", "commitment": "Full-time"},
       "descriptionPlain": "You have 1+ years of experience in PyTorch.", "additionalPlain": ""}]
AB = {"apiVersion": "1", "jobs": [{
    "id": "u-1", "title": "Senior ML Engineer", "location": "San Francisco", "isRemote": False,
    "employmentType": "FullTime", "publishedAt": "2026-11-09T14:02:11.000Z", "jobUrl": "https://jobs.ashbyhq.com/acme/u-1",
    "descriptionPlain": "6+ years of experience", "isListed": True,
    "compensation": {"summaryComponents": [{"compensationType": "Salary", "minValue": 200000, "maxValue": 250000}]}}]}
SP = [{"source": "Simplify", "category": "AI/ML/Data", "company_name": "Wise", "id": "x1",
       "title": "Graduate Data Scientist", "active": True, "date_updated": NOW, "date_posted": NOW - 7200,
       "url": "https://example.com/apply", "locations": ["Austin, TX"], "company_url": "", "is_visible": True,
       "sponsorship": "Other", "degrees": ["Bachelor's", "Master's"]},
      {"source": "Simplify", "category": "AI/ML/Data", "company_name": "X", "id": "x2", "title": "Data Analyst",
       "active": False, "date_updated": 0, "date_posted": 0, "url": "", "locations": [], "company_url": "",
       "is_visible": True, "sponsorship": "Other", "degrees": []}]


def test_greenhouse_parse_and_match():
    [j] = parse_greenhouse("acme", GH)
    assert j["id"] == "gh:acme:4012345" and j["remote"] and "Graduating in 2027" in j["description"]
    ok, why, enr = evaluate(j, CFG)
    assert ok, why
    assert enr["class_match"] and enr["salary_min"] == 120000 and "python" in enr["skills"]


def test_lever_parse_and_match():
    [j] = parse_lever("acme", LV)
    assert j["remote"] and j["locations"] == ["Austin, TX"]
    ok, why, enr = evaluate(j, CFG)
    assert ok, why
    assert enr["preferred_location"]


def test_ashby_senior_dropped():
    [j] = parse_ashby("acme", AB)
    assert j["salary_max"] == 250000
    assert evaluate(j, CFG)[0] is False


def test_simplify_skips_inactive():
    js = parse_simplify(SP)
    assert len(js) == 1 and evaluate(js[0], CFG)[0]


def test_rules():
    assert min_yoe("Requires 3+ years of professional experience") == 3
    assert min_yoe("0-2 years of industry experience") == 0
    assert is_us("Remote in USA") and is_us("Dallas, TX") and not is_us("London, UK")
    base = dict(source="greenhouse", company="A", url="", locations=["Seattle, WA"], remote=False,
                posted_at=NOW, team="", employment_type="", degrees=[], sponsorship="", category="")
    drop = lambda **kw: not evaluate({**base, **kw}, CFG)[0]
    assert drop(title="Data Scientist, New Grad 2026", description="")           # wrong class year
    assert drop(title="Data Analyst, Entry Level", description="Must be a U.S. citizen. US citizenship is required")
    assert drop(title="Data Scientist Intern", description="")
    assert drop(title="Software Engineer, New Grad", description="")             # not Data/AI
    assert drop(title="Data Scientist", description="")                           # no level signal
    assert not drop(title="Data Scientist", description="1+ years of experience")  # unlabeled but YOE fits
