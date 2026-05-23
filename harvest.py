#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx
import tldextract
from dotenv import load_dotenv
from supabase import Client, create_client

NEGATIVE_KEYWORDS_FILE = Path("negative_keywords.txt")

HARDCODED_EXCLUSIONS = {
    "zillow","redfin","trulia","realtor","homes.com","opendoor","offerpad","homevestors","homelight","movoto",
    "loopnet","biggerpockets","foreclosure","auction","bankowned","fanniemae","freddiemac","youtube","facebook",
    "instagram","twitter","tiktok","linkedin","yelp","bbb.org","wikipedia","reddit","quora","pinterest",
    "nextdoor","craigslist","angieslist","thumbtack","homeadvisor","angi","apartmentlist","apartments","rent",
    "hud","gov",".edu","bankofamerica","wellsfargo","chase","google","bing","yahoo","msn"
}
KNOWN_SITE_NAMES = {
    "zillow","redfin","trulia","realtor","homes","opendoor","offerpad","homevestors","homelight","movoto",
    "loopnet","biggerpockets","youtube","facebook","instagram","twitter","tiktok","linkedin","yelp","wikipedia",
    "reddit","quora","pinterest","nextdoor","craigslist","angieslist","thumbtack","homeadvisor","angi",
    "apartmentlist","apartments","bankofamerica","wellsfargo","chase","google","bing","yahoo","msn","hud","gov"
}


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def make_supabase() -> Client:
    load_dotenv()
    url = os.getenv("SUPABASE_URL", "").strip()
    key = os.getenv("SUPABASE_SERVICE_ROLE_KEY", "").strip()
    if not url or not key:
        raise RuntimeError("Missing SUPABASE_URL or SUPABASE_SERVICE_ROLE_KEY in .env")
    return create_client(url, key)


def get_serper_key() -> str:
    key = os.getenv("SERPER_API_KEY", "").strip()
    if not key:
        raise RuntimeError("Missing SERPER_API_KEY in .env")
    return key


def normalize_domain(v: str) -> str | None:
    c = (v or "").strip().lower()
    if not c:
        return None
    if "://" not in c:
        c = "http://" + c
    p = urlparse(c)
    host = p.netloc or p.path.split("/")[0]
    d = tldextract.extract(host).top_domain_under_public_suffix
    return d.lower() if d else None


def load_exclusions() -> set[str]:
    exclusions = {x.lower() for x in HARDCODED_EXCLUSIONS}
    if NEGATIVE_KEYWORDS_FILE.exists():
        for line in NEGATIVE_KEYWORDS_FILE.read_text(encoding="utf-8").splitlines():
            t = line.strip().lower()
            if t and ("." in t or t in KNOWN_SITE_NAMES):
                exclusions.add(t)
    return exclusions


def is_excluded(domain: str, exclusions: set[str]) -> bool:
    return any(token in domain for token in exclusions)


def ensure_running_job(sb: Client) -> dict:
    existing = sb.table("jobs").select("*").eq("kind", "harvest").eq("status", "running").limit(1).execute().data or []
    total = int((sb.table("queries").select("id", count="exact").in_("status", ["pending", "done"]).limit(1).execute().count) or 0)
    if existing:
        job = existing[0]
        sb.table("jobs").update({"queries_total": total, "updated_at": now_iso()}).eq("id", job["id"]).execute()
        job["queries_total"] = total
        return job

    created = sb.table("jobs").insert({"kind": "harvest", "status": "running", "queries_total": total, "updated_at": now_iso()}).execute()
    return created.data[0]


def fetch_pending(sb: Client, limit: int) -> list[dict]:
    return sb.table("queries").select("id,query_text,layer").eq("status", "pending").limit(limit).execute().data or []


async def serper_search(client: httpx.AsyncClient, api_key: str, query_text: str) -> list[str]:
    r = await client.post(
        "https://google.serper.dev/search",
        headers={"X-API-KEY": api_key, "Content-Type": "application/json"},
        json={"q": query_text, "num": 10, "gl": "us", "hl": "en"},
        timeout=30.0,
    )
    r.raise_for_status()
    payload = r.json()
    organic = payload.get("organic") or []
    links: list[str] = []
    for item in organic:
        link = item.get("link")
        if link:
            links.append(link)
    return links


