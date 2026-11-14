"""Parser + filter tests. Fixtures follow the documented public API shapes."""
import time
from pathlib import Path

import yaml

from jobwave.filters import evaluate, is_us, min_yoe
from jobwave.sources import parse_ashby, parse_greenhouse, parse_lever, parse_simplify

CFG = yaml.safe_load((Path(__file__).parent.parent / "config.yaml").read_text())
STRICT = {**CFG, "profile": {**CFG["profile"], "require_grad_year": True}}
CFG = {**CFG, "profile": {**CFG["profile"], "require_grad_year": False}}   # most tests check the other rules
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
       "categories": {"location": "San Francisco, CA", "team": "ML", "commitment": "Full-time"},
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
    assert j["remote"] and j["locations"] == ["San Francisco, CA"]
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


# ---------- Jobright, Workday, URL detection ----------
from jobwave.sources import detect_board, parse_jobright, parse_workday, workday_parts

JR = """| Company | Job Title | Location | Work Model | Date Posted |
| ----- | --------- |  --------- | ---- | ------- |
| **[Acme AI](https://acme.ai)** | **[Machine Learning Engineer, New Grad](https://jobright.ai/jobs/info/6af7aa?utm_source=1103)** | Austin, TX, United States | Remote | Nov 13 |
| ↳ | **[Data Scientist I](https://jobright.ai/jobs/info/6af7bb?utm_source=1103)** | Dallas, TX, United States | On Site | Nov 12 |
"""
WD = [{"total": 2, "jobPostings": [
    {"title": "Associate Data Scientist", "externalPath": "/job/Plano-TX/Associate-Data-Scientist_R123",
     "locationsText": "Plano, TX", "postedOn": "Posted Today", "bulletFields": ["R123"]},
    {"title": "Senior Data Engineer", "externalPath": "/job/Remote/Senior-Data-Engineer_R456",
     "locationsText": "Remote - US", "postedOn": "Posted 3 Days Ago", "bulletFields": ["R456"]}]}]


def test_jobright_parse():
    a, b = parse_jobright("2026-AI-New-Grad", JR)
    assert a["company"] == b["company"] == "Acme AI"          # ↳ row inherits company
    assert a["remote"] and b["locations"] == ["Dallas, TX"] and a["id"] == "jr:6af7aa"
    assert evaluate(a, CFG)[0] and evaluate(b, CFG)[0]


def test_workday_parse():
    assert workday_parts("https://nvidia.wd5.myworkdayjobs.com/en-US/NVIDIAExternalCareerSite") == \
        ("nvidia.wd5.myworkdayjobs.com", "nvidia", "NVIDIAExternalCareerSite")
    a, b = parse_workday("Capital One | https://capitalone.wd12.myworkdayjobs.com/Capital_One", WD)
    assert a["company"] == "Capital One" and a["url"].endswith("/Capital_One/job/Plano-TX/Associate-Data-Scientist_R123")
    assert evaluate(a, CFG)[0] and not evaluate(b, CFG)[0]


def test_detect_board():
    assert detect_board("https://boards.greenhouse.io/databricks/jobs/123", fetch=False) == ("greenhouse", "databricks")
    assert detect_board("https://job-boards.greenhouse.io/anthropic", fetch=False) == ("greenhouse", "anthropic")
    assert detect_board("https://jobs.lever.co/palantir/abc-123", fetch=False) == ("lever", "palantir")
    assert detect_board("https://jobs.ashbyhq.com/openai/xyz", fetch=False) == ("ashby", "openai")
    assert detect_board("https://capitalone.wd12.myworkdayjobs.com/en-US/Capital_One/job/x", fetch=False) == \
        ("workday", "https://capitalone.wd12.myworkdayjobs.com/en-US/Capital_One")
    assert detect_board("https://example.com/careers", fetch=False) is None


def test_experienced_track():
    base = dict(source="greenhouse", company="A", url="", locations=["Dallas, TX"], remote=False,
                posted_at=NOW, team="", employment_type="", degrees=[], sponsorship="", category="")
    ev = lambda title, desc: evaluate({**base, "title": title, "description": desc}, CFG)
    ok, _, enr = ev("Senior Data Engineer", "3+ years of experience building pipelines in Spark")
    assert ok and enr["level"] == "Experienced · 3+ yrs"
    assert ev("Data Scientist II", "Bachelor's with 4 years of experience or Master's with 2 years of experience")[0]
    assert not ev("Senior Data Engineer", "6+ years of experience")[0]          # asks too much
    assert not ev("Senior Data Scientist", "")[0]                               # experience not stated
    assert not ev("Staff Machine Learning Engineer", "3+ years of experience")[0]
    assert not ev("Data Engineering Manager", "2+ years of experience")[0]
    ok, _, enr = ev("Data Engineer", "3-5 years of experience with Airflow")    # unlabeled mid-level
    assert ok and enr["level"].startswith("Experienced")
    ok, _, enr = ev("Machine Learning Engineer", "1+ years of experience")
    assert ok and enr["level"] == "New grad"


