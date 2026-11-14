"""Local JobWave app: `python -m jobwave ui` -> http://127.0.0.1:8787 (only reachable from this laptop).

Browse live matches, track applications, edit filters (saved to config.yaml, optionally pushed to GitHub
so the scheduled watcher uses them), and send test / demo alerts.
"""
from __future__ import annotations

import json
import os
import subprocess
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import yaml

from . import notify
from .filters import evaluate
from .sources import fetch_all

ROOT = Path.cwd()
CFG_PATH = ROOT / "config.yaml"
LOCAL = ROOT / ".jobwave-local.json"     # tracker + settings; gitignored, never leaves the laptop
PAGE = Path(__file__).with_name("ui.html")
KEEP = ("id", "source", "company", "title", "url", "locations", "remote", "posted_at", "families", "level",
        "class_match", "sponsorship", "salary_min", "salary_max", "skills", "preferred_location", "score", "yoe")


class App:
    def __init__(self):
        self.lock = threading.Lock()
        self.local = self._load_local()
        self.matches: list[dict] = self.local.get("cache", {}).get("matches", [])
        self.meta: dict = self.local.get("cache", {}).get("meta", {})
        self.checking = False
        self.error = ""

    # ---------- persistence ----------
    def _load_local(self):
        try:
            return json.loads(LOCAL.read_text())
        except Exception:
            return {"tracker": {}, "seen": [], "settings": {}, "cache": {}}

    def save_local(self):
        with self.lock:
            LOCAL.write_text(json.dumps(self.local, indent=1))

    def cfg(self):
        return yaml.safe_load(CFG_PATH.read_text())

    def topic(self):
        return os.getenv("NTFY_TOPIC") or self.local.get("settings", {}).get("ntfy_topic", "")

    # ---------- checking ----------
    def check(self):
        if self.checking:
            return
        self.checking, self.error = True, ""

        def work():
            try:
                cfg = self.cfg()
                from .__main__ import dedupe
                raw, health = fetch_all(cfg)
                seen = set(self.local.get("seen", []))
                first = not seen
                out = []
                for j in dedupe(raw):
                    ok, _, enr = evaluate(j, cfg)
                    if ok:
                        j.update(enr)
                        row = {k: j.get(k) for k in KEEP}
                        row["is_new"] = (not first) and j["id"] not in seen
                        out.append(row)
                out.sort(key=lambda r: -(r["posted_at"] or 0))
                meta = {"last_check": time.time(), "scanned": len(raw), "matched": len(out),
                        "new": sum(r["is_new"] for r in out),
                        "sources_ok": sum(v != "error" for v in health.values()),
                        "sources_failed": [k for k, v in health.items() if v == "error"]}
                with self.lock:
                    self.matches, self.meta = out, meta
                    self.local["seen"] = sorted(seen | {r["id"] for r in out})
                    self.local["cache"] = {"matches": out, "meta": meta}
                self.save_local()
            except Exception as e:
                self.error = str(e)[:300]
            finally:
                self.checking = False

        threading.Thread(target=work, daemon=True).start()

    # ---------- API ----------
    def state(self):
        cfg = self.cfg()
        return {"matches": self.matches, "meta": self.meta, "checking": self.checking, "error": self.error,
                "tracker": self.local.get("tracker", {}), "config": cfg,
                "topic_set": bool(self.topic()), "topic_from_env": bool(os.getenv("NTFY_TOPIC")),
                "git": (ROOT / ".git").exists()}

    def track(self, body):
        jid, status = body["id"], body.get("status")
        tr = self.local.setdefault("tracker", {})
        if not status:
            tr.pop(jid, None)
        else:
            job = next((m for m in self.matches if m["id"] == jid), None) or tr.get(jid, {}).get("job", {})
            prev = tr.get(jid, {})
            tr[jid] = {"status": status, "note": body.get("note", prev.get("note", "")),
                       "updated": time.time(), "job": {k: job.get(k) for k in ("title", "company", "url", "locations")}}
        self.save_local()
        return {"ok": True}

    def save_config(self, body):
        cfg = self.cfg()
        for section in ("profile", "filters", "alerts", "sources"):
            if section in body:
                cfg.setdefault(section, {}).update(body[section])
        CFG_PATH.with_suffix(".yaml.bak").write_text(CFG_PATH.read_text())
        CFG_PATH.write_text("# JobWave config — edited from the local app (previous version: config.yaml.bak)\n"
                            + yaml.safe_dump(cfg, sort_keys=False, allow_unicode=True, width=110))
        return {"ok": True}

    def push(self):
        def git(*a):
            return subprocess.run(["git", *a], cwd=ROOT, capture_output=True, text=True, timeout=60)
        if not (ROOT / ".git").exists():
            return {"ok": False, "msg": "This folder isn't a git repo yet."}
        git("add", "config.yaml")
        c = git("commit", "-m", "Update JobWave filters")
        if c.returncode and "nothing to commit" in (c.stdout + c.stderr):
            return {"ok": True, "msg": "Already up to date — nothing to push."}
        git("pull", "--rebase", "-q")
        p = git("push", "-q")
        if p.returncode:
            return {"ok": False, "msg": (p.stderr or p.stdout).strip()[:300]}
        return {"ok": True, "msg": "Pushed. The GitHub watcher uses these filters from its next run."}

    def add(self, body):
        from .__main__ import add_company, save_cfg
        cfg = self.cfg()
        ok, msg = add_company(body.get("url", ""), cfg, body.get("name", ""))
        if ok and msg.startswith("Added"):
            save_cfg(cfg, CFG_PATH)
        return {"ok": ok, "msg": msg}

    def alert(self, body):
        topic = self.topic()
        if not topic:
            return {"ok": False, "msg": "Add your ntfy topic in Alerts first."}
        os.environ["NTFY_TOPIC"] = topic
        if body.get("kind") == "test":
            used = notify.send_text("JobWave test alert", "Alerts are connected. You'll hear here when a new role goes live.")
        else:
            n = int(body.get("count", 3))
            pick = [m for m in self.matches if not body.get("id") or m["id"] == body["id"]][:n]
            used = notify.send(pick, self.cfg())
        return {"ok": bool(used), "msg": f"Sent via {', '.join(used)}." if used else "Nothing sent."}

    def settings(self, body):
        self.local.setdefault("settings", {})["ntfy_topic"] = body.get("ntfy_topic", "").strip()
        self.save_local()
        return {"ok": True}