async def process_one(item: dict[str, Any], client: httpx.AsyncClient, sem: asyncio.Semaphore, api_key: str, exclusions: set[str]) -> dict:
    async with sem:
        query_id = item["id"]
        q = item["query_text"]
        layer = item.get("layer") or "keyword"
        try:
            links = await serper_search(client, api_key, q)
            domains = []
            seen = set()
            for link in links:
                domain = normalize_domain(link)
                if not domain or is_excluded(domain, exclusions) or domain in seen:
                    continue
                seen.add(domain)
                domains.append(domain)
            return {"id": query_id, "layer": layer, "domains": domains, "error": None}
        except Exception as exc:
            return {"id": query_id, "layer": layer, "domains": [], "error": str(exc)}


def update_job_progress(sb: Client, job_id: Any, done: int, domains: int, credits: int, status: str = "running") -> None:
    sb.table("jobs").update(
        {"status": status, "queries_done": done, "domains_found": domains, "credits_used": credits, "updated_at": now_iso()}
    ).eq("id", job_id).execute()


def mark_query_done(sb: Client, query_id: Any, results_count: int, new_domains: int) -> None:
    sb.table("queries").update(
        {
            "status": "done",
            "results_count": results_count,
            "new_domains": new_domains,
            "credits_used": 1,
            "processed_at": now_iso(),
            "error": None,
        }
    ).eq("id", query_id).execute()


def mark_query_failed(sb: Client, query_id: Any, error: str) -> None:
    sb.table("queries").update({"status": "failed", "error": error, "processed_at": now_iso()}).eq("id", query_id).execute()


async def run(limit: int | None) -> None:
    sb = make_supabase()
    api_key = get_serper_key()
    exclusions = load_exclusions()
    job = ensure_running_job(sb)
    job_id = job["id"]

    done_count = int((sb.table("queries").select("id", count="exact").eq("status", "done").limit(1).execute().count) or 0)
    domains_inserted_total = int(job.get("domains_found") or 0)
    credits_used_total = int(job.get("credits_used") or 0)
    total_queries = int(job.get("queries_total") or 0)

    sem = asyncio.Semaphore(12)
    remaining = limit

    async with httpx.AsyncClient() as client:
        try:
            while True:
                if remaining is not None and remaining <= 0:
                    break
                batch_size = 100 if remaining is None else min(100, remaining)
                batch = fetch_pending(sb, batch_size)
                if not batch:
                    break

                results = await asyncio.gather(*[process_one(row, client, sem, api_key, exclusions) for row in batch])

                for r in results:
                    if r["error"]:
                        mark_query_failed(sb, r["id"], r["error"])
                        continue

                    new_domain_count = 0
                    for domain in r["domains"]:
                        resp = sb.table("domains").upsert(
                            {"domain": domain, "source": r["layer"], "status": "new"},
                            on_conflict="domain",
                            ignore_duplicates=True,
                        ).execute()
                        if resp.data:
                            new_domain_count += 1

                    mark_query_done(sb, r["id"], results_count=len(r["domains"]), new_domains=new_domain_count)
                    domains_inserted_total += new_domain_count
                    credits_used_total += 1
                    done_count += 1

                if remaining is not None:
                    remaining -= len(batch)

                update_job_progress(sb, job_id, done_count, domains_inserted_total, credits_used_total, status="running")
                print(f"harvest: {done_count}/{total_queries} queries, {domains_inserted_total} domains, {credits_used_total} credits used")

        except KeyboardInterrupt:
            update_job_progress(sb, job_id, done_count, domains_inserted_total, credits_used_total, status="paused")
            print("Paused. Re-run harvest.py to resume.")
            return

    pending_left = int((sb.table("queries").select("id", count="exact").eq("status", "pending").limit(1).execute().count) or 0)
    if pending_left == 0 and (limit is None or remaining == 0):
        sb.table("jobs").update({"status": "complete", "finished_at": now_iso(), "updated_at": now_iso()}).eq("id", job_id).execute()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=None, help="Process only first N pending queries")
    args = parser.parse_args()
    asyncio.run(run(args.limit))


if __name__ == "__main__":
    main()
