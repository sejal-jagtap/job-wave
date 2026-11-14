"""Classify postings (role family, level, class year...) and apply the user's filters."""
from __future__ import annotations

import re
import time

FAMILIES = {
    "data_science": r"data scien|decision scien|applied scien|research scien|statistician|quantitative research",
    "ml_ai": r"\b(machine learning|ml|ai|a\.i\.|artificial intelligence|deep learning|llms?|genai|gen ai|generative|"
             r"nlp|natural language|computer vision|mlops|ai/ml|ml/ai|conversational ai|large language|"
             r"foundation models?|reinforcement learning|speech recognition)\b|research engineer",
    "data_engineering": r"data engineer|analytics engineer|data platform|\betl\b|\belt\b|data infrastructure|"
                        r"data warehouse|big data|data pipeline|data integration engineer|dataops|database engineer",
    # Not in your default role_families; add them back in config.yaml or the app if you want them.
    "analytics": r"data analy|business intelligence|\bbi\b|\banalytics\b|insights analyst|product analyst|"
                 r"marketing science|quantitative analyst|business analyst",
    "research": r"research (engineer|scientist)|applied scien",
}
FAMILY_LABEL = {"data_science": "Data Science", "ml_ai": "ML / AI", "data_engineering": "Data Engineering",
                "analytics": "Analytics / BI", "research": "Research"}

ENTRY = re.compile(r"new grad|new graduate|graduate|entry[- ]level|early[- ]career|university|campus|junior|"
                   r"\bjr\b|associate|\b(i|1)\b|level 1|20(26|27|28)|rotational|recent grad|residency|fellow",
                   re.I)
# Titles that signal an experienced (non-entry) role. Allowed only when the posting asks <= experienced.max_years.
EXPERIENCED_TITLE = re.compile(r"\b(senior|sr\.?|ii|iii|2|3|mid[- ]level|experienced)\b", re.I)
# A title must name a core DS / DE / ML role; otherwise "AI Analyst", "Consultant - GenAI" etc. are dropped.
CORE_ROLE = re.compile(r"scien|engineer|machine learning|\bml\b|developer|researcher", re.I)
INTERN = re.compile(r"\bintern(ship)?\b|co-?op\b", re.I)
PHD_ONLY = re.compile(r"\bph\.?d\b", re.I)
YOE = re.compile(r"(?:minimum of |at least )?(\d{1,2})\s*(?:\+|plus)?\s*(?:(?:-|–|to)\s*\d{1,2}\s*)?\+?\s*"
                 r"years?(?:\s+of)?[^.\n]{0,60}?experience", re.I)
GRAD_YEAR = re.compile(r"(?:class of|graduat\w*|grads?\b|degree|start(?:ing)?(?: date)?(?: in)?)[^.\n]{0,40}?"
                       r"\b(20[2-3]\d)\b|\b(20[2-3]\d)\s*(?:new )?(?:grad|graduate|start)", re.I)
TITLE_YEAR = re.compile(r"\b(20[2-3]\d)\b")
CITIZEN = re.compile(r"u\.?s\.? citizen(ship)?\s+(is\s+)?required|must be a u\.?s\.? citizen|"
                     r"requires? u\.?s\.? citizenship|citizenship is required", re.I)
CLEARANCE = re.compile(r"(security|secret|ts/sci|top secret)\s+clearance|clearance (is )?required|"
                       r"active clearance|obtain (and maintain )?(a |an )?(\w+ )?clearance", re.I)
NO_SPONSOR = re.compile(r"(not|unable to|cannot|can't|won't|will not)\s+(be able to\s+)?(provide |offer )?"
                        r"(visa |immigration )?sponsor|sponsorship (is )?not available|"
                        r"without (the )?(need for )?(current or future )?(visa )?sponsorship", re.I)
SALARY = re.compile(r"\$\s?(\d{2,3})(?:,(\d{3})|\s?[kK])")

US_STATES = set("AL AK AZ AR CA CO CT DE FL GA HI ID IL IN IA KS KY LA ME MD MA MI MN MS MO MT NE NV NH NJ NM "
                "NY NC ND OH OK OR PA RI SC SD TN TX UT VT VA WA WV WI WY DC".split())
US_HINT = re.compile(r"united states|\busa?\b|u\.s\.|remote in usa|san francisco|\bsf\b|new york|\bnyc\b|"
                     r"seattle|austin|boston|chicago|los angeles|dallas|plano|denver|atlanta|washington|"
                     r"mountain view|palo alto|menlo park|sunnyvale|san jose|bay area|pittsburgh|miami", re.I)
NON_US = re.compile(r"\b(uk|united kingdom|london|canada|toronto|vancouver|india|bengaluru|bangalore|"
                    r"hyderabad|germany|berlin|france|paris|ireland|dublin|singapore|japan|tokyo|australia|"
                    r"sydney|netherlands|amsterdam|poland|spain|mexico|brazil|israel|china|korea|emea|apac)\b", re.I)


