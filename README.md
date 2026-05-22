# carrot-prospector

Discovery spike for validating two assumptions using only public data.

## What this does

- `detect.py`: probes domains with one HTTP GET and flags whether the `server` response header contains `carrot`.
- `ct_probe.py`: collects potential `*.oncarrot.com` hostnames from certificate transparency sources.

## Data sources and use policy

This project uses **public data only**:

- Certificate Transparency logs (`crt.sh` and certSpotter issuances API)
- Public HTTP response headers

Intended use is **B2B competitive research / lead generation only**.

Requirements for ethical/compliant use:

- Outreach must go from a **separate dedicated/warmed sending domain**.
- Honor **opt-outs** and comply with **CAN-SPAM**.
- Respect `robots.txt` and service **rate limits**.
- **Do NOT cold-text scraped phone numbers**.

## Requirements

- Python 3.11+
- `httpx`
- `tldextract`

Install:

```bash
python -m pip install httpx tldextract
```

## Usage

```bash
python detect.py raqhomes.com example.com
python ct_probe.py
```
