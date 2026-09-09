#!/usr/bin/env python3
from __future__ import annotations

import os
import re
import json
import ssl
import smtplib
import html
import hashlib
from pathlib import Path
from datetime import datetime, timezone, timedelta
from email.message import EmailMessage
from email.utils import format_datetime
from urllib.parse import quote_plus, urlparse
import requests
import feedparser
from bs4 import BeautifulSoup
from dateutil import parser as dateparser
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "config.json"
STATE_PATH = ROOT / "state" / "job_state.json"
TZ = ZoneInfo("Asia/Kolkata")
UTC = timezone.utc

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (compatible; KSRCT-Placement-JobAlert/1.0; "
        "+https://github.com/)"
    )
}

def now_ist() -> datetime:
    return datetime.now(TZ)

def iso(dt: datetime | None) -> str | None:
    return dt.isoformat() if dt else None

def parse_dt(value):
    if not value:
        return None
    try:
        dt = dateparser.parse(str(value))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        return dt.astimezone(TZ)
    except Exception:
        return None

def clean_text(text: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(text or "")).strip()

def normalize_url(url: str) -> str:
    if not url:
        return ""
    p = urlparse(url)
    if p.scheme not in ("http", "https"):
        return ""
    return p._replace(fragment="").geturl()

def normalize_key(title: str, company: str, url: str) -> str:
    base = f"{title}|{company}|{url}".lower()
    base = re.sub(r"[^a-z0-9]+", " ", base).strip()
    return hashlib.sha1(base.encode()).hexdigest()

def load_config():
    return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))

def load_state():
    if not STATE_PATH.exists():
        return {"last_run_at": None, "seen": {}}
    try:
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {"last_run_at": None, "seen": {}}

def save_state(state):
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    cutoff = now_ist() - timedelta(days=14)
    new_seen = {}
    for k, v in state.get("seen", {}).items():
        ts = parse_dt(v)
        if ts and ts >= cutoff:
            new_seen[k] = v
    state["seen"] = new_seen
    STATE_PATH.write_text(
        json.dumps(state, indent=2, ensure_ascii=False),
        encoding="utf-8"
    )

def fetch_google_news(query: str, limit: int):
    url = (
        "https://news.google.com/rss/search?q="
        + quote_plus(query)
        + "&hl=en-IN&gl=IN&ceid=IN:en"
    )
    r = requests.get(url, headers=HEADERS, timeout=18)
    r.raise_for_status()
    feed = feedparser.parse(r.content)
    return feed.entries[:limit]

def resolve_url(url: str):
    url = normalize_url(url)
    if not url:
        return ""
    try:
        r = requests.get(
            url,
            headers=HEADERS,
            timeout=18,
            allow_redirects=True,
            stream=True
        )
        final = normalize_url(r.url) or url
        r.close()
        return final
    except Exception:
        return url

def extract_page(url: str, max_chars: int):
    if not url:
        return {}
    try:
        r = requests.get(url, headers=HEADERS, timeout=18)
        r.raise_for_status()
        content_type = (r.headers.get("content-type") or "").lower()
        if "text/html" not in content_type:
            return {"status": r.status_code, "text": ""}
        soup = BeautifulSoup(r.text[:max_chars], "html.parser")

        def meta(*names):
            for name in names:
                tag = soup.find("meta", attrs={"name": name})
                if tag and tag.get("content"):
                    return clean_text(tag["content"])
                tag = soup.find("meta", attrs={"property": name})
                if tag and tag.get("content"):
                    return clean_text(tag["content"])
            return ""

        published = meta(
            "article:published_time", "datePublished", "publish_date",
            "parsely-pub-date", "pubdate"
        )
        modified = meta(
            "article:modified_time", "dateModified", "last-modified"
        )

        jsonld = []
        for s in soup.find_all("script", attrs={"type": "application/ld+json"}):
            if s.string:
                jsonld.append(s.string[:30000])

        text = clean_text(soup.get_text(" ", strip=True))
        return {
            "status": r.status_code,
            "text": text[:max_chars],
            "published": published,
            "modified": modified,
            "jsonld": jsonld,
            "title": clean_text(soup.title.get_text(" ", strip=True)) if soup.title else ""
        }
    except Exception as e:
        return {"error": str(e), "text": ""}