# Other names your target companies post under (company field, case-insensitive).
COMPANY_ALIASES = {
    "google": ["google", "alphabet", "deepmind", "youtube"], "microsoft": ["microsoft"],
    "amazon": ["amazon", "aws", "amazon web services", "prime video", "a2z"], "aws": ["aws", "amazon web services"],
    "prime video": ["prime video"], "meta": ["meta", "facebook", "instagram", "whatsapp"], "apple": ["apple"],
    "jpmorgan": ["jpmorgan", "jp morgan", "j.p. morgan", "jpmorganchase", "jpmorgan chase", "chase"],
    "jp morgan & chase": ["jpmorgan", "jp morgan", "j.p. morgan", "chase"],
    "goldman sachs": ["goldman sachs", "goldman"], "gs": ["goldman sachs", "goldman"],
    "bcg": ["bcg", "boston consulting group"], "mckinsey & company": ["mckinsey", "quantumblack"],
    "bank of america": ["bank of america", "bofa", "merrill"], "bofa": ["bank of america", "bofa", "merrill"],
    "amex": ["american express", "amex"], "american express": ["american express", "amex"],
    "bny": ["bny", "bny mellon", "bank of new york"], "td bank": ["td bank", "td securities", "toronto-dominion"],
    "bank ozk": ["bank ozk", "ozk"], "amd": ["amd", "advanced micro devices"],
    "sony interactive entertainment": ["sony interactive", "playstation"], "sony": ["sony"],
    "spacex": ["spacex", "space exploration technologies", "starlink"], "salesforce": ["salesforce", "slack", "tableau"],
    "walmart": ["walmart", "sam's club"], "fidelity": ["fidelity", "fmr"], "capital one": ["capital one"],
}


def target_company(company: str, targets: list[str]) -> str | None:
    """Return the target-list name this company matches, or None."""
    name = (company or "").lower()
    for t in targets or []:
        for alias in COMPANY_ALIASES.get(t.lower().strip(), [t.lower().strip()]):
            if re.search(r"(?<![a-z0-9])" + re.escape(alias) + r"(?![a-z0-9])", name):
                return t
    return None


def _kw_regex(words):
    words = [w.strip() for w in words or [] if w.strip()]
    if not words:
        return None
    return re.compile("|".join(r"(?<!\w)" + re.escape(w) + r"(?!\w)" for w in words), re.I)


def is_us(loc: str) -> bool:
    if NON_US.search(loc):
        return bool(US_HINT.search(loc)) and "remote in canada" not in loc.lower()
    if re.search(r",\s*([A-Z]{2})\b", loc) and re.search(r",\s*([A-Z]{2})\b", loc).group(1) in US_STATES:
        return True
    return bool(US_HINT.search(loc)) or "remote" in loc.lower() or loc.strip() == ""


def families(j: dict) -> list[str]:
    text = f"{j['title']} {j.get('team', '')}"
    fams = [f for f, rx in FAMILIES.items() if re.search(rx, text, re.I)]
    if re.search(r"\b(gis|cad|clinical|structural|mechanical|electrical|hardware|soc|asic|rtl|fpga|silicon|"
                 r"microarchitecture|tapeout|firmware|trader)\b", j["title"], re.I) \
            and not re.search(r"data scien|machine learning|\bml\b", j["title"], re.I):
        return []  # adjacent fields that keyword-match but aren't Data/AI roles
    return fams


def min_yoe(desc: str) -> int | None:
    vals = [int(m.group(1)) for m in YOE.finditer(desc or "") if int(m.group(1)) <= 20]
    return min(vals) if vals else None


def grad_years(j: dict) -> set[int]:
    ys = {int(y) for y in TITLE_YEAR.findall(j["title"])}
    for m in GRAD_YEAR.finditer(j.get("description") or ""):
        ys.add(int(m.group(1) or m.group(2)))
    ys.update(j.get("page_years") or [])      # found by reading the job page (enrich.py)
    return ys


def salary(j: dict):
    if j.get("salary_max") or j.get("salary_min"):
        return j.get("salary_min"), j.get("salary_max")
    vals = []
    for a, b in SALARY.findall(j.get("description") or ""):
        v = int(a) * 1000 if not b else int(a + b)
        if 30_000 <= v <= 1_000_000:
            vals.append(v)
    return (min(vals), max(vals)) if vals else (None, None)


