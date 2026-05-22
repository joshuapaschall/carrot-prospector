#!/usr/bin/env python3
import csv
import json
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

import requests
from bs4 import BeautifulSoup

SEEDS_FILE = Path("seeds.txt")
SOURCE_MAP_FILE = Path("seed_sources.json")
OUT_CSV = Path("carrot_leads.csv")
MAX_WORKERS = 5
REQUEST_TIMEOUT = 15

PHONE_RE = re.compile(r"(?:\+?1[-.\s]?)?\(?\d{3}\)?[-.\s]?\d{3}[-.\s]?\d{4}")
EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")


def load_seeds() -> list[str]:
    if not SEEDS_FILE.exists():
        return []
    return [line.strip().lower() for line in SEEDS_FILE.read_text(encoding="utf-8").splitlines() if line.strip()]


def load_source_map() -> dict[str, str]:
    if not SOURCE_MAP_FILE.exists():
        return {}
    try:
        data = json.loads(SOURCE_MAP_FILE.read_text(encoding="utf-8"))
        return {k.lower(): str(v) for k, v in data.items()}
    except Exception:
        return {}


def fetch_site(domain: str):
    url = f"https://{domain}"
    try:
        resp = requests.get(url, timeout=REQUEST_TIMEOUT, headers={"User-Agent": "Mozilla/5.0"}, allow_redirects=True)
        if resp.status_code != 200:
            return None
        return resp
    except Exception:
        return None


def extract_contact(resp: requests.Response):
    soup = BeautifulSoup(resp.text, "html.parser")

    business_name = ""
    if soup.title and soup.title.text:
        business_name = soup.title.text.strip()

    phone = ""
    email = ""
    city = ""
    state = ""

    for script in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(script.string or "{}")
            objs = data if isinstance(data, list) else [data]
            for obj in objs:
                if not isinstance(obj, dict):
                    continue
                if not business_name:
                    business_name = str(obj.get("name", "")).strip()
                phone = phone or str(obj.get("telephone", "")).strip()
                email = email or str(obj.get("email", "")).strip()
                addr = obj.get("address", {})
                if isinstance(addr, dict):
                    city = city or str(addr.get("addressLocality", "")).strip()
                    state = state or str(addr.get("addressRegion", "")).strip()
        except Exception:
            continue

    if not phone:
        tel = soup.select_one('a[href^="tel:"]')
        if tel:
            phone = tel.get("href", "").replace("tel:", "").strip()
    if not email:
        mailto = soup.select_one('a[href^="mailto:"]')
        if mailto:
            email = mailto.get("href", "").replace("mailto:", "").split("?")[0].strip()

    text = soup.get_text(" ", strip=True)
    if not phone:
        m = PHONE_RE.search(text)
        if m:
            phone = m.group(0)
    if not email:
        m = EMAIL_RE.search(text)
        if m:
            email = m.group(0)

    return business_name, phone, email, city, state


def process_domain(domain: str, source_map: dict[str, str]):
    time.sleep(2)
    resp = fetch_site(domain)
    if not resp:
        return None

    server_hdr = (resp.headers.get("server") or "").lower()
    is_carrot = "carrot" in server_hdr
    business_name, phone, email, city, state = extract_contact(resp)

    return {
        "domain": domain,
        "business_name": business_name,
        "phone": phone,
        "email": email,
        "city": city,
        "state": state,
        "is_carrot": str(is_carrot),
        "source": source_map.get(domain, "SERP"),
        "detected_at": datetime.now(timezone.utc).isoformat(),
    }


def main():
    seeds = load_seeds()
    if not seeds:
        print("No seeds found in seeds.txt")
        return

    source_map = load_source_map()
    rows = []

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
        futures = {ex.submit(process_domain, d, source_map): d for d in seeds}
        for fut in as_completed(futures):
            row = fut.result()
            if row:
                rows.append(row)

    rows.sort(key=lambda r: r["domain"])
    with OUT_CSV.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=["domain", "business_name", "phone", "email", "city", "state", "is_carrot", "source", "detected_at"],
        )
        writer.writeheader()
        writer.writerows(rows)

    print(f"Saved {len(rows)} leads to {OUT_CSV}")


if __name__ == "__main__":
    main()
