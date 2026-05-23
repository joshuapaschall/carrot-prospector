#!/usr/bin/env python3
import argparse
import asyncio
import json
import os
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple

import httpx
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from supabase import Client, create_client

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)
TIMEOUT_SECONDS = 15.0
BATCH_SIZE = 100
MAX_CONCURRENCY = 25

WHOLESALER_PHRASES = [
    "we buy houses",
    "we buy homes",
    "sell my house fast",
    "sell your house fast",
    "cash offer",
    "cash for your home",
    "cash for your house",
    "cash for houses",
    "cash home buyer",
    "any condition",
    "as-is",
    "as is",
    "no realtor",
    "no commission",
    "fair cash offer",
    "we buy ugly houses",
    "sell as is",
    "stop foreclosure",
    "get a cash offer",
    "close in days",
    "we'll buy your house",
]
AGENT_PHRASES = [
    "list your home",
    "homes for sale",
    "mls",
    "real estate agent",
    "realtor",
    "licensed agent",
    "free home valuation",
    "what's my home worth",
    "comparative market analysis",
    "our listings",
    "view listings",
    "buyers and sellers",
    "find your dream home",
    "new listings",
    "property search",
    "keller williams",
    "re/max",
    "remax",
    "century 21",
    "coldwell banker",
    "exp realty",
    "sotheby",
    "berkshire hathaway",
    "howard hanna",
    "weichert",
]
INVESTOR_PHRASES = [
    "fix and flip",
    "fix & flip",
    "rental property",
    "rental properties",
    "real estate investing",
    "investment property",
    "buy and hold",
    "portfolio",
    "brrrr",
    "wholesale",
    "wholesaling",
    "off market",
    "off-market",
    "cash flow",
    "motivated sellers",
    "assignment fee",
    "double close",
]

JUNK_CATEGORIES = {
    "mortgage": ["mortgage rates", "refinance", "loan officer", "nmls", "mortgage calculator", "home loan", "apr "],
    "media_finance": [
        "advertiser disclosure",
        "editorial team",
        "forbes",
        "cnbc",
        "investopedia",
        "bankrate",
        "nerdwallet",
        "marketwatch",
        "motley fool",
    ],
    "directory": ["business directory", "find a business", "browse listings", "yellow pages", "yelp.com"],
    "ecommerce": ["add to cart", "shopping cart", "checkout", "shop now"],
}

REI_PLATFORMS = ["leadpropeller", "investorfuse", "reisift", "reiblackbook", "ballpointmarketing", "investorcarrot"]

PHONE_RE = re.compile(r"\(?\d{3}\)?[-.\s]?\d{3}[-.\s]?\d{4}")
EMAIL_RE = re.compile(r"[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}", re.IGNORECASE)


@dataclass
class FetchResult:
    status_code: Optional[int]
    html: Optional[str]
    headers: Dict[str, str]
    url: Optional[str]
    error: Optional[str]


def get_client() -> Client:
    load_dotenv()
    url = os.getenv("SUPABASE_URL")
    key = os.getenv("SUPABASE_SERVICE_ROLE_KEY")
    if not url or not key:
        raise RuntimeError("Missing SUPABASE_URL or SUPABASE_SERVICE_ROLE_KEY in environment/.env")
    return create_client(url, key)


def normalize_whitespace(value: str) -> str:
    return re.sub(r"\s+", " ", value or "").strip()


def extract_jsonld(soup: BeautifulSoup) -> List[Dict[str, Any]]:
    items: List[Dict[str, Any]] = []
    for tag in soup.find_all("script", attrs={"type": re.compile(r"ld\+json", re.IGNORECASE)}):
        raw = (tag.string or tag.get_text() or "").strip()
        if not raw:
            continue
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, list):
                items.extend([x for x in parsed if isinstance(x, dict)])
            elif isinstance(parsed, dict):
                if isinstance(parsed.get("@graph"), list):
                    items.extend([x for x in parsed["@graph"] if isinstance(x, dict)])
                else:
                    items.append(parsed)
        except json.JSONDecodeError:
            continue
    return items


def phrase_hits(text: str, phrases: Sequence[str]) -> Set[str]:
    return {p for p in phrases if p in text}


