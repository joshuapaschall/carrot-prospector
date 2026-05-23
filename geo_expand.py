#!/usr/bin/env python3
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Dict, List, Tuple

from dotenv import load_dotenv
from supabase import Client, create_client

KEYWORDS_FILE = Path("keywords.txt")
CITIES_FILE = Path("data/usaCities.json")
STATES_FILE = Path("data/states_json.json")
BATCH_SIZE = 500

CITY_TEMPLATES = [
    "we buy houses {city}",
    "sell my house fast {city}",
    "cash home buyers {city}",
    "cash for houses {city}",
    "we buy ugly houses {city}",
    "{city} real estate investors",
    "sell house fast cash {city}",
]

STATE_TEMPLATES = [
    "we buy houses {state}",
    "cash home buyers {state}",
    "sell my house fast {state}",
]


def make_supabase() -> Client:
    load_dotenv()
    url = os.getenv("SUPABASE_URL", "").strip()
    key = os.getenv("SUPABASE_SERVICE_ROLE_KEY", "").strip()
    if not url or not key:
        raise RuntimeError("Missing SUPABASE_URL or SUPABASE_SERVICE_ROLE_KEY in .env")
    return create_client(url, key)


def load_keywords() -> List[str]:
    return [line.strip().lower() for line in KEYWORDS_FILE.read_text(encoding="utf-8").splitlines() if line.strip()]


def load_cities() -> List[dict]:
    return json.loads(CITIES_FILE.read_text(encoding="utf-8"))


def load_states() -> Dict[str, str]:
    return json.loads(STATES_FILE.read_text(encoding="utf-8"))


def build_rows() -> Tuple[List[dict], Dict[str, int], int]:
    layer_counts = {"keyword": 0, "city_template": 0, "state_template": 0}
    raw_total = 0
    seen: set[str] = set()
    rows: List[dict] = []

    for kw in load_keywords():
        raw_total += 1
        layer_counts["keyword"] += 1
        if kw not in seen:
            seen.add(kw)
            rows.append({"query_text": kw, "layer": "keyword", "status": "pending"})

    for city_obj in load_cities():
        city = (city_obj.get("city") or "").strip()
        state = (city_obj.get("state") or "").strip()
        locality = f"{city} {state}".strip().lower()
        if not locality:
            continue
        for template in CITY_TEMPLATES:
            text = template.format(city=locality).strip().lower()
            raw_total += 1
            layer_counts["city_template"] += 1
            if text not in seen:
                seen.add(text)
                rows.append({"query_text": text, "layer": "city_template", "status": "pending"})

    for _, state_name in load_states().items():
        state = (state_name or "").strip().lower()
        if not state:
            continue
        for template in STATE_TEMPLATES:
            text = template.format(state=state).strip().lower()
            raw_total += 1
            layer_counts["state_template"] += 1
            if text not in seen:
                seen.add(text)
                rows.append({"query_text": text, "layer": "state_template", "status": "pending"})

    return rows, layer_counts, raw_total


def chunked(items: List[dict], size: int):
    for i in range(0, len(items), size):
        yield items[i : i + size]


def count_pending(sb: Client) -> int:
    resp = sb.table("queries").select("id", count="exact").eq("status", "pending").limit(1).execute()
    return int(resp.count or 0)


def main() -> None:
    rows, layer_counts, raw_total = build_rows()
    deduped_total = len(rows)

    sb = make_supabase()
    inserted_total = 0
    for batch in chunked(rows, BATCH_SIZE):
        result = (
            sb.table("queries")
            .upsert(batch, on_conflict="query_text", ignore_duplicates=True)
            .execute()
        )
        inserted_total += len(result.data or [])

    already_existing = deduped_total - inserted_total
    pending_total = count_pending(sb)

    print("geo_expand summary")
    print(f"generated_raw_total={raw_total}")
    print(f"keyword_count={layer_counts['keyword']}")
    print(f"city_template_count={layer_counts['city_template']}")
    print(f"state_template_count={layer_counts['state_template']}")
    print(f"deduped_total={deduped_total}")
    print(f"newly_inserted={inserted_total}")
    print(f"already_existing={already_existing}")
    print(f"pending_total_now={pending_total}")


if __name__ == "__main__":
    main()