def evaluate(j: dict, cfg: dict) -> tuple[bool, str, dict]:
    """Return (match, reason_if_rejected, enrichment)."""
    f, p = cfg["filters"], cfg["profile"]
    title, desc = j["title"], j.get("description") or ""
    enr: dict = {}

    fams = families(j)
    fams = [x for x in fams if x in f["role_families"]]
    if not fams:
        return False, "role family", enr
    enr["families"] = [FAMILY_LABEL[x] for x in fams]

    off = _kw_regex(f.get("off_target_words"))
    if off and off.search(title) and not CORE_ROLE.search(title):
        return False, "off-target role (analyst / consulting / business)", enr

    bad = _kw_regex(f.get("exclude_keywords"))
    if bad and bad.search(title):
        return False, "excluded keyword / seniority", enr
    good = _kw_regex(f.get("include_keywords"))
    if good and not good.search(title):
        return False, "include keyword", enr
    if j["company"].lower() in {c.lower() for c in f.get("company_blocklist") or []}:
        return False, "company blocklist", enr
    enr["target"] = target_company(j["company"], p.get("target_companies"))
    if p.get("only_target_companies") and not enr["target"]:
        return False, "not a target company", enr

    if INTERN.search(title) or "intern" in (j.get("employment_type") or "").lower():
        if not f.get("include_internships"):
            return False, "internship", enr

    labeled_entry = j["source"] in ("simplify", "jobright") or bool(ENTRY.search(title))
    yoe = min_yoe(desc)
    enr["yoe"] = yoe
    exp = f.get("experienced") or {}
    exp_on, exp_max = bool(exp.get("enabled")), exp.get("max_years", 4)
    new_max = f["max_years_experience"]

    if EXPERIENCED_TITLE.search(title) and not re.search(r"new grad|graduate|university|early career", title, re.I):
        # Senior / Level II-III roles: only if the posting itself asks for few enough years.
        if not exp_on:
            return False, "senior / level II+", enr
        if yoe is None:
            return False, "senior, experience not stated", enr
        if yoe > exp_max:
            return False, f"{yoe}+ yrs experience", enr
        if exp.get("role_families") and not set(exp["role_families"]) & {k for k, v in FAMILY_LABEL.items() if v in enr["families"]}:
            return False, "role family (experienced)", enr
        enr["level"] = f"Experienced · {yoe}+ yrs"
        labeled_entry = False
    else:
        cap = exp_max if exp_on else new_max
        if yoe is not None and yoe > cap:
            return False, f"{yoe}+ yrs experience", enr
        if not labeled_entry and not (f.get("allow_unlabeled_level") and yoe is not None):
            return False, "not entry level", enr
        enr["level"] = ("New grad" if labeled_entry or yoe <= new_max else f"Experienced · {yoe}+ yrs")

    years = grad_years(j)
    gy = p["grad_year"]
    if years and gy not in years and max(years) < gy:
        return False, f"class of {max(years)}", enr
    enr["class_match"] = gy in years
    if p.get("require_grad_year") and enr["level"] == "New grad" and not enr["class_match"]:
        # Strict mode: new-grad roles must say they're for this class. Experienced roles don't need to.
        return False, ("no " + str(gy) + " mention" if "page_years" in j else "job page not read yet / unreadable"), enr

    degs = j.get("degrees") or []
    if degs and not set(degs) & set(p["degrees"]):
        return False, "degree", enr
    if "PhD" not in p["degrees"] and PHD_ONLY.search(title):
        return False, "PhD role", enr

    locs = j.get("locations") or [""]
    if f.get("us_only") and not any(is_us(l) for l in locs):
        return False, "non-US", enr
    if f.get("remote_only") and not j.get("remote"):
        return False, "not remote", enr
    if f.get("locations"):
        if not any(w.lower() in l.lower() for w in f["locations"] for l in locs):
            return False, "location", enr

    ex = [x.lower() for x in f.get("exclude_if") or []]
    spons = (j.get("sponsorship") or "").lower()
    full = title + "\n" + desc
    if "citizenship" in ex and (CITIZEN.search(full) or "citizenship" in spons):
        return False, "US citizenship required", enr
    if "clearance" in ex and CLEARANCE.search(full):
        return False, "clearance required", enr
    if f.get("needs_sponsorship") and (NO_SPONSOR.search(desc) or "does not offer" in spons or "citizenship" in spons):
        return False, "no sponsorship", enr
    enr["sponsorship"] = ("Offers sponsorship" if "offers" in spons else
                          "No sponsorship" if (NO_SPONSOR.search(desc) or "does not offer" in spons) else "")

    smin, smax = salary(j)
    enr["salary_min"], enr["salary_max"] = smin, smax
    if f.get("min_salary") and smax and smax < f["min_salary"]:
        return False, "salary", enr

    # ---- ranking ----
    low = (title + " " + desc).lower()
    hits = [s for s in p.get("skills", []) if re.search(r"(?<!\w)" + re.escape(s.lower()) + r"(?!\w)", low)]
    enr["skills"] = hits[:8]
    pref = any(w.lower() in l.lower() for w in f.get("preferred_locations") or [] for l in locs) or (
        "remote" in [w.lower() for w in f.get("preferred_locations") or []] and j.get("remote"))
    enr["preferred_location"] = bool(pref)
    age_h = (time.time() - (j.get("posted_at") or 0)) / 3600
    enr["score"] = round(6 * bool(enr.get("target")) + min(len(hits), 6) + 3 * pref + 3 * enr["class_match"] + 2 * labeled_entry
                         + max(0, 3 - age_h / 24), 1)
    return True, "", enr