def extract_jsonld_dates(jsonld_blocks):
    published = None
    modified = None
    for raw in jsonld_blocks or []:
        for key, target in [("datePublished", "published"), ("dateModified", "modified")]:
            m = re.search(rf'"{key}"\s*:\s*"([^"]+)"', raw, flags=re.I)
            if m:
                dt = parse_dt(m.group(1))
                if target == "published" and not published:
                    published = dt
                if target == "modified" and not modified:
                    modified = dt
    return published, modified

def infer_company(title: str, text: str, source: str):
    parts = re.split(r"\s+[|–—:-]\s+", title, maxsplit=1)
    candidates = []
    if len(parts) == 2:
        candidates.append(parts[0].strip())
        candidates.append(parts[1].strip())
    # Prefer a common company pattern in title.
    for sep in [" at ", " @ "]:
        if sep in title.lower():
            tail = re.split(sep, title, flags=re.I)[-1].strip()
            if 2 <= len(tail.split()) <= 8:
                candidates.append(tail)
    # Known source title fragments often include company names.
    if source and source.lower() not in {"news.google.com"}:
        candidates.append(source)
    for c in candidates:
        if c and not re.search(r"\b(job|jobs|careers)\b", c, re.I):
            return clean_text(c)
    return "Not disclosed"

def infer_branches(blob: str, cfg):
    lower = blob.lower()
    found = []
    for label, terms in cfg["branch_keywords"].items():
        if any(t in lower for t in terms):
            found.append(label)
    if not found:
        return "Not disclosed"
    if "Any B.E./B.Tech" in found and len(found) > 1:
        found.remove("Any B.E./B.Tech")
    return ", ".join(found)

def infer_experience(blob: str):
    lower = blob.lower()
    if "freshers" in lower or "fresher" in lower:
        return "Fresher"
    patterns = [
        r"\b0\s*[-to]{1,3}\s*1\s*years?\b",
        r"\b0\s*[-to]{1,3}\s*2\s*years?\b",
        r"\b1\s*[-to]{1,3}\s*2\s*years?\b"
    ]
    for p in patterns:
        if re.search(p, lower):
            return re.search(p, lower).group(0)
    if "no experience" in lower:
        return "No experience required"
    return "Not disclosed"

def infer_salary(blob: str):
    patterns = [
        r"(₹\s?[0-9][0-9,]*(?:\s*-\s*₹?\s*[0-9][0-9,]*)?\s*(?:lpa|lakhs?|per month|/month)?)",
        r"([0-9]+(?:\.[0-9]+)?\s*LPA)"
    ]
    for p in patterns:
        m = re.search(p, blob, flags=re.I)
        if m:
            return clean_text(m.group(1))
    return "Not disclosed"

def infer_deadline(blob: str):
    patterns = [
        r"(?:apply|application)\s+(?:by|before|until)\s+([A-Za-z]{3,9}\s+\d{1,2},?\s+\d{4})",
        r"(?:last date|deadline)\s*[:\-]?\s*([A-Za-z]{3,9}\s+\d{1,2},?\s+\d{4})"
    ]
    for p in patterns:
        m = re.search(p, blob, flags=re.I)
        if m:
            return clean_text(m.group(1))
    return "Not disclosed"

def looks_closed(blob: str):
    lower = blob.lower()
    bad = [
        "job is no longer available",
        "position has been filled",
        "this job has expired",
        "applications are closed",
        "application closed",
        "no longer accepting applications",
        "job not found",
        "404 not found"
    ]
    return any(x in lower for x in bad)

def substantial_experience(blob: str):
    lower = blob.lower()
    # Exclude clear multi-year requirements; allow "2+ years" only if the role is
    # otherwise strongly fresher-oriented.
    ranges = re.findall(r"\b([3-9]|[1-9][0-9])\s*\+\s*years?\b", lower)
    if ranges:
        return True
    if re.search(r"\b[3-9]\s*[-to]\s*[5-9]\s*years?\b", lower):
        return True
    return False

