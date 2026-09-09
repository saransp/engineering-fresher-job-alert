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

HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; KSRCT-Placement-JobAlert/1.0)"}

def now_ist(): return datetime.now(TZ)
def iso(dt): return dt.isoformat() if dt else None

def parse_dt(value):
    if not value: return None
    try:
        dt = dateparser.parse(str(value))
        if dt.tzinfo is None: dt = dt.replace(tzinfo=UTC)
        return dt.astimezone(TZ)
    except Exception: return None

def clean_text(text): return re.sub(r"\s+", " ", html.unescape(str(text or ""))).strip()

def normalize_url(url):
    if not url: return ""
    p = urlparse(url)
    return p._replace(fragment="").geturl() if p.scheme in ("http", "https") else ""

def key_for(title, company, url):
    base = re.sub(r"[^a-z0-9]+", " ", f"{title}|{company}|{url}".lower()).strip()
    return hashlib.sha1(base.encode()).hexdigest()

def load_state():
    try: return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except Exception: return {"last_run_at": None, "seen": {}}

def save_state(state):
    cutoff = now_ist() - timedelta(days=14)
    state["seen"] = {k:v for k,v in state.get("seen", {}).items() if (parse_dt(v) or now_ist()) >= cutoff}
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8")

def fetch_feed(query, limit=12):
    url = f"https://news.google.com/rss/search?q={quote_plus(query)}&hl=en-IN&gl=IN&ceid=IN:en"
    r = requests.get(url, headers=HEADERS, timeout=18)
    r.raise_for_status()
    return feedparser.parse(r.content).entries[:limit]

def resolve_url(url):
    url = normalize_url(url)
    if not url: return ""
    try:
        r = requests.get(url, headers=HEADERS, timeout=18, allow_redirects=True, stream=True)
        final = normalize_url(r.url) or url
        r.close(); return final
    except Exception: return url

def fetch_page(url):
    try:
        r = requests.get(url, headers=HEADERS, timeout=18)
        r.raise_for_status()
        if "text/html" not in (r.headers.get("content-type") or "").lower(): return {"text":"", "published":None, "modified":None}
        soup = BeautifulSoup(r.text[:160000], "html.parser")
        def meta(*names):
            for n in names:
                t = soup.find("meta", attrs={"name": n}) or soup.find("meta", attrs={"property": n})
                if t and t.get("content"): return clean_text(t["content"])
            return ""
        return {"text": clean_text(soup.get_text(" ", strip=True)), "published": meta("article:published_time","datePublished","publish_date","parsely-pub-date"), "modified": meta("article:modified_time","dateModified","last-modified")}
    except Exception: return {"text":"", "published":None, "modified":None}

def infer_company(title, source):
    for sep in [" at ", " @ ", " – ", " — ", " | "]:
        if sep.lower() in title.lower():
            parts = re.split(re.escape(sep), title, maxsplit=1, flags=re.I)
            if len(parts) == 2:
                for p in reversed(parts):
                    p = clean_text(p)
                    if p and not re.search(r"\b(jobs?|careers?)\b", p, re.I): return p
    return clean_text(source) or "Not disclosed"

def infer_branches(blob, cfg):
    low = blob.lower(); found=[]
    for label, terms in cfg["branch_keywords"].items():
        if any(t.lower() in low for t in terms): found.append(label)
    if "Any B.E./B.Tech" in found and len(found)>1: found.remove("Any B.E./B.Tech")
    return ", ".join(found) if found else "Not disclosed"

def infer_exp(blob):
    low=blob.lower()
    for phrase in ["freshers","fresher","no experience","0-1 year","0 to 1 year","0-2 years","0 to 2 years","2026 batch","2025 batch"]:
        if phrase in low: return "Fresher / entry-level"
    return "Not disclosed"

def infer_salary(blob):
    m=re.search(r"(₹\s?[0-9][0-9,]*(?:\s*[-–]\s*₹?\s*[0-9][0-9,]*)?\s*(?:LPA|lakhs?|/month|per month)?)",blob,re.I)
    return clean_text(m.group(1)) if m else "Not disclosed"

def infer_deadline(blob):
    m=re.search(r"(?:deadline|last date|apply by|applications? close(?:s|d)?)(?:\s*[:\-]\s*|\s+)([A-Za-z]{3,9}\s+\d{1,2},?\s+\d{4})",blob,re.I)
    return clean_text(m.group(1)) if m else "Not disclosed"

def looks_closed(blob):
    low=blob.lower()
    return any(x in low for x in ["job has expired","job is no longer available","position has been filled","applications are closed","application closed","no longer accepting applications","job not found","404 not found"])

def substantial_experience(blob):
    low=blob.lower()
    return bool(re.search(r"\b(?:3\s*\+|4\s*\+|5\s*\+|6\s*\+|7\s*\+|8\s*\+|9\s*\+|10\s*\+)\s*years?\b",low) or re.search(r"\b[3-9]\s*[-–to]\s*[5-9]\s*years?\b",low))

def relevant(title, blob, cfg):
    low=f"{title} {blob}".lower()
    return any(x in low for x in ["engineer","engineering","b.e","btech","b.tech","graduate engineer"]) and any(x in low for x in cfg["positive_terms"]) and not any(x in low for x in cfg["negative_terms"]) and not substantial_experience(low)

