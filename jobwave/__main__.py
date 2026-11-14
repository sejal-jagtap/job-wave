"""JobWave CLI.

  python -m jobwave run            fetch -> filter -> alert on new matches -> save state
  python -m jobwave run --no-alert  same, without sending alerts
  python -m jobwave explain TEXT    show why postings whose title contains TEXT were kept/dropped
  python -m jobwave test-alert      send one test alert to every configured channel
  python -m jobwave add URL         watch a company: paste its careers page URL (Greenhouse/Lever/Ashby/Workday)
  python -m jobwave check-sources [--prune]   test every career page; --prune drops the ones that fail
  python -m jobwave ui               open the local app in your browser (http://127.0.0.1:8787)
  python -m jobwave demo [--count N] send real alerts for the N newest current matches (for a screen recording)
"""
from __future__ import annotations

import argparse
import collections
import json
import logging
import re
import sys
import time
from pathlib import Path

import yaml

from . import notify
from .enrich import enrich
from .filters import evaluate
from .sources import _fetch_one, detect_board, fetch_all, source_tasks

ROOT = Path.cwd()
STATE = ROOT / "state"
log = logging.getLogger("jobwave")


def load_cfg(path="config.yaml") -> dict:
    return yaml.safe_load(Path(path).read_text())


def _read(name, default):
    p = STATE / name
    return json.loads(p.read_text()) if p.exists() else default


def _write(name, obj):
    STATE.mkdir(exist_ok=True)
    (STATE / name).write_text(json.dumps(obj, indent=1, default=str))


def _key(j):
    loc = (j["locations"] or [""])[0].split(",")[0].lower()
    return (re.sub(r"[^a-z0-9]", "", j["company"].lower()), re.sub(r"[^a-z0-9]", "", j["title"].lower()), loc)


def dedupe(jobs):
    """Same role from a company board and an aggregator -> keep the company board copy."""
    best = {}
    for j in jobs:
        k = _key(j)
        agg = ("simplify", "jobright")
        if k not in best or (best[k]["source"] in agg and j["source"] not in agg):
            best[k] = j
    return list(best.values())


def match(jobs: list[dict], cfg: dict, cache_path: Path):
    """Evaluate every posting. With profile.check_job_pages (or require_grad_year), new-grad candidates' job pages
    are read first, so roles stated for an earlier class (e.g. 'graduating 2026') are dropped and 2027 ones tagged."""
    if cfg["profile"].get("check_job_pages") or cfg["profile"].get("require_grad_year"):
        loose = {**cfg, "profile": {**cfg["profile"], "require_grad_year": False}}
        cands = []
        for j in jobs:
            ok, _, enr = evaluate(j, loose)
            if ok and enr.get("level") == "New grad":
                cands.append(j)
        enrich(cands, cache_path, cfg["profile"].get("max_pages_per_run", 200))
    out = []
    for j in jobs:
        ok, why, enr = evaluate(j, cfg)
        out.append((j, ok, why, enr))
    return out


def run(cfg, send_alerts=True, backlog=False):
    t0 = time.time()
    raw, health = fetch_all(cfg)
    jobs = dedupe(raw)
    log.info("fetched %d postings (%d after de-dup) from %d sources", len(raw), len(jobs), len(health))

    matched, reasons = [], collections.Counter()
    for j, ok, why, enr in match(jobs, cfg, STATE / "page_years.json"):
        if ok:
            j.update(enr)
            matched.append(j)
        else:
            reasons[why.split(" yrs")[0] if "yrs" in why else why] += 1

    seen: dict = _read("seen.json", {})
    first_run = not seen
    now = time.time()
    alert_window = cfg["alerts"].get("max_age_hours", 72) * 3600

    new = []
    for j in matched:
        first = seen.get(j["id"])
        if first is None:
            seen[j["id"]] = now
            fresh = (now - (j["posted_at"] or now)) <= alert_window
            if (not first_run or backlog) and fresh:
                new.append(j)
        j["first_seen"] = seen[j["id"]]
    seen = {k: v for k, v in seen.items() if now - v < 90 * 86400}

    new.sort(key=lambda j: -j.get("score", 0))
    channels = notify.send(new, cfg) if send_alerts else []

    # A board that keeps failing (renamed, moved ATS) gets one heads-up alert, since there's no dashboard to see it.
    prev = _read("meta.json", {})
    streak = {k: (prev.get("fail_streak", {}).get(k, 0) + 1 if v == "error" else 0) for k, v in health.items()}
    broken = [k for k, n in streak.items() if n == cfg["alerts"].get("broken_source_after_runs", 8)]
    if broken and send_alerts:
        notify.send_text("JobWave: a career page stopped responding",
                         "These sources have failed for ~2 hours. Check the board name in config.yaml:\n"
                         + "\n".join("• " + b for b in broken))
    meta = {"last_run": now, "duration_s": round(time.time() - t0, 1), "fetched": len(raw), "unique": len(jobs),
            "matched": len(matched), "new_this_run": len(new), "first_run": first_run,
            "alert_channels": channels, "rejections": reasons.most_common(),
            "sources_ok": sum(1 for v in health.values() if v != "error"),
            "sources_failed": [k for k, v in health.items() if v == "error"], "health": health, "fail_streak": streak}
    _write("seen.json", seen)
    _write("meta.json", meta)
    log.info("matched %d · new %d · alerts via %s · failed sources: %s",
             len(matched), len(new), channels or "none", meta["sources_failed"] or "none")
    return meta