def fresher_relevant(title, blob, cfg):
    lower = f"{title} {blob}".lower()
    has_positive = any(t in lower for t in cfg["positive_terms"])
    has_negative = any(t in lower for t in cfg["negative_terms"])
    engineering = any(
        k in lower for k in [
            "engineer", "engineering", "b.e", "btech", "b.tech",
            "graduate engineer", "trainee engineer"
        ]
    )
    return engineering and has_positive and not has_negative and not substantial_experience(lower)

def score(item):
    title = item["title"].lower()
    s = 0
    for phrase, pts in [
        ("fresher", 20), ("graduate engineer trainee", 25),
        ("graduate trainee", 20), ("0-1", 15), ("2026", 10),
        ("entry level", 15), ("trainee engineer", 18)
    ]:
        if phrase in title:
            s += pts
    if item["location"].lower() in item["blob"].lower():
        s += 5
    if item["new"]:
        s += 30
    return s

def build_items(cfg, state):
    last_run = parse_dt(state.get("last_run_at")) or (now_ist() - timedelta(hours=48))
    seen = state.get("seen", {})
    candidates = []
    errors = []

    for location in cfg["locations"]:
        for template in cfg["search_templates"]:
            query = template.format(location=location)
            try:
                entries = fetch_google_news(query, cfg["max_items_per_query"])
            except Exception as e:
                errors.append(f"{location}: {e}")
                continue

            for entry in entries:
                title = clean_text(entry.get("title", ""))
                summary = clean_text(entry.get("summary", ""))
                source = clean_text(
                    (entry.get("source") or {}).get("title", "")
                    if isinstance(entry.get("source"), dict)
                    else ""
                ) or "Google News"
                rss_date = parse_dt(entry.get("published") or entry.get("updated"))
                raw_url = normalize_url(entry.get("link", ""))
                final_url = resolve_url(raw_url)
                page = extract_page(final_url, cfg["max_page_chars"])
                page_text = clean_text(page.get("text", ""))
                blob = f"{title} {summary} {page_text}"
                if not fresher_relevant(title, blob, cfg):
                    continue
                if looks_closed(blob):
                    continue
                if substantial_experience(blob):
                    continue

                page_pub = parse_dt(page.get("published"))
                page_mod = parse_dt(page.get("modified"))
                jsonld_pub, jsonld_mod = extract_jsonld_dates(page.get("jsonld", []))
                job_date = page_mod or jsonld_mod or page_pub or jsonld_pub or rss_date

                if job_date and job_date < last_run and normalize_key(title, "", final_url) in seen:
                    continue

                company = infer_company(title, page_text or summary, source)
                branches = infer_branches(blob, cfg)
                exp = infer_experience(blob)
                salary = infer_salary(blob)
                deadline = infer_deadline(blob)

                key = normalize_key(title, company, final_url)
                is_new = key not in seen or (job_date and job_date >= last_run)

                candidates.append({
                    "key": key,
                    "title": title,
                    "company": company,
                    "location": location,
                    "branches": branches,
                    "experience": exp,
                    "salary": salary,
                    "deadline": deadline,
                    "source": source,
                    "url": final_url or raw_url,
                    "job_date": iso(job_date),
                    "rss_date": iso(rss_date),
                    "new": bool(is_new),
                    "blob": blob,
                })

    # Deduplicate by key.
    dedup = {}
    for item in candidates:
        old = dedup.get(item["key"])
        if old is None or (item["new"] and not old["new"]):
            dedup[item["key"]] = item

    items = list(dedup.values())
    for item in items:
        item["score"] = score(item)

    items.sort(key=lambda x: (x["new"], x["score"], x["job_date"] or ""), reverse=True)
    return items[:cfg["max_total_items"]], errors