def test_off_target_titles():
    base = dict(source="jobright", company="A", url="", locations=["Dallas, TX"], remote=False, posted_at=NOW,
                team="", employment_type="new-grad", degrees=[], sponsorship="", category="", description="")
    ok = lambda t: evaluate({**base, "title": t}, CFG)[0]
    assert not ok("Business Analyst - AI & Automation")
    assert not ok("Associate Consultant - Generative AI")
    assert not ok("Marketing Analyst")
    assert ok("Media and Marketing Data Scientist")      # core role present -> kept
    assert ok("Machine Learning Engineer New Grad - E-Commerce User Growth")


def test_strict_2027():
    from jobwave.enrich import years_in
    assert years_in("Candidates graduating between December 2026 and June 2027") == [2026, 2027]
    assert years_in("This role is for the Class of 2027.") == [2027]
    assert years_in("Summer 2027 start date") == [2027]
    assert years_in("© 2026 Acme Inc. All rights reserved") == []
    base = dict(source="jobright", company="A", url="", locations=["Dallas, TX"], remote=False, posted_at=NOW,
                team="", employment_type="new-grad", degrees=[], sponsorship="", category="", description="",
                title="Data Scientist, New Grad")
    ev = lambda **kw: evaluate({**base, **kw}, STRICT)
    assert not ev()[0]                                   # page not read -> not confirmed
    assert not ev(page_years=[])[0]                      # read, no year stated
    assert not ev(page_years=[2025, 2026])[0]            # earlier classes only
    ok, _, enr = ev(page_years=[2026, 2027])
    assert ok and enr["class_match"]
    exp = dict(source="greenhouse", employment_type="", title="Senior Data Engineer", description="3+ years of experience")
    assert ev(**exp)[0]                                  # experienced roles don't need a class year


def test_target_companies():
    from jobwave.filters import target_company
    T = STRICT["profile"]["target_companies"]
    assert target_company("Amazon Web Services, Inc.", T) == "Amazon"
    assert target_company("JPMorgan Chase & Co.", T) == "JPMorgan"
    assert target_company("Sony Interactive Entertainment", T) == "Sony Interactive Entertainment"
    assert target_company("American Express", T) == "American Express"
    assert target_company("Metabase", T) is None and target_company("Acme AI", T) is None
    base = dict(source="jobright", url="", locations=["Seattle, WA"], remote=False, posted_at=NOW, team="",
                employment_type="new-grad", degrees=[], sponsorship="", category="", description="",
                title="Data Scientist, New Grad")
    a = evaluate({**base, "company": "Meta"}, CFG)[2]
    b = evaluate({**base, "company": "Acme AI"}, CFG)[2]
    assert a["target"] == "Meta" and a["score"] > b["score"]
    only = {**CFG, "profile": {**CFG["profile"], "only_target_companies": True}}
    assert not evaluate({**base, "company": "Acme AI"}, only)[0]


def test_amazon_and_smartrecruiters_parse():
    from jobwave.sources import parse_amazon, parse_smartrecruiters
    [a] = parse_amazon({"jobs": [{"id_icims": "3001", "title": "Data Engineer I", "posted_date": "November 12, 2026",
                                  "normalized_location": "Seattle, Washington, USA", "job_path": "/en/jobs/3001/data-engineer-i",
                                  "company_name": "Amazon Web Services, Inc.", "basic_qualifications": "1+ years of experience"}]})
    assert a["url"] == "https://www.amazon.jobs/en/jobs/3001/data-engineer-i" and a["company"] == "Amazon"
    assert evaluate(a, CFG)[0]
    [v] = parse_smartrecruiters("Visa", {"content": [{"id": "744", "name": "Data Scientist", "releasedDate": "2026-11-12T10:00:00.000Z",
                                                       "location": {"city": "Foster City", "region": "ca", "country": "us"},
                                                       "company": {"name": "Visa"}, "experienceLevel": {"label": "Entry Level"}}]})
    assert v["url"] == "https://jobs.smartrecruiters.com/Visa/744" and v["locations"] == ["Foster City, CA"]