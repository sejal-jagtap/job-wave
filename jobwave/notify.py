"""Alert channels. Each is enabled only when its env vars / secrets are set:

  NTFY_TOPIC (+ optional NTFY_SERVER)            phone/desktop push via the free ntfy app
  SLACK_WEBHOOK_URL / DISCORD_WEBHOOK_URL         chat
  SMTP_HOST, SMTP_PORT, SMTP_USER, SMTP_PASS, ALERT_EMAIL_TO   email
"""
from __future__ import annotations

import html
import logging
import os
import smtplib
import time
from email.mime.text import MIMEText

import requests

log = logging.getLogger("jobwave")


# ---------- formatting ----------
def _k(v):
    return f"${round(v / 1000)}K"


def pay(j):
    lo, hi = j.get("salary_min"), j.get("salary_max")
    if lo and hi and lo != hi:
        return f"{_k(lo)}–{_k(hi)}"
    return _k(lo or hi) if (lo or hi) else ""


def posted(j):
    if not j.get("posted_at"):
        return ""
    h = (time.time() - j["posted_at"]) / 3600
    return "posted just now" if h < 1 else f"posted {round(h)}h ago" if h < 48 else f"posted {round(h / 24)}d ago"


def facts(j) -> str:
    """One line: where, pay, when."""
    locs = j.get("locations") or []
    loc = " / ".join(locs[:2]) + (f" +{len(locs) - 2}" if len(locs) > 2 else "")
    return " · ".join(x for x in (j["company"], loc, pay(j), posted(j)) if x)


def tags(j) -> str:
    parts = list(j.get("families") or [])
    if j.get("class_match"):
        parts.append("mentions 2027")
    if j.get("sponsorship"):
        parts.append(j["sponsorship"])
    if j.get("skills"):
        parts.append("skills: " + ", ".join(j["skills"][:5]))
    return " · ".join(parts)


# ---------- channels ----------
def _ntfy(title, body, click="", actions="", markdown=False):
    topic = os.getenv("NTFY_TOPIC")
    if not topic:
        return False
    hdr = {"Title": title.encode("ascii", "ignore").decode(), "Tags": "briefcase", "Priority": "high"}
    if click:
        hdr["Click"] = click
    if actions:
        hdr["Actions"] = actions
    if markdown:
        hdr["Markdown"] = "yes"
    try:
        r = requests.post(f"{os.getenv('NTFY_SERVER', 'https://ntfy.sh')}/{topic}", data=body.encode(),
                          headers=hdr, timeout=15)
        r.raise_for_status()
    except Exception as e:
        log.warning("ntfy failed: %s", e)
    return True


def _chat(text_slack, text_discord):
    used = []
    for env, key, text in (("SLACK_WEBHOOK_URL", "text", text_slack), ("DISCORD_WEBHOOK_URL", "content", text_discord)):
        if hook := os.getenv(env):
            try:
                requests.post(hook, json={key: text[:1900]}, timeout=15).raise_for_status()
            except Exception as e:
                log.warning("%s failed: %s", env, e)
            used.append(env.split("_")[0].lower())
    return used


def _email(subject, html_body):
    if not (os.getenv("SMTP_HOST") and os.getenv("ALERT_EMAIL_TO")):
        return False
    m = MIMEText(html_body, "html")
    m["Subject"], m["From"], m["To"] = subject, os.getenv("SMTP_USER", ""), os.getenv("ALERT_EMAIL_TO")
    try:
        with smtplib.SMTP(os.getenv("SMTP_HOST"), int(os.getenv("SMTP_PORT", "587")), timeout=30) as s:
            s.starttls()
            s.login(os.getenv("SMTP_USER", ""), os.getenv("SMTP_PASS", ""))
            s.send_message(m)
    except Exception as e:
        log.warning("email failed: %s", e)
    return True


# ---------- public ----------
def send(new: list[dict], cfg: dict) -> list[str]:
    """Alert on new matches: one push per role, or one digest if there are many."""
    if not new:
        return []
    used = set()
    digest = len(new) > cfg["alerts"].get("digest_threshold", 5)

    # push
    if digest:
        body = "\n".join(f"**[{j['title']}]({j['url']})** — {facts(j)}" for j in new[:20])
        if len(new) > 20:
            body += f"\n…and {len(new) - 20} more (see email/Slack)"
        if _ntfy(f"{len(new)} new Data & AI roles", body, markdown=True):
            used.add("ntfy")
    else:
        for j in new:
            if _ntfy(f"{j['company']}: {j['title']}", f"{facts(j)}\n{tags(j)}", click=j["url"],
                     actions=f"view, Apply now, {j['url']}, clear=true"):
                used.add("ntfy")

    # chat
    head = f"*{len(new)} new Data & AI role{'s' if len(new) > 1 else ''}*\n"
    used.update(_chat(head + "\n".join(f"• <{j['url']}|{j['title']}> — {facts(j)}" for j in new[:30]),
                      head.replace("*", "**") + "\n".join(f"• [{j['title']}](<{j['url']}>) — {facts(j)}" for j in new[:30])))

    # email
    rows = "".join(
        f"<tr><td style='padding:12px 0;border-bottom:1px solid #e1e8ed'>"
        f"<div style='font-weight:600;font-size:15px'>{html.escape(j['title'])}</div>"
        f"<div style='color:#5d6f7c;font-size:13px;margin:2px 0 6px'>{html.escape(facts(j))}</div>"
        f"<div style='color:#0b7466;font-size:12px;margin-bottom:8px'>{html.escape(tags(j))}</div>"
        f"<a href='{html.escape(j['url'])}' style='background:#1f6feb;color:#fff;padding:6px 12px;border-radius:6px;"
        f"text-decoration:none;font-size:13px'>Apply now →</a></td></tr>" for j in new[:50])
    subject = (f"New: {new[0]['title']} at {new[0]['company']}" if len(new) == 1
               else f"JobWave: {len(new)} new Data & AI roles")
    if _email(subject, f"<div style='font-family:system-ui,sans-serif;max-width:600px'><h2 style='margin:0 0 4px'>"
                       f"{html.escape(subject)}</h2><p style='color:#5d6f7c;margin:0 0 8px'>Best matches first. "
                       f"New-grad roles fill fast — apply today.</p><table width='100%'>{rows}</table></div>"):
        used.add("email")

    for j in new:
        log.info("ALERT %s | %s | %s", j["title"], facts(j), j["url"])
    return sorted(used)


def send_text(title: str, body: str) -> list[str]:
    """Plain system message (test alert, broken source)."""
    used = []
    if _ntfy(title, body):
        used.append("ntfy")
    used += _chat(f"*{title}*\n{body}", f"**{title}**\n{body}")
    if _email(title, f"<div style='font-family:system-ui,sans-serif'><h3>{html.escape(title)}</h3>"
                     f"<pre style='font-family:inherit'>{html.escape(body)}</pre></div>"):
        used.append("email")
    log.info("NOTICE %s | %s", title, body.replace("\n", " / "))
    return used
