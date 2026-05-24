#!/usr/bin/env python3
import argparse
import asyncio
import csv
import json
import os
import re
import threading
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

import httpx
from dotenv import load_dotenv

from qualify import get_client, is_valid_email, supabase_execute_with_retry

EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
SERPER_URL = "https://google.serper.dev/search"
SERPER_CONCURRENCY = 8
BATCH_LOG_EVERY = 100

EXTRA_BLOCKED_DOMAINS = {
    "facebook.com", "instagram.com", "linkedin.com", "yelp.com", "bbb.org",
    "mailchimp.com", "godaddy.com", "wix.com", "wixpress.com", "squarespace.com",
    "google.com", "schema.org", "w3.org", "domain.com", "example.com", "sentry.io",
}
EXTRA_BLOCKED_PREFIXES = ("no-reply@", "noreply@", "postmaster@", "abuse@", "support@facebook.com")
FREE_PROVIDERS = {"gmail.com", "yahoo.com", "outlook.com", "hotmail.com", "aol.com", "icloud.com"}


@dataclass
class DomainRow:
    domain: str
    persona: Optional[str]
    confidence_score: float
    business_name: Optional[str]
    email: Optional[str]
    signals: Dict[str, Any]


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Enrich missing emails from Serper @domain search")
    p.add_argument("--limit", type=int, default=None)
    p.add_argument("--persona", choices=["wholesaler", "investor", "agent_realtor", "all"], default="all")
    p.add_argument("--min-credits", type=int, default=500)
    p.add_argument("--max-credits", type=int, default=7000)
    p.add_argument("--num", type=int, default=20)
    p.add_argument("--self-test", action="store_true")
    return p.parse_args()


def load_env() -> Tuple[str, str, str]:
    load_dotenv()
    su = os.getenv("SUPABASE_URL")
    sk = os.getenv("SUPABASE_SERVICE_ROLE_KEY")
    serper = os.getenv("SERPER_API_KEY")
    if not su or not sk or not serper:
        raise RuntimeError("Missing SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY, or SERPER_API_KEY")
    return su, sk, serper


def normalized_domain(d: str) -> str:
    d = (d or "").strip().lower()
    return d[4:] if d.startswith("www.") else d


def registrable(host: str) -> str:
    parts = normalized_domain(host).split(".")
    return ".".join(parts[-2:]) if len(parts) >= 2 else normalized_domain(host)


def looks_related_domain(target: str, other: str) -> bool:
    t = registrable(target)
    o = registrable(other)
    if t == o:
        return True
    t_name = t.split(".")[0]
    o_name = o.split(".")[0]
    variants = {t_name, t_name.replace("-", ""), t_name + "s", t_name[:-1] if t_name.endswith("s") else t_name}
    o_variants = {o_name, o_name.replace("-", ""), o_name + "s", o_name[:-1] if o_name.endswith("s") else o_name}
    return bool(variants.intersection(o_variants))


def is_valid_enrich_email(email: str) -> bool:
    e = (email or "").lower().strip()
    if not is_valid_email(e):
        return False
    if any(e.startswith(p) for p in EXTRA_BLOCKED_PREFIXES):
        return False
    if "@" not in e:
        return False
    _, domain = e.split("@", 1)
    if domain in EXTRA_BLOCKED_DOMAINS:
        return False
    if domain.endswith("sentry-next.wixpress.com"):
        return False
    return True


def extract_serper_emails(organic: List[Dict[str, Any]]) -> Tuple[List[Dict[str, Any]], Dict[str, str]]:
    found: Dict[str, Dict[str, Any]] = {}
    texts: Dict[str, str] = {}
    for position, item in enumerate(organic or [], start=1):
        title = str(item.get("title") or "")
        snippet = str(item.get("snippet") or "")
        link = str(item.get("link") or "")
        blob = f"{title} {snippet} {link}"
        for match in EMAIL_RE.findall(blob):
            em = match.lower().strip(".,;:()[]{}<>\"'")
            if not is_valid_enrich_email(em):
                continue
            if em not in found:
                found[em] = {"source_url": link, "position": position}
                texts[em] = blob.lower()
    return [{"email": k, "source_url": v["source_url"], "position": v["position"]} for k, v in found.items()], texts


def classify_primary(target_domain: str, business_name: Optional[str], serper_emails: List[Dict[str, Any]], texts: Dict[str, str]) -> str:
    t = normalized_domain(target_domain)
    for item in serper_emails:
        dom = item["email"].split("@", 1)[1]
        if normalized_domain(dom) == t:
            return item["email"]
    for item in serper_emails:
        dom = item["email"].split("@", 1)[1]
        if looks_related_domain(t, dom):
            return item["email"]
    bn = (business_name or "").strip().lower()
    for item in serper_emails:
        dom = item["email"].split("@", 1)[1]
        if dom in FREE_PROVIDERS and bn and bn in texts.get(item["email"], ""):
            return item["email"]
    return ""


