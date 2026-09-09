#!/usr/bin/env python3
from __future__ import annotations

import hashlib
import html
import json
import os
import re
import requests
import feedparser
from bs4 import BeautifulSoup
from datetime import datetime, timedelta, timezone
from dateutil import parser as dtparse
from email.message import EmailMessage
from email.utils import format_datetime
from urllib.parse import quote_plus, urlparse
import ssl
import smtplib
from zoneinfo import ZoneInfo

ROOT = os.path.dirname(os.path.abspath(__file__))
TZ = ZoneInfo("Asia/Kolkata")
UTC = timezone.utc
CONFIG = json.load(open(os.path.join(ROOT, "config.json"), encoding="utf-8"))
STATE_PATH = os.path.join(ROOT, "state", "job_state.json")
HEADERS = {"User-Agent": "KSRCT-Placement-JobAlert/1.0"}


def now_ist():
    return datetime.now(TZ)


def clean(value):
    return re.sub(r"\s+", " ", html.unescape(str(value or ""))).strip()


def parse_date(value):
    if not value:
        return None
    try:
        d = dtparse.parse(str(value))
        if d.tzinfo is None:
            d = d.replace(tzinfo=UTC)
        return d.astimezone(TZ)
    except Exception:
        return None


def norm_url(url):
    try:
        p = urlparse(url)
        return p._replace(fragment="").geturl() if p.scheme in ("http", "https") else ""
    except Exception:
        return ""


def key(title, company, url):
    raw = f"{clean(title).lower()}|{clean(company).lower()}|{norm_url(url).lower()}"
    return hashlib.sha1(raw.encode()).hexdigest()


def load_state():
    try:
        with open(STATE_PATH, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"last_run_at": None, "seen": {}}


def save_state(state):
    cutoff = now_ist() - timedelta(days=14)
    state["seen"] = {k: v for k, v in state.get("seen", {}).items() if (parse_date(v) or cutoff) >= cutoff}
    os.makedirs(os.path.dirname(STATE_PATH), exist_ok=True)
    with open(STATE_PATH, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, ensure_ascii=False)


def news_entries(query, limit=12):
    url = "https://news.google.com/rss/search?q=" + quote_plus(query) + "&hl=en-IN&gl=IN&ceid=IN:en"
    r = requests.get(url, headers=HEADERS, timeout=20)
    r.raise_for_status()
    return feedparser.parse(r.content).entries[:limit]


def page_info(url):
    try:
        r = requests.get(url, headers=HEADERS, timeout=20, allow_redirects=True)
        final = norm_url(r.url) or norm_url(url)
        text = ""
        pub = mod = None
        if "text/html" in (r.headers.get("content-type") or "").lower():
            soup = BeautifulSoup(r.text[:180000], "html.parser")
            text = clean(soup.get_text(" ", strip=True))
            def meta(names):
                for n in names:
                    tag = soup.find("meta", attrs={"property": n}) or soup.find("meta", attrs={"name": n})
                    if tag and tag.get("content"):
                        return tag.get("content")
                return ""
            pub = parse_date(meta(["article:published_time", "datePublished", "publish_date"]))
            mod = parse_date(meta(["article:modified_time", "dateModified", "last-modified"]))
        return final, text, pub, mod
    except Exception:
        return norm_url(url), "", None, None


def closed(text):
    t = text.lower()
    return any(x in t for x in [
        "job is no longer available", "position has been filled", "job has expired",
        "applications are closed", "application closed", "no longer accepting applications",
        "job not found", "404 not found"
    ])


def too_experienced(text):
    t = text.lower()
    if re.search(r"\b(?:3|4|5|6|7|8|9|10|11|12|13|14|15)\s*\+\s*years?\b", t):
        return True
    if re.search(r"\b[3-9]\s*(?:-|to)\s*[5-9]\s*years?\b", t):
        return True
    return False