def report_text(items, errors, generated):
    lines = [
        "DAILY B.E./B.TECH FRESHER ENGINEERING JOB ALERT",
        f"Alert generated: {generated.strftime('%d %B %Y, %I:%M %p IST')}",
        "Coverage: Coimbatore, Bengaluru, Chennai, Hyderabad, Kerala (Kochi & Thiruvananthapuram)",
        "",
        f"Verified/relevant openings found: {len(items)}",
        ""
    ]

    for idx, x in enumerate(items, 1):
        freshness = "NEW/UPDATED" if x["new"] else "RECENT"
        lines.extend([
            f"{idx}. [{freshness}] {x['company']} — {x['title']}",
            f"Location: {x['location']}",
            f"Posting/update date: {x['job_date'] or 'Not disclosed'}",
            f"Eligible branches: {x['branches']}",
            f"Batch/experience: {x['experience']}",
            f"Salary/CTC: {x['salary']}",
            "Job type: Full-time / Entry-level (verify on employer page)",
            f"Important skills/requirements: {x['blob'][:450].strip()}",
            f"Application deadline: {x['deadline']}",
            f"Source: {x['source']}",
            f"Direct application: {x['url']}",
            ""
        ])

    if not items:
        lines.append("No high-confidence new/open fresher engineering roles were identified in this alert window.")
        lines.append("Search will continue and newly posted/updated roles will be prioritized in the next alert.")

    lines.extend([
        "VERIFICATION NOTE",
        "Roles were filtered using fresher/entry-level signals and closed-role indicators from the available listing text. Job boards can change status quickly; confirm the employer/application page before applying."
    ])
    if errors:
        lines.extend(["", "SOURCE WARNINGS"] + [f"- {e}" for e in errors[:10]])
    return "\n".join(lines)

def send_email(subject, body):
    """
    Primary: Brevo Transactional Email API (free tier).
    Fallback: SMTP if BREVO_API_KEY is not supplied.
    """
    recipient = os.getenv("MAIL_TO") or "placement@ksrct.ac.in"
    brevo_key = os.getenv("BREVO_API_KEY")
    brevo_from = os.getenv("MAIL_FROM")

    if brevo_key:
        if not brevo_from:
            raise RuntimeError("MAIL_FROM must be set when using BREVO_API_KEY.")

        payload = {
            "sender": {"email": brevo_from, "name": "KSRCT Placement Office"},
            "to": [{"email": recipient}],
            "subject": subject,
            "textContent": body,
        }

        r = requests.post(
            "https://api.brevo.com/v3/smtp/email",
            headers={
                "accept": "application/json",
                "api-key": brevo_key,
                "content-type": "application/json",
            },
            json=payload,
            timeout=30,
        )
        if r.status_code >= 300:
            raise RuntimeError(
                f"Brevo email failed ({r.status_code}): {r.text[:1000]}"
            )
        return

    host = os.getenv("SMTP_HOST") or "smtp.gmail.com"
    port = int(os.getenv("SMTP_PORT") or "465")
    user = os.getenv("SMTP_USER")
    password = os.getenv("SMTP_PASSWORD")
    use_ssl = (os.getenv("SMTP_USE_SSL") or "true").lower() == "true"

    if not user or not password:
        raise RuntimeError(
            "Set BREVO_API_KEY + MAIL_FROM (recommended) or SMTP_USER + SMTP_PASSWORD."
        )

    msg = EmailMessage()
    msg["From"] = user
    msg["To"] = recipient
    msg["Subject"] = subject
    msg["Date"] = format_datetime(datetime.now(UTC))
    msg.set_content(body)

    if use_ssl:
        context = ssl.create_default_context()
        with smtplib.SMTP_SSL(host, port, context=context, timeout=30) as smtp:
            smtp.login(user, password)
            smtp.send_message(msg)
    else:
        with smtplib.SMTP(host, port, timeout=30) as smtp:
            smtp.ehlo()
            smtp.starttls(context=ssl.create_default_context())
            smtp.ehlo()
            smtp.login(user, password)
            smtp.send_message(msg)

def main():
    cfg = load_config()
    state = load_state()
    generated = now_ist()
    items, errors = build_items(cfg, state)

    body = report_text(items, errors, generated)
    subject = f"Daily B.E./B.Tech Fresher Engineering Job Alert — {generated.strftime('%d %b %Y')}"
    send_email(subject, body)

    # Update seen state only after the mail was successfully accepted by SMTP.
    for item in items:
        state.setdefault("seen", {})[item["key"]] = iso(generated)
    state["last_run_at"] = iso(generated)
    save_state(state)

    print(body)

if __name__ == "__main__":
    main()