def collect(cfg, state):
    last=parse_dt(state.get("last_run_at")) or (now_ist()-timedelta(hours=48)); seen=state.get("seen",{}); out={}; errors=[]
    for loc in cfg["locations"]:
        for template in cfg["search_templates"]:
            try: entries=fetch_feed(template.format(location=loc),cfg["max_items_per_query"])
            except Exception as e: errors.append(f"{loc}: {e}"); continue
            for e in entries:
                title=clean_text(e.get("title")); summary=clean_text(e.get("summary")); source=clean_text((e.get("source") or {}).get("title", "")) or "Google News"
                rss_date=parse_dt(e.get("published") or e.get("updated")); raw=normalize_url(e.get("link")); url=resolve_url(raw); page=fetch_page(url); blob=clean_text(f"{title} {summary} {page.get('text','')}")
                if not relevant(title,blob,cfg) or looks_closed(blob): continue
                job_date=parse_dt(page.get("modified")) or parse_dt(page.get("published")) or rss_date; company=infer_company(title,source); key=key_for(title,company,url)
                if key in seen and job_date and job_date < last: continue
                out[key]={"key":key,"title":title,"company":company,"location":loc,"branches":infer_branches(blob,cfg),"experience":infer_exp(blob),"salary":infer_salary(blob),"deadline":infer_deadline(blob),"source":source,"url":url or raw,"job_date":iso(job_date),"new":bool((key not in seen) or (job_date and job_date>=last)),"blob":blob[:650]}
    items=list(out.values()); items.sort(key=lambda x:(x["new"],x.get("job_date") or ""),reverse=True); return items[:cfg["max_total_items"]],errors

def make_report(items,errors,generated):
    lines=["DAILY B.E./B.TECH FRESHER ENGINEERING JOB ALERT",f"Alert generated: {generated.strftime('%d %B %Y, %I:%M %p IST')}","Coverage: Coimbatore, Bengaluru, Chennai, Hyderabad, Kerala (Kochi & Thiruvananthapuram)",f"Relevant openings: {len(items)}",""]
    for i,x in enumerate(items,1):
        lines += [f"{i}. {'NEW/UPDATED' if x['new'] else 'RECENT'} — {x['company']} — {x['title']}",f"Location: {x['location']}",f"Posting/update date: {x['job_date'] or 'Not disclosed'}",f"Eligible B.E./B.Tech branches: {x['branches']}",f"Batch/experience: {x['experience']}",f"Salary/CTC: {x['salary']}","Job type: Full-time / entry-level where indicated",f"Important skills/requirements: {x['blob']}",f"Application deadline: {x['deadline']}",f"Source: {x['source']}",f"Direct application: {x['url']}",""]
    if not items: lines += ["No high-confidence new/open fresher engineering roles were identified in this alert window.",""]
    lines += ["VERIFICATION NOTE","Listings were screened for fresher/entry-level signals and obvious closed/expired indicators in the accessible public listing text. Confirm the employer application page before applying."]
    if errors: lines += ["","SOURCE WARNINGS"]+[f"- {e}" for e in errors[:10]]
    return "\n".join(lines)

def send_email(subject,body):
    recipient=os.getenv("MAIL_TO","placement@ksrct.ac.in"); brevo=os.getenv("BREVO_API_KEY"); sender=os.getenv("MAIL_FROM")
    if brevo:
        if not sender: raise RuntimeError("MAIL_FROM must be set when using BREVO_API_KEY.")
        r=requests.post("https://api.brevo.com/v3/smtp/email",headers={"accept":"application/json","api-key":brevo,"content-type":"application/json"},json={"sender":{"email":sender,"name":"KSRCT Placement Office"},"to":[{"email":recipient}],"subject":subject,"textContent":body},timeout=30)
        if r.status_code>=300: raise RuntimeError(f"Brevo email failed ({r.status_code}): {r.text[:1000]}")
        return
    host=os.getenv("SMTP_HOST","smtp.gmail.com"); port=int(os.getenv("SMTP_PORT","465")); user=os.getenv("SMTP_USER"); password=os.getenv("SMTP_PASSWORD")
    if not user or not password: raise RuntimeError("Set BREVO_API_KEY + MAIL_FROM, or SMTP_USER + SMTP_PASSWORD.")
    msg=EmailMessage(); msg["From"]=user; msg["To"]=recipient; msg["Subject"]=subject; msg["Date"]=format_datetime(datetime.now(UTC)); msg.set_content(body)
    with smtplib.SMTP_SSL(host,port,context=ssl.create_default_context(),timeout=30) as smtp: smtp.login(user,password); smtp.send_message(msg)

def main():
    cfg=json.loads(CONFIG_PATH.read_text(encoding="utf-8")); state=load_state(); generated=now_ist(); items,errors=collect(cfg,state); body=make_report(items,errors,generated)
    send_email(f"Daily B.E./B.Tech Fresher Engineering Job Alert — {generated.strftime('%d %b %Y')}",body)
    for x in items: state.setdefault("seen",{})[x["key"]]=iso(generated)
    state["last_run_at"]=iso(generated); save_state(state); print(body)

if __name__=="__main__": main()