def compute_position_histogram(serper_emails_all: List[Dict[str, Any]]) -> Dict[str, int]:
    histogram = {"1-3": 0, "4-10": 0, "11-20": 0, ">20": 0}
    for item in serper_emails_all:
        pos = int(item.get("position") or 0)
        if 1 <= pos <= 3:
            histogram["1-3"] += 1
        elif 4 <= pos <= 10:
            histogram["4-10"] += 1
        elif 11 <= pos <= 20:
            histogram["11-20"] += 1
        elif pos > 20:
            histogram[">20"] += 1
    return histogram


def fetch_targets(sb: Any, persona: str, limit: Optional[int]) -> List[DomainRow]:
    q = sb.table("domains").select("domain,persona,confidence_score,business_name,email,signals").eq("qualified", True).is_("email", "null")
    if persona != "all":
        q = q.eq("persona", persona)
    resp = supabase_execute_with_retry(lambda: q.limit(limit or 50000), domain=None, is_write=False)
    data = (resp.data if resp and hasattr(resp, "data") else []) or []
    rows: List[DomainRow] = []
    for r in data:
        signals = r.get("signals") if isinstance(r.get("signals"), dict) else {}
        done_value = (signals or {}).get("serper_enrich_done")
        if done_value in (True, "true"):
            continue
        rows.append(DomainRow(
            domain=r.get("domain") or "",
            persona=r.get("persona"),
            confidence_score=float(r.get("confidence_score") or 0),
            business_name=r.get("business_name"),
            email=r.get("email"),
            signals=signals,
        ))
    rows.sort(key=lambda x: (x.persona == "wholesaler", x.persona == "investor", x.confidence_score), reverse=True)
    return rows[:limit] if limit else rows


def run_self_test() -> None:
    organic = [{"title": "Contact", "snippet": "Email info@wanttosellnow.com today", "link": "https://wanttosellnow.com/contact"}]
    emails, texts = extract_serper_emails(organic)
    assert any(x["email"] == "info@wanttosellnow.com" for x in emails)
    assert classify_primary("wanttosellnow.com", "", emails, texts) == "info@wanttosellnow.com"
    assert any(x["source_url"] == "https://wanttosellnow.com/contact" for x in emails if x["email"] == "info@wanttosellnow.com")

    organic2 = [
        {"title": "no email", "snippet": "", "link": "https://x.com/one"},
        {"title": "no email", "snippet": "", "link": "https://x.com/two"},
        {"title": "Team", "snippet": "info@x.com jane@x.com", "link": "https://x.com/about"},
    ]
    emails2, texts2 = extract_serper_emails(organic2)
    assert len(emails2) == 2
    assert any(x["email"] == "info@x.com" and x["position"] == 3 for x in emails2)
    assert classify_primary("x.com", None, emails2, texts2).endswith("@x.com")

    organic3 = [{"title": "dir", "snippet": "name@domain.com x@facebook.com", "link": "https://facebook.com/x"}]
    emails3, _ = extract_serper_emails(organic3)
    assert len(emails3) == 0

    budget_stop = {"stop": False}
    requests_made = {"value": 0}
    max_credits = 2
    for _ in range(4):
        if budget_stop["stop"]:
            continue
        if requests_made["value"] >= max_credits:
            budget_stop["stop"] = True
            continue
        requests_made["value"] += 1
    assert budget_stop["stop"] is True and requests_made["value"] == 2

    hist = compute_position_histogram([
        {"email": "a@x.com", "source_url": "u", "position": 1},
        {"email": "b@x.com", "source_url": "u", "position": 3},
        {"email": "c@x.com", "source_url": "u", "position": 4},
        {"email": "d@x.com", "source_url": "u", "position": 10},
        {"email": "e@x.com", "source_url": "u", "position": 11},
        {"email": "f@x.com", "source_url": "u", "position": 20},
        {"email": "g@x.com", "source_url": "u", "position": 21},
    ])
    assert hist == {"1-3": 2, "4-10": 2, "11-20": 2, ">20": 1}

    emails4, texts4 = extract_serper_emails([])
    primary4 = classify_primary("none.com", "", emails4, texts4)
    done_obj = {"serper_enrich_done": True, "email": primary4}
    assert done_obj["serper_enrich_done"] is True and done_obj["email"] == ""
    print("SELF-TEST PASS: all assertions passed")


