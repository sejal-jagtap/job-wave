"""JobWave CLI.

  python -m jobwave run            fetch -> filter -> alert on new matches -> save state
  python -m jobwave run --no-alert  same, without sending alerts
  python -m jobwave explain TEXT    show why postings whose title contains TEXT were kept/dropped
  python -m jobwave test-alert      send one test alert to every configured channel
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
from .filters import evaluate
from .sources import fetch_all

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
        if k not in best or (best[k]["source"] == "simplify" and j["source"] != "simplify"):
            best[k] = j
    return list(best.values())


def run(cfg, send_alerts=True, backlog=False):
    t0 = time.time()
    raw, health = fetch_all(cfg)
    jobs = dedupe(raw)
    log.info("fetched %d postings (%d after de-dup) from %d sources", len(raw), len(jobs), len(health))

    matched, reasons = [], collections.Counter()
    for j in jobs:
        ok, why, enr = evaluate(j, cfg)
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


def explain(cfg, text):
    raw, _ = fetch_all(cfg)
    for j in dedupe(raw):
        if text.lower() in j["title"].lower() or text.lower() in j["company"].lower():
            ok, why, enr = evaluate(j, cfg)
            print(f"{'KEEP' if ok else 'DROP':4} {j['company'][:22]:22} {j['title'][:60]:60} "
                  f"{why or ('score ' + str(enr.get('score')))}")


def main(argv=None):
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    ap = argparse.ArgumentParser(prog="jobwave")
    ap.add_argument("cmd", choices=["run", "explain", "test-alert"])
    ap.add_argument("text", nargs="?")
    ap.add_argument("--no-alert", action="store_true")
    ap.add_argument("--backlog", action="store_true", help="on first run, alert on recent matches too")
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
    return 0


if __name__ == "__main__":
    sys.exit(main())
