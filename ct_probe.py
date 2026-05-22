#!/usr/bin/env python3
import json
import re
import time
from typing import Iterable, Set
from urllib import error, request

CRT_URL = "https://crt.sh/?q=%25.oncarrot.com&output=json"
CERTSPOTTER_URL = (
    "https://api.certspotter.com/v1/issuances"
    "?domain=oncarrot.com&include_subdomains=true&expand=dns_names"
)
USER_AGENT = "carrot-prospector/0.1"
MAX_RETRIES = 6
TIMEOUT = 25


def _get_json(url: str):
    req = request.Request(url, headers={"User-Agent": USER_AGENT}, method="GET")
    with request.urlopen(req, timeout=TIMEOUT) as resp:
        return resp.getcode(), json.loads(resp.read().decode("utf-8"))


def normalize_hostname(name: str) -> str:
    return name.strip().lower().rstrip(".")


def extract_oncarrot_hosts(candidates: Iterable[str]) -> Set[str]:
    hosts: Set[str] = set()
    for raw in candidates:
        name = normalize_hostname(raw)
        if not name or name.startswith("*.") or name == "oncarrot.com":
            continue
        if name.endswith(".oncarrot.com"):
            hosts.add(name)
    return hosts


def fetch_crt_sh_with_retries():
    last_error = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            _, data = _get_json(CRT_URL)
            return data
        except error.HTTPError as exc:
            last_error = exc
            if exc.code != 502 or attempt == MAX_RETRIES:
                break
            delay = 2 ** (attempt + 1)
            print(f"crt.sh attempt {attempt}/{MAX_RETRIES} failed (HTTP 502); retrying in {delay}s...")
            time.sleep(delay)
        except Exception as exc:
            last_error = exc
            if attempt == MAX_RETRIES:
                break
            delay = 2 ** (attempt + 1)
            print(f"crt.sh attempt {attempt}/{MAX_RETRIES} failed ({exc}); retrying in {delay}s...")
            time.sleep(delay)
    raise RuntimeError(f"crt.sh failed after {MAX_RETRIES} attempts. Last error: {last_error}")


def fetch_certspotter():
    try:
        _, data = _get_json(CERTSPOTTER_URL)
        return data
    except error.HTTPError as exc:
        if exc.code == 429:
            print("certSpotter rate-limited (429); continuing with whatever data we already have.")
            return []
        raise


def classify_slug_scheme(hosts: Set[str]) -> str:
    wordish = 0
    randomish = 0
    for host in hosts:
        slug = host[: -len(".oncarrot.com")].split(".")[-1]
        cleaned = slug.replace("-", "")
        has_letters = bool(re.search(r"[a-z]", cleaned))
        has_digits = bool(re.search(r"\d", cleaned))
        if re.fullmatch(r"[a-z0-9]{10,}", cleaned) and has_letters and has_digits:
            randomish += 1
        elif has_letters and not has_digits:
            wordish += 1
        else:
            wordish += 1
    return (
        "Guess: labels skew toward random alphanumeric IDs."
        if randomish > wordish
        else "Guess: labels skew toward business names/words."
    )


def main() -> int:
    hosts: Set[str] = set()
    try:
        crt_rows = fetch_crt_sh_with_retries()
        names = []
        for row in crt_rows:
            names.extend((row.get("name_value") or "").splitlines())
            if row.get("common_name"):
                names.append(row["common_name"])
        hosts |= extract_oncarrot_hosts(names)
    except RuntimeError as exc:
        print(exc)

    try:
        cert_rows = fetch_certspotter()
        names = []
        for row in cert_rows:
            names.extend(row.get("dns_names", []))
        hosts |= extract_oncarrot_hosts(names)
    except Exception as exc:
        print(f"certSpotter request failed: {exc}")

    sample = sorted(hosts)[:30]
    print(f"total unique subdomains found: {len(hosts)}")
    print("sample (30 sorted):")
    for h in sample:
        print(h)
    print(classify_slug_scheme(hosts) if hosts else "No subdomains to classify.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
