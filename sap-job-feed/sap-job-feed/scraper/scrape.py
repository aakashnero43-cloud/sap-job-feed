#!/usr/bin/env python3
"""
Nightly SAP BRIM / FI-CA job discovery feed.

Runs JobSpy (login-free, public listings) plus optional public ATS feeds,
filters to relevant postings, de-duplicates against data/seen.json and
writes data/jobs.json — a compact file a downstream agent can fetch.

No personal data is read or written by this script. Job descriptions are
untrusted third-party text and are stored verbatim (truncated) as data.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests
import yaml

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
CFG = yaml.safe_load((ROOT / "scraper" / "config.yaml").read_text(encoding="utf-8"))

UTC = timezone.utc
NOW = datetime.now(UTC)
TODAY = NOW.date().isoformat()

MUST = [re.compile(p, re.I) for p in CFG["must_match"]]
EXCL = [re.compile(p, re.I) for p in CFG.get("exclude_companies", [])]

IN_PAT = re.compile(r"India|Bengaluru|Bangalore|Pune|Hyderabad|Mumbai|Chennai|Gurgaon|Gurugram|Noida|Kolkata|Kochi|Ahmedabad|Gandhinagar|\bIN\b", re.I)
US_PAT = re.compile(r"United States|\bUSA?\b|,\s*[A-Z]{2}(?:\s|$)", re.I)


def log(*a):
    print(time.strftime("%H:%M:%S"), *a, file=sys.stderr, flush=True)


def norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (s or "").lower()).strip()


def job_key(company: str, title: str, location: str) -> str:
    raw = f"{norm(company)}|{norm(title)}|{norm(location)}"
    return hashlib.sha1(raw.encode()).hexdigest()[:16]


def region_of(location: str) -> str:
    loc = location or ""
    if IN_PAT.search(loc) and not US_PAT.search(loc):
        return "India"
    if US_PAT.search(loc):
        return "US"
    if IN_PAT.search(loc):
        return "India"
    return "Other"


def relevant(title: str, desc: str, company: str) -> bool:
    if any(p.search(company or "") for p in EXCL):
        return False
    text = f"{title}\n{desc or ''}"
    return any(p.search(text) for p in MUST)


def clean(s) -> str:
    if s is None:
        return ""
    if isinstance(s, float):
        return "" if s != s else str(s)  # NaN guard
    return str(s).strip()


# --------------------------------------------------------------------------
# JobSpy
# --------------------------------------------------------------------------
def run_jobspy() -> list[dict]:
    from jobspy import scrape_jobs

    out: list[dict] = []
    sites = CFG["sites"]
    terms = list(CFG["search_terms"])
    terms += [f"SAP BRIM {c}" for c in CFG.get("target_companies", [])]
    for region in CFG["regions"]:
        for term in terms:
            for site in sites:
                try:
                    df = scrape_jobs(
                        site_name=[site],
                        search_term=term,
                        google_search_term=f"{term} jobs in {region['location']} since last week",
                        location=region["location"],
                        country_indeed=region.get("country_indeed", "usa"),
                        is_remote=bool(region.get("remote", False)),
                        results_wanted=int(CFG.get("results_per_query", 25)),
                        hours_old=int(os.environ.get("FEED_HOURS_OLD") or CFG.get("hours_old", 96)),
                        linkedin_fetch_description=True,
                        description_format="markdown",
                        verbose=0,
                    )
                except Exception as e:  # a blocked site must never fail the run
                    log(f"[{site}] {region['name']} '{term}' -> {type(e).__name__}: {str(e)[:120]}")
                    continue
                n = 0
                for _, r in df.iterrows():
                    rec = {
                        "source": site,
                        "title": clean(r.get("title")),
                        "company": clean(r.get("company")),
                        "location": clean(r.get("location")),
                        "remote": bool(r.get("is_remote")) if clean(r.get("is_remote")) else bool(region.get("remote")),
                        "posted_at": clean(r.get("date_posted")),
                        "apply_url": clean(r.get("job_url_direct")) or clean(r.get("job_url")),
                        "listing_url": clean(r.get("job_url")),
                        "job_type": clean(r.get("job_type")),
                        "salary": " ".join(x for x in [clean(r.get("min_amount")), clean(r.get("max_amount")), clean(r.get("currency")), clean(r.get("interval"))] if x),
                        "description": clean(r.get("description"))[: int(CFG.get("description_max_chars", 6000))],
                        "query": term,
                        "region_hint": region["name"],
                    }
                    if rec["title"] and rec["company"]:
                        out.append(rec)
                        n += 1
                log(f"[{site}] {region['name']} '{term}' -> {n}")
                time.sleep(1.5)  # be polite; also keeps LinkedIn under its per-IP threshold
    return out


# --------------------------------------------------------------------------
# Public ATS feeds (official, unauthenticated JSON endpoints)
# --------------------------------------------------------------------------
def run_ats_feeds() -> list[dict]:
    out: list[dict] = []
    s = requests.Session()
    s.headers["User-Agent"] = "sap-job-feed/1.0 (+github actions; public job board API)"
    for f in CFG.get("ats_feeds", []) or []:
        try:
            t = f["type"]
            if t == "greenhouse":
                url = f"https://boards-api.greenhouse.io/v1/boards/{f['token']}/jobs?content=true"
                for j in s.get(url, timeout=30).json().get("jobs", []):
                    out.append({"source": "greenhouse", "title": j.get("title", ""), "company": f.get("company", f["token"]),
                                "location": (j.get("location") or {}).get("name", ""), "remote": False,
                                "posted_at": (j.get("updated_at") or "")[:10], "apply_url": j.get("absolute_url", ""),
                                "listing_url": j.get("absolute_url", ""), "job_type": "", "salary": "",
                                "description": re.sub(r"<[^>]+>", " ", j.get("content", ""))[: CFG["description_max_chars"]],
                                "query": "ats", "region_hint": ""})
            elif t == "lever":
                url = f"https://api.lever.co/v0/postings/{f['company']}?mode=json"
                for j in s.get(url, timeout=30).json():
                    out.append({"source": "lever", "title": j.get("text", ""), "company": f.get("name", f["company"]),
                                "location": (j.get("categories") or {}).get("location", ""), "remote": "remote" in json.dumps(j.get("workplaceType", "")).lower(),
                                "posted_at": datetime.fromtimestamp(j.get("createdAt", 0) / 1000, UTC).date().isoformat() if j.get("createdAt") else "",
                                "apply_url": j.get("applyUrl") or j.get("hostedUrl", ""), "listing_url": j.get("hostedUrl", ""),
                                "job_type": (j.get("categories") or {}).get("commitment", ""), "salary": "",
                                "description": re.sub(r"<[^>]+>", " ", j.get("descriptionPlain") or j.get("description", ""))[: CFG["description_max_chars"]],
                                "query": "ats", "region_hint": ""})
            elif t == "ashby":
                url = f"https://api.ashbyhq.com/posting-api/job-board/{f['name']}?includeCompensation=true"
                for j in s.get(url, timeout=30).json().get("jobs", []):
                    out.append({"source": "ashby", "title": j.get("title", ""), "company": f.get("company", f["name"]),
                                "location": j.get("location", ""), "remote": bool(j.get("isRemote")),
                                "posted_at": (j.get("publishedAt") or "")[:10], "apply_url": j.get("applyUrl") or j.get("jobUrl", ""),
                                "listing_url": j.get("jobUrl", ""), "job_type": j.get("employmentType", ""), "salary": "",
                                "description": (j.get("descriptionPlain") or "")[: CFG["description_max_chars"]],
                                "query": "ats", "region_hint": ""})
        except Exception as e:
            log(f"[ats:{f}] {type(e).__name__}: {str(e)[:120]}")
    return out


# --------------------------------------------------------------------------
def main() -> int:
    DATA.mkdir(exist_ok=True)
    seen_path = DATA / "seen.json"
    seen: dict[str, dict] = json.loads(seen_path.read_text(encoding="utf-8")) if seen_path.exists() else {}

    raw = run_jobspy() + run_ats_feeds()
    log(f"raw postings: {len(raw)}")

    merged: dict[str, dict] = {}
    for r in raw:
        if not relevant(r["title"], r["description"], r["company"]):
            continue
        k = job_key(r["company"], r["title"], r["location"])
        if k in merged:
            # keep the richer description / direct apply link
            if len(r["description"]) > len(merged[k]["description"]):
                merged[k]["description"] = r["description"]
            if not merged[k]["apply_url"] and r["apply_url"]:
                merged[k]["apply_url"] = r["apply_url"]
            merged[k]["sources"] = sorted(set(merged[k]["sources"] + [r["source"]]))
            continue
        r["id"] = k
        r["region"] = region_of(r["location"]) if r["region_hint"] != "US-remote" else "US"
        r["sources"] = [r.pop("source")]
        r.pop("region_hint", None)
        merged[k] = r

    new_ids = []
    for k, r in merged.items():
        if k not in seen:
            seen[k] = {"first_seen": TODAY, "company": r["company"], "title": r["title"], "location": r["location"]}
            new_ids.append(k)
        seen[k]["last_seen"] = TODAY
        r["first_seen"] = seen[k]["first_seen"]

    window = NOW.date() - timedelta(days=int(CFG.get("new_window_days", 4)))
    emit = [r for r in merged.values() if datetime.fromisoformat(r["first_seen"]).date() >= window]
    emit.sort(key=lambda r: (r["first_seen"], r["company"], r["title"]), reverse=True)

    # prune the seen index after 120 days so it never grows unbounded
    cutoff = (NOW.date() - timedelta(days=120)).isoformat()
    seen = {k: v for k, v in seen.items() if v.get("last_seen", TODAY) >= cutoff}

    out = {
        "generated_at": NOW.isoformat(timespec="seconds"),
        "window_days": int(CFG.get("new_window_days", 4)),
        "counts": {"raw": len(raw), "relevant": len(merged), "new_today": len(new_ids), "emitted": len(emit)},
        "jobs": emit,
    }
    (DATA / "jobs.json").write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    seen_path.write_text(json.dumps(seen, ensure_ascii=False, indent=0), encoding="utf-8")

    # tiny human-readable summary for the Action log / README badge
    lines = [f"# Feed summary {TODAY}", "", f"raw {len(raw)} · relevant {len(merged)} · new today {len(new_ids)} · emitted {len(emit)}", ""]
    for r in emit[:40]:
        lines.append(f"- {r['first_seen']} · **{r['company']}** — {r['title']} · {r['location']} · {'/'.join(r['sources'])}")
    (DATA / "SUMMARY.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    log(f"done: relevant={len(merged)} new={len(new_ids)} emitted={len(emit)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
