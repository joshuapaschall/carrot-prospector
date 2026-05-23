#!/usr/bin/env python3
import argparse
import csv
import os
from pathlib import Path
from typing import Dict, Iterable, List, Set, Tuple

import tldextract
from dotenv import load_dotenv
from supabase import Client, create_client

EXCLUDED_DOMAINS = {
    "carrot.com",
    "oncarrot.com",
    "leadconnectorhq.com",
    "msgsndr.com",
    "resimpliwebsites.com",
    "resimpli.com",
    "grumpyhare.com",
    "leadpropeller.com",
    "shared.leadpropeller.com",
    "reileadz.com",
    "publicwww.com",
    "facebook.com",
    "twitter.com",
    "x.com",
    "instagram.com",
    "linkedin.com",
    "youtube.com",
    "google.com",
}

PAGE_SIZE = 1000
INSERT_BATCH_SIZE = 500


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Import PublicWWW domains CSV into Supabase domains table")
    parser.add_argument("--file", required=True, help="Path to PublicWWW cluster-export CSV (no header)")
    parser.add_argument("--dry-run", action="store_true", help="Parse and print counts without writing to Supabase")
    return parser.parse_args()


def get_client() -> Client:
    load_dotenv()
    url = os.getenv("SUPABASE_URL")
    key = os.getenv("SUPABASE_SERVICE_ROLE_KEY")
    if not url or not key:
        raise RuntimeError("Missing SUPABASE_URL or SUPABASE_SERVICE_ROLE_KEY in environment/.env")
    return create_client(url, key)


def normalize_domain(raw: str) -> str:
    value = (raw or "").strip().lower()
    if not value:
        return ""
    extracted = tldextract.extract(value)
    if not extracted.domain or not extracted.suffix:
        return ""
    return f"{extracted.domain}.{extracted.suffix}".lower()


def should_skip_domain(domain: str) -> bool:
    return domain in EXCLUDED_DOMAINS


def load_domains_from_csv(file_path: Path) -> Tuple[int, int, List[str]]:
    parsed_rows = 0
    skipped_rows = 0
    discovered: Set[str] = set()

    with file_path.open("r", encoding="utf-8", newline="") as handle:
        reader = csv.reader(handle)
        for row in reader:
            parsed_rows += 1
            raw_domain = row[0] if row else ""
            domain = normalize_domain(raw_domain)
            if not domain or should_skip_domain(domain):
                skipped_rows += 1
                continue
            discovered.add(domain)

    return parsed_rows, skipped_rows, sorted(discovered)


def fetch_existing_domains(client: Client) -> Set[str]:
    all_domains: Set[str] = set()
    offset = 0

    while True:
        response = (
            client.table("domains")
            .select("domain")
            .range(offset, offset + PAGE_SIZE - 1)
            .execute()
        )
        rows = response.data or []
        if not rows:
            break

        for item in rows:
            domain = (item.get("domain") or "").strip().lower()
            if domain:
                all_domains.add(domain)

        if len(rows) < PAGE_SIZE:
            break
        offset += PAGE_SIZE

    return all_domains


def chunked(values: List[str], size: int) -> Iterable[List[str]]:
    for i in range(0, len(values), size):
        yield values[i : i + size]


def insert_new_domains(client: Client, domains: List[str]) -> int:
    inserted = 0
    for batch in chunked(domains, INSERT_BATCH_SIZE):
        payload = [
            {
                "domain": domain,
                "source": "publicwww",
                "signals": {"publicwww": True},
            }
            for domain in batch
        ]
        client.table("domains").insert(payload).execute()
        inserted += len(batch)
    return inserted


def append_source_for_existing(client: Client, domains: List[str]) -> int:
    updated = 0
    for domain in domains:
        existing_resp = client.table("domains").select("source").eq("domain", domain).limit(1).execute()
        rows = existing_resp.data or []
        if not rows:
            continue
        current_source = (rows[0].get("source") or "").strip()
        sources = [item.strip() for item in current_source.split(",") if item.strip()]
        if "publicwww" in sources:
            continue
        new_source = ",".join(sources + ["publicwww"]) if sources else "publicwww"
        client.table("domains").update({"source": new_source}).eq("domain", domain).execute()
        updated += 1
    return updated


def main() -> None:
    args = parse_args()
    file_path = Path(args.file)
    if not file_path.exists():
        raise FileNotFoundError(f"CSV file not found: {file_path}")

    rows_parsed, skipped, normalized_domains = load_domains_from_csv(file_path)

    if args.dry_run:
        print(f"rows parsed: {rows_parsed}")
        print(f"skipped (platform/empty): {skipped}")
        print(f"new inserted: 0")
        print(f"already existing: 0")
        return

    client = get_client()
    existing_domains = fetch_existing_domains(client)

    new_domains = [d for d in normalized_domains if d not in existing_domains]
    existing_hits = [d for d in normalized_domains if d in existing_domains]

    inserted_count = insert_new_domains(client, new_domains)
    updated_count = append_source_for_existing(client, existing_hits)

    print(f"rows parsed: {rows_parsed}")
    print(f"skipped (platform/empty): {skipped}")
    print(f"new inserted: {inserted_count}")
    print(f"already existing: {updated_count}")


if __name__ == "__main__":
    main()