def detect_platform(html_lc: str, headers: Dict[str, str], is_carrot: bool) -> str:
    if is_carrot:
        return "carrot"
    if any(marker in html_lc for marker in REI_PLATFORMS):
        return "rei_platform"
    if "wp-content" in html_lc or "wordpress" in html_lc:
        return "wordpress"
    if "wix.com" in html_lc or "static.wixstatic.com" in html_lc:
        return "wix"
    if "squarespace" in html_lc:
        return "squarespace"
    return "unknown"


def has_lead_form(soup: BeautifulSoup, text_lc: str) -> bool:
    if any(token in text_lc for token in ["get my cash offer", "get your offer", "get a cash offer"]):
        return True
    for form in soup.find_all("form"):
        names = " ".join((inp.get("name") or "") + " " + (inp.get("id") or "") for inp in form.find_all(["input", "textarea", "select"]))
        names = names.lower()
        if "name" in names and ("phone" in names or "address" in names):
            return True
    return False


def pick_jsonld_value(schema_items: List[Dict[str, Any]], key: str, allowed_types: Optional[Set[str]] = None) -> Optional[str]:
    for item in schema_items:
        t = item.get("@type")
        types = {t.lower()} if isinstance(t, str) else {x.lower() for x in t} if isinstance(t, list) else set()
        if allowed_types and not (types & {x.lower() for x in allowed_types}):
            continue
        val = item.get(key)
        if isinstance(val, str) and val.strip():
            return normalize_whitespace(val)
    return None


def extract_city_state(schema_items: List[Dict[str, Any]]) -> Tuple[Optional[str], Optional[str]]:
    for item in schema_items:
        address = item.get("address")
        if isinstance(address, dict):
            city = normalize_whitespace(address.get("addressLocality", "")) or None
            state = normalize_whitespace(address.get("addressRegion", "")) or None
            if city or state:
                return city, state
    return None, None