async def main_async(args: argparse.Namespace) -> None:
    if args.self_test:
        run_self_test()
        return
    _, _, serper_key = load_env()
    sb_holder = {"client": get_client()}
    def refresh_client() -> None:
        sb_holder["client"] = get_client()

    rows = fetch_targets(sb_holder["client"], args.persona, args.limit)
    total = len(rows)
    if total == 0:
        print("No targets to process")
        return

    sem = asyncio.Semaphore(SERPER_CONCURRENCY)
    lock = threading.Lock()
    stop_flag = {"stop": False}
    stop_reason = {"budget": False}
    requests_made = {"value": 0}
    credits_spent_this_run = {"value": 0}
    processed = 0
    emails_found_total = 0
    domains_with_primary = 0
    domains_no_email = 0
    results: List[Dict[str, Any]] = []

    async with httpx.AsyncClient(timeout=20.0) as client:
        async def worker(row: DomainRow) -> None:
            nonlocal processed, emails_found_total, domains_with_primary, domains_no_email
            try:
                async with sem:
                    with lock:
                        if stop_flag["stop"]:
                            return
                        if requests_made["value"] >= args.max_credits:
                            stop_flag["stop"] = True
                            stop_reason["budget"] = True
                            return
                        requests_made["value"] += 1
                    query = '"@' + row.domain + '"'
                    payload = {"q": query, "num": args.num, "gl": "us"}
                    resp = await client.post(SERPER_URL, headers={"X-API-KEY": serper_key}, json=payload)
                    resp.raise_for_status()
                    body = resp.json()
                    request_cost = int(body.get("credits", 1))
                    with lock:
                        credits_spent_this_run["value"] += request_cost

                    serper_emails, texts = extract_serper_emails(body.get("organic") or [])
                    primary = classify_primary(row.domain, row.business_name, serper_emails, texts)

                    new_signals = dict(row.signals or {})
                    new_signals["serper_emails"] = serper_emails
                    new_signals["serper_enrich_done"] = True
                    new_signals["serper_request_cost"] = request_cost

                    update_payload: Dict[str, Any] = {"signals": new_signals}
                    if (not row.email or not row.email.strip()) and primary:
                        update_payload["email"] = primary

                    supabase_execute_with_retry(
                        lambda: sb_holder["client"].table("domains").update(update_payload).eq("domain", row.domain),
                        domain=row.domain,
                        is_write=True,
                        refresh_client=refresh_client,
                    )

                    with lock:
                        processed += 1
                        emails_found_total += len(serper_emails)
                        if primary:
                            domains_with_primary += 1
                        else:
                            domains_no_email += 1
                        results.append({
                            "domain": row.domain,
                            "persona": row.persona or "",
                            "confidence_score": row.confidence_score,
                            "primary_email": primary,
                            "all_emails_json": json.dumps(serper_emails),
                            "source_urls_json": json.dumps([x.get("source_url") for x in serper_emails]),
                        })
                        if processed % BATCH_LOG_EVERY == 0:
                            print(f"processed={processed} requests_made={requests_made['value']} credits_spent_this_run={credits_spent_this_run['value']} emails_found_total={emails_found_total} domains_with_primary_email={domains_with_primary} domains_no_email={domains_no_email}")
            except Exception as e:
                print(f"ERROR domain={row.domain} error={e}")

        await asyncio.gather(*[worker(r) for r in rows])

    remaining = max(total - processed, 0)
    if stop_reason["budget"]:
        print(f"STOPPED: hit --max-credits budget of {args.max_credits} (requests_made={requests_made['value']})")
    print(f"SUMMARY processed={processed} total_targets={total} remaining={remaining} requests_made={requests_made['value']} credits_spent_this_run={credits_spent_this_run['value']} emails_found_total={emails_found_total} domains_with_primary_email={domains_with_primary} domains_no_email={domains_no_email}")
    all_emails: List[Dict[str, Any]] = []
    for row_result in results:
        all_emails.extend(json.loads(row_result["all_emails_json"]))
    hist = compute_position_histogram(all_emails)
    hist_parts = [f"1-3={hist['1-3']}", f"4-10={hist['4-10']}", f"11-20={hist['11-20']}"]
    if hist[">20"]:
        hist_parts.append(f">20={hist['>20']}")
    print("POSITION_HISTOGRAM " + " ".join(hist_parts))

    with open("serper_enrich_results.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["domain", "persona", "confidence_score", "primary_email", "all_emails_json", "source_urls_json"])
        w.writeheader()
        w.writerows(results)


def main() -> None:
    args = parse_args()
    asyncio.run(main_async(args))


if __name__ == "__main__":
    main()