def save_cfg(cfg, path="config.yaml"):
    p = Path(path)
    p.with_suffix(".yaml.bak").write_text(p.read_text())
    p.write_text("# JobWave config (previous version saved as config.yaml.bak)\n"
                 + yaml.safe_dump(cfg, sort_keys=False, allow_unicode=True, width=110))


def add_company(url: str, cfg: dict, name: str = "") -> tuple[bool, str]:
    """Detect the job system behind a careers URL, check it responds, and add it to the config."""
    found = detect_board(url.strip())
    if not found:
        return False, ("Couldn't find a Greenhouse, Lever, Ashby or Workday job board on that page. "
                       "Open one job posting on the site and paste that link instead.")
    kind, slug = found
    entry = f"{name.strip()} | {slug}" if kind == "workday" and name.strip() else slug
    current = cfg["sources"].setdefault(kind, []) or []
    if any(e == entry or e.rpartition("|")[2].strip() == slug for e in current):
        return True, f"Already watching {slug} ({kind})."
    res = _fetch_one(kind, entry, cfg)
    if res is None:
        return False, f"Found {kind} board '{slug}', but it didn't respond. Check the URL and try again."
    current.append(entry)
    cfg["sources"][kind] = current
    return True, f"Added {slug} ({kind}): {len(res)} open roles right now."


def check_sources(cfg, prune=False):
    tasks = source_tasks(cfg)
    bad = []
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=12) as ex:
        for (k, s), res in zip(tasks, ex.map(lambda t: _fetch_one(*t, cfg), tasks)):
            print(f"{'OK ' if res is not None else 'ERR'}  {k:10} {s[:60]:60} {'' if res is None else str(len(res)) + ' roles'}")
            if res is None:
                bad.append((k, s))
    print(f"\n{len(tasks) - len(bad)} of {len(tasks)} sources responding.")
    if bad and prune:
        for k, s in bad:
            if k in cfg["sources"] and isinstance(cfg["sources"][k], list):
                cfg["sources"][k] = [e for e in cfg["sources"][k] if e != s]
        save_cfg(cfg)
        print(f"Removed {len(bad)} failing sources from config.yaml (backup: config.yaml.bak).")
    elif bad:
        print("Run with --prune to remove the failing ones.")


def explain(cfg, text):
    raw, _ = fetch_all(cfg)
    for j, ok, why, enr in match(dedupe(raw), cfg, ROOT / ".jobwave-pages.json"):
        if text.lower() in j["title"].lower() or text.lower() in j["company"].lower():
            print(f"{'KEEP' if ok else 'DROP':4} {j['company'][:22]:22} {j['title'][:60]:60} "
                  f"{why or ('score ' + str(enr.get('score')))}")


def demo(cfg, count=3):
    """Alert on the newest real matches right now, without touching saved state."""
    raw, health = fetch_all(cfg)
    matched = []
    for j, ok, _, enr in match(dedupe(raw), cfg, ROOT / ".jobwave-pages.json"):
        if ok:
            j.update(enr)
            matched.append(j)
    matched.sort(key=lambda j: -(j["posted_at"] or 0))
    pick = matched[:count]
    print(f"{len(matched)} current matches from {sum(v != 'error' for v in health.values())} sources; "
          f"alerting on the {len(pick)} newest:")
    for j in pick:
        print("  •", j["company"], "—", j["title"], "|", notify.facts(j))
    print("sent via:", notify.send(pick, cfg) or "no channels configured")


def main(argv=None):
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    ap = argparse.ArgumentParser(prog="jobwave")
    ap.add_argument("cmd", choices=["run", "explain", "test-alert", "demo", "ui", "add", "check-sources"])
    ap.add_argument("text", nargs="?")
    ap.add_argument("--no-alert", action="store_true")
    ap.add_argument("--backlog", action="store_true", help="on first run, alert on recent matches too")
    ap.add_argument("--count", type=int, default=3, help="demo: how many roles to alert on")
    ap.add_argument("--prune", action="store_true", help="check-sources: remove failing sources")
    ap.add_argument("--name", default="", help="add: display name for a Workday company")
    ap.add_argument("--port", type=int, default=8787)
    ap.add_argument("--no-browser", action="store_true")
    ap.add_argument("--config", default="config.yaml")
    a = ap.parse_args(argv)
    cfg = load_cfg(a.config)
    if a.cmd == "run":
        run(cfg, send_alerts=not a.no_alert, backlog=a.backlog)
    elif a.cmd == "explain":
        explain(cfg, a.text or "")
    elif a.cmd == "test-alert":
        print("sent via:", notify.send_text("JobWave test alert", "Alerts are connected. You'll hear here when a new role goes live.")
              or "no channels configured — set NTFY_TOPIC / SMTP_* / SLACK_WEBHOOK_URL")
    elif a.cmd == "add":
        ok, msg = add_company(a.text or "", cfg, a.name)
        if ok and "Added" in msg:
            save_cfg(cfg, a.config)
        print(msg)
        return 0 if ok else 1
    elif a.cmd == "check-sources":
        check_sources(cfg, a.prune)
    elif a.cmd == "ui":
        from .ui import serve
        logging.getLogger("jobwave").setLevel(logging.ERROR)
        serve(a.port, open_browser=not a.no_browser)
    elif a.cmd == "demo":
        demo(cfg, a.count)
    return 0


if __name__ == "__main__":
    sys.exit(main())