def score_and_classify(domain: str, status_code: Optional[int], html: str, headers: Dict[str, str]) -> Dict[str, Any]:
    soup = BeautifulSoup(html, "html.parser")
    visible_text = normalize_whitespace(soup.get_text(" ", strip=True)).lower()
    html_lc = html.lower()
    combined = f"{visible_text} {html_lc}"

    server = (headers.get("server") or "").lower()
    carrot_html = "cdn.carrot.com" in html_lc or "image-cdn.carrot.com" in html_lc
    is_carrot = server == "carrot" or carrot_html

    wholesaler_hits = phrase_hits(visible_text, WHOLESALER_PHRASES)
    agent_hits = phrase_hits(visible_text, AGENT_PHRASES)
    investor_hits = phrase_hits(visible_text, INVESTOR_PHRASES)

    junk_category: Optional[str] = None
    junk_counts: Dict[str, int] = {}
    for category, phrases in JUNK_CATEGORIES.items():
        hits = len(phrase_hits(combined, phrases))
        junk_counts[category] = hits
        if hits >= 2 and junk_category is None:
            junk_category = category
    if domain.lower().endswith(".gov") or domain.lower().endswith(".edu"):
        junk_counts["gov_edu"] = 2
        junk_category = junk_category or "gov_edu"
    else:
        junk_counts["gov_edu"] = 0

    has_phone = bool(re.search(r"tel:\s*", html_lc) or PHONE_RE.search(combined))
    has_form = has_lead_form(soup, visible_text)

    schema_items = extract_jsonld(soup)
    has_schema = False
    for item in schema_items:
        t = item.get("@type")
        types = [t.lower()] if isinstance(t, str) else [x.lower() for x in t] if isinstance(t, list) else []
        if "localbusiness" in types or "realestateagent" in types:
            has_schema = True
            break

    platform = detect_platform(html_lc, headers, is_carrot)

    score = 0
    if is_carrot:
        score += 35
    elif platform == "rei_platform":
        score += 20
    score += min(len(wholesaler_hits) * 6, 36)
    score += min(len(investor_hits) * 5, 25)
    score += min(len(agent_hits) * 4, 20)
    if has_phone:
        score += 10
    if has_form:
        score += 12
    if has_schema:
        score += 8
    raw_score = max(0, min(score, 100))

    total_persona_hits = len(wholesaler_hits) + len(agent_hits) + len(investor_hits)
    hard_junk = any(count >= 2 for count in junk_counts.values()) and total_persona_hits < 2

    if hard_junk:
        persona = "junk"
        confidence = min(raw_score, 15)
    else:
        confidence = raw_score
        w = len(wholesaler_hits)
        a = len(agent_hits)
        i = len(investor_hits)
        if max(w, a, i) == 0:
            has_re_content = any(token in combined for token in ["real estate", "house", "home", "property"])
            persona = "unknown" if (has_re_content or has_phone) else "unknown"
        else:
            ranked = [(w, 3, "wholesaler"), (i, 2, "investor"), (a, 1, "agent_realtor")]
            persona = sorted(ranked, reverse=True)[0][2]

    qualified = (not hard_junk) and persona in {"wholesaler", "agent_realtor", "investor"} and confidence >= 35

    business_name = (
        pick_jsonld_value(schema_items, "name", {"Organization", "LocalBusiness", "RealEstateAgent"})
        or (soup.find("meta", property="og:site_name") or {}).get("content")
        or ""
    )
    if not business_name:
        title = soup.title.string if soup.title and soup.title.string else ""
        business_name = re.split(r"\s+[\-|\|]\s+", title)[0].strip() if title else None

    phone = pick_jsonld_value(schema_items, "telephone")
    if not phone:
        tel = soup.find("a", href=re.compile(r"^tel:", re.IGNORECASE))
        if tel and tel.get("href"):
            phone = tel.get("href").split(":", 1)[1].strip()
    if not phone:
        m = PHONE_RE.search(combined)
        phone = m.group(0) if m else None

    email = pick_jsonld_value(schema_items, "email")
    if not email:
        mailto = soup.find("a", href=re.compile(r"^mailto:", re.IGNORECASE))
        if mailto and mailto.get("href"):
            email = mailto.get("href").split(":", 1)[1].split("?")[0].strip()
    if not email:
        em = EMAIL_RE.search(combined)
        email = em.group(0) if em else None
    if email:
        bad_bits = ["example.com", "sentry", "wixpress", "godaddy", "your@email"]
        if any(x in email.lower() for x in bad_bits):
            email = None

    city, state = extract_city_state(schema_items)

    phrases_found = sorted(set(wholesaler_hits) | set(agent_hits) | set(investor_hits))
    return {
        "business_name": business_name or None,
        "phone": phone,
        "email": email,
        "city": city,
        "state": state,
        "is_carrot": is_carrot,
        "platform": platform,
        "confidence_score": confidence,
        "http_status": status_code,
        "qualified": qualified,
        "persona": persona,
        "product_fit": ["listhit", "sendtext"] if qualified else [],
        "signals": {
            "persona": persona,
            "wholesaler_count": len(wholesaler_hits),
            "agent_count": len(agent_hits),
            "investor_count": len(investor_hits),
            "junk_category": junk_category if hard_junk else None,
            "carrot": is_carrot,
            "platform": platform,
            "has_phone": has_phone,
            "has_form": has_form,
            "has_schema": has_schema,
            "raw_score": raw_score,
            "phrases_found": phrases_found,
        },
    }


async def fetch_homepage(client: httpx.AsyncClient, domain: str) -> FetchResult:
    for scheme in ("https://", "http://"):
        url = f"{scheme}{domain}"
        try:
            resp = await client.get(url)
            return FetchResult(resp.status_code, resp.text or "", {k.lower(): v for k, v in resp.headers.items()}, str(resp.url), None)
        except (httpx.TimeoutException, httpx.ConnectError, httpx.NetworkError):
            continue
        except httpx.HTTPError as exc:
            return FetchResult(None, None, {}, None, str(exc))
    return FetchResult(None, None, {}, None, "unreachable")


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


async def process_domain(sem: asyncio.Semaphore, client: httpx.AsyncClient, row: Dict[str, Any]) -> Dict[str, Any]:
    async with sem:
        domain = row["domain"]
        fetched = await fetch_homepage(client, domain)
        if fetched.html is None:
            return {
                "id": row["id"],
                "domain": domain,
                "update": {
                    "confidence_score": 0,
                    "http_status": None,
                    "qualified": False,
                    "persona": "unreachable",
                    "last_scraped_at": now_iso(),
                },
                "persona": "unreachable",
            }

        computed = score_and_classify(domain, fetched.status_code, fetched.html, fetched.headers)
        computed["last_scraped_at"] = now_iso()
        return {"id": row["id"], "domain": domain, "update": computed, "persona": computed["persona"]}