def make_handler(app: App):
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _send(self, code, body, ctype="application/json"):
            data = body if isinstance(body, bytes) else json.dumps(body, default=str).encode()
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)

        def _local_origin(self):
            # Block other websites from calling this local API from your browser.
            o = self.headers.get("Origin") or self.headers.get("Referer") or ""
            host = self.headers.get("Host", "")
            return host.split(":")[0] in ("127.0.0.1", "localhost") and (
                not o or o.split("//")[-1].split("/")[0].split(":")[0] in ("127.0.0.1", "localhost"))

        def do_GET(self):
            if self.path in ("/", "/index.html"):
                return self._send(200, PAGE.read_bytes(), "text/html; charset=utf-8")
            if self.path == "/api/state" and self._local_origin():
                return self._send(200, app.state())
            self._send(404, {"error": "not found"})

        def do_POST(self):
            if not self._local_origin():
                return self._send(403, {"error": "forbidden"})
            n = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(n) or b"{}")
            routes = {"/api/check": lambda b: (app.check(), {"ok": True})[1], "/api/track": app.track,
                      "/api/config": app.save_config, "/api/push": lambda b: app.push(),
                      "/api/alert": app.alert, "/api/add": app.add, "/api/settings": app.settings}
            fn = routes.get(self.path)
            if not fn:
                return self._send(404, {"error": "not found"})
            try:
                self._send(200, fn(body))
            except Exception as e:
                self._send(500, {"ok": False, "msg": str(e)[:300]})
    return H


def serve(port=8787, open_browser=True):
    app = App()
    srv = ThreadingHTTPServer(("127.0.0.1", port), make_handler(app))
    url = f"http://127.0.0.1:{port}"
    print(f"JobWave is running at {url}  (Ctrl+C to stop)")
    if not app.matches or time.time() - app.meta.get("last_check", 0) > 1800:
        app.check()
    if open_browser:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")