def is_fresher_engineering(title, text):
    t = f"{title} {text}".lower()
    engineering = any(x in t for x in ["engineer", "engineering", "b.e", "btech", "b.tech", "graduate engineer", "trainee engineer"])
    fresher = any(x in t for x in CONFIG["positive_terms"])
    negative = any(x in t for x in CONFIG["negative_terms"])
    return engineering and fresher and not negative and not too_experienced(t)


def infer_company(title, source):
    parts = re.split(r"\s+[|–—:-]\s+", title, maxsplit=1)
    if len(parts) == 2 and 1 <= len(parts[0].split()) <= 10:
        return clean(parts[0])
    m = re.search(r"\bat\s+([A-Z][A-Za-z0-9&. -]{2,60})", title)
    if m:
        return clean(m.group(1))
    return clean(source) or "Not disclosed"


def infer_branches(text):
    t = text.lower()
    out = []
    for name, terms in CONFIG["branch_keywords"].items():
        if any(x in t for x in terms):
            out.append(name)
    if not out:
        return "Not disclosed"
    if "Any B.E./B.Tech" in out and len(out) > 1:
        out.remove("Any B.E./B.Tech")
    return ", ".join(dict.fromkeys(out))


def infer_salary(text):
    for pattern in [
        r"₹\s?[0-9][0-9,]*(?:\s*-\s*₹?\s*[0-9][0-9,]*)?\s*(?:LPA|lakhs?|/month|per month)?",
        r"[0-9]+(?:\.[0-9]+)?\s*LPA"
    ]:
        m = re.search(pattern, text, re.I)
        if m:
            return clean(m.group(0))
    return "Not disclosed"


def infer_deadline(text):
    patterns = [
        r"(?:last date|deadline)\s*[:\-]?\s*([A-Za-z]{3,9}\s+\d{1,2},?\s+\d{4})",
        r"(?:apply|application)\s+(?:by|before|until)\s+([A-Za-z]{3,9}\s+\d{1,2},?\s+\d{4})"
    ]
    for p in patterns:
        m = re.search(p, text, re.I)
        if m:
            return clean(m.group(1))
    return "Not disclosed"


def collect():
    state = load_state()
    last = parse_date(state.get("last_run_at")) or (now_ist() - timedelta(hours=48))
    seen = state.get("seen", {})
    items, errors = [], []
    locations = CONFIG["locations"]
    for loc in locations:
        queries = [
            f'"B.E" OR "B.Tech" fresher engineer "{loc}" when:2d',
            f'"graduate engineer trainee" "{loc}" when:2d',
            f'"engineering fresher" "{loc}" when:2d',
            f'"entry level engineer" "{loc}" when:2d'
        ]
        for q in queries:
            try:
                entries = news_entries(q, CONFIG["max_items_per_query"])
            except Exception as e:
                errors.append(f"{loc}: {e}")
                continue
            for e in entries:
                title = clean(e.get("title"))
                rss_date = parse_date(e.get("published") or e.get("updated"))
                source = clean((e.get("source") or {}).get("title", "")) or "Google News"
                raw = norm_url(e.get("link"))
                final, text, pub, mod = page_info(raw)
                blob = f"{title} {clean(e.get('summary'))} {text}"
                if not is_fresher_engineering(title, blob) or closed(blob):
                    continue
                job_date = mod or pub or rss_date
                company = infer_company(title, source)
                k = key(title, company, final or raw)
                # New/updated since the previous successful run; when a source exposes
                # no date, allow the item only if it has never been seen.
                if job_date:
                    if job_date < last:
                        continue
                elif k in seen:
                    continue
                if k in seen and job_date and job_date <= parse_date(seen[k]):
                    continue
                items.append({
                    "key": k,
                    "title": title,
                    "company": company,
                    "location": loc,
                    "branches": infer_branches(blob),
                    "experience": "Fresher / entry-level",
                    "salary": infer_salary(blob),
                    "deadline": infer_deadline(blob),
                    "source": source,
                    "url": final or raw,
                    "job_date": job_date.isoformat() if job_date else "Not disclosed",
                    "published": rss_date.isoformat() if rss_date else "Not disclosed",
                })

    dedup = {}
    for x in items:
        dedup[x["key"]] = x
    result = list(dedup.values())
    result.sort(key=lambda x: (x["job_date"] != "Not disclosed", x["job_date"]), reverse=True)
    return result[:CONFIG["max_total_items"]], errors, state


def build_report(items, errors, generated):
    lines = [
        "DAILY B.E./B.TECH FRESHER ENGINEERING JOB ALERT",
        f"Alert generated: {generated.strftime('%d %B %Y, %I:%M %p IST')}",
        "Coverage: Coimbatore, Bengaluru, Chennai, Hyderabad, Kerala (Kochi & Thiruvananthapuram)",
        f"New/recent verified openings: {len(items)}",
        ""
    ]
    if not items:
        lines += ["No high-confidence new/updated fresher engineering openings were identified since the previous successful alert.", ""]
    for i, x in enumerate(items, 1):
        lines += [
            f"{i}. {x['company']} — {x['title']}",
            f"Location: {x['location']}",
            f"Job posting/update date: {x['job_date']}",
            f"Eligible B.E./B.Tech branches: {x['branches']}",
            f"Batch/experience: {x['experience']}",
            f"Salary/CTC: {x['salary']}",
            "Job type: Full-time / entry-level (verify on employer page)",
            f"Important skills/requirements: {clean(x['title'])}; see source listing for full requirements",
            f"Application deadline: {x['deadline']}",
            f"Source: {x['source']}",
            f"Direct application link: {x['url']}",
            ""
        ]
    lines += [
        "VERIFICATION NOTE",
        "Listings are filtered for fresher/entry-level engineering signals and obvious closed/experienced roles. Employer pages can change quickly; candidates should confirm the live application status before applying."
    ]
    if errors:
        lines += ["", "SOURCE WARNINGS"] + [f"- {e}" for e in errors[:8]]
    return "\n".join(lines)


def send_email(subject, body):
    recipient = os.getenv("MAIL_TO", "placement@ksrct.ac.in")
    brevo_key = os.getenv("BREVO_API_KEY")
    sender = os.getenv("MAIL_FROM")
    if brevo_key:
        if not sender:
            raise RuntimeError("MAIL_FROM secret is required when BREVO_API_KEY is set")
        r = requests.post(
            "https://api.brevo.com/v3/smtp/email",
            headers={"api-key": brevo_key, "accept": "application/json", "content-type": "application/json"},
            json={"sender": {"email": sender}, "to": [{"email": recipient}], "subject": subject, "textContent": body},
            timeout=30,
        )
        if r.status_code >= 300:
            raise RuntimeError(f"Brevo returned HTTP {r.status_code}: {r.text[:800]}")
        return

    user = os.getenv("SMTP_USER")
    password = os.getenv("SMTP_PASSWORD")
    if not user or not password:
        raise RuntimeError("Set BREVO_API_KEY + MAIL_FROM (recommended) or SMTP_USER + SMTP_PASSWORD")
    host = os.getenv("SMTP_HOST", "smtp.gmail.com")
    port = int(os.getenv("SMTP_PORT", "465"))
    msg = EmailMessage()
    msg["From"], msg["To"], msg["Subject"] = user, recipient, subject
    msg["Date"] = format_datetime(datetime.now(UTC))
    msg.set_content(body)
    with smtplib.SMTP_SSL(host, port, context=ssl.create_default_context(), timeout=30) as smtp:
        smtp.login(user, password)
        smtp.send_message(msg)


def main():
    generated = now_ist()
    items, errors, state = collect()
    report = build_report(items, errors, generated)
    subject = f"Daily B.E./B.Tech Fresher Engineering Job Alert — {generated.strftime('%d %b %Y')}"
    send_email(subject, report)
    for x in items:
        state.setdefault("seen", {})[x["key"]] = generated.isoformat()
    state["last_run_at"] = generated.isoformat()
    save_state(state)
    print(report)


if __name__ == "__main__":
    main()