def upsert_job_running(sb: Client, total: int) -> str:
    payload = {"kind": "qualify", "status": "running", "queries_total": total, "queries_done": 0, "domains_qualified": 0}
    resp = sb.table("jobs").insert(payload).execute()
    return resp.data[0]["id"]


def update_job(sb: Client, job_id: str, fields: Dict[str, Any]) -> None:
    fields["updated_at"] = now_iso()
    sb.table("jobs").update(fields).eq("id", job_id).execute()


def get_batch(sb: Client, limit: int) -> List[Dict[str, Any]]:
    resp = sb.table("domains").select("id,domain").is_("last_scraped_at", "null").limit(limit).execute()
    return resp.data or []


def run_self_test() -> None:
    tests = [
        ("wholesaler.test", "<html><body>we buy houses we buy homes sell your house fast get a cash offer call 555-123-4567 <form><input name='name'/><input name='phone'/><input name='address'/></form></body></html>"),
        ("agent.test", "<html><body>homes for sale MLS free home valuation Keller Williams re/max list your home view listings property search call us 212-555-1212 <form><input name='name'/><input name='phone'/></form></body></html>"),
        ("junk.test", "<html><body>mortgage rates refinance NMLS licensed loan officer</body></html>"),
    ]
    for domain, html in tests:
        out = score_and_classify(domain, 200, html, {"server": "nginx"})
        print((out["persona"], out["confidence_score"], out["qualified"]))


async def run(limit: Optional[int]) -> None:
    sb = get_client()
    total_q = sb.table("domains").select("id", count="exact").is_("last_scraped_at", "null").execute().count or 0
    if limit is not None:
        total_q = min(total_q, limit)
    job_id = upsert_job_running(sb, total_q)

    done = 0
    qualified_total = 0
    persona_counts = {"wholesaler": 0, "agent_realtor": 0, "investor": 0}
    sem = asyncio.Semaphore(MAX_CONCURRENCY)

    try:
        async with httpx.AsyncClient(
            follow_redirects=True,
            timeout=TIMEOUT_SECONDS,
            headers={"User-Agent": USER_AGENT},
        ) as client:
            while True:
                remaining = None if limit is None else max(limit - done, 0)
                if remaining == 0:
                    break
                batch = get_batch(sb, min(BATCH_SIZE, remaining) if remaining is not None else BATCH_SIZE)
                if not batch:
                    break

                tasks = [process_domain(sem, client, row) for row in batch]
                results = await asyncio.gather(*tasks, return_exceptions=False)

                for result in results:
                    sb.table("domains").update(result["update"]).eq("id", result["id"]).execute()
                    done += 1
                    if result["update"].get("qualified"):
                        qualified_total += 1
                    persona = result["persona"]
                    if persona in persona_counts:
                        persona_counts[persona] += 1

                update_job(sb, job_id, {"queries_done": done, "domains_qualified": qualified_total, "status": "running"})
                print(
                    f"qualify: {done}/{total_q}, {qualified_total} qualified | "
                    f"w={persona_counts['wholesaler']} a={persona_counts['agent_realtor']} i={persona_counts['investor']}"
                )

        update_job(sb, job_id, {"status": "complete", "finished_at": now_iso(), "queries_done": done, "domains_qualified": qualified_total})
    except KeyboardInterrupt:
        update_job(sb, job_id, {"status": "paused", "queries_done": done, "domains_qualified": qualified_total})
        print("Paused. Resume by re-running qualify.py; it continues from rows with last_scraped_at IS NULL.")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Qualify and classify domains from Supabase")
    parser.add_argument("--limit", type=int, default=None, help="Maximum number of domains to process")
    parser.add_argument("--self-test", action="store_true", help="Run local dry scorer tests and exit")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.self_test:
        run_self_test()
        return 0
    asyncio.run(run(args.limit))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
