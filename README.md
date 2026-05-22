# carrot-prospector

Discovery and lead-enrichment pipeline for real estate wholesaler websites using public web data.

## Full pipeline

1. `keywords.txt` (4,934 search queries) + `negative_keywords.txt` (3,729 exclusions)
2. `python3 seed_harvest.py`
   - Pulls candidate domains from:
     - DuckDuckGo SERPs (primary)
     - Carrot showcase/review pages
     - Facebook Ad Library (optional)
   - Writes:
     - `seeds.txt` (final filtered domains)
     - `seed_sources.json` (domain → source labels)
     - `serp_progress.txt` (resume ledger)
     - `seeds_partial.txt` (safety checkpoints)
3. `python3 run_pipeline.py`
   - Reads `seeds.txt`
   - Verifies domains by HTTP 200 response
   - Detects whether each domain appears to be Carrot-hosted (`is_carrot`)
   - Scrapes contact details
   - Writes `carrot_leads.csv`

## Resume capability

`seed_harvest.py` is resumable:

- On startup, it loads completed queries from `serp_progress.txt`.
- Re-running the script skips already-completed query lines and continues from where it left off.
- On interruption (`Ctrl+C`), it prints:
  - `Interrupted. Progress saved. Re-run to resume.`
  - and preserves checkpoint data in `seeds_partial.txt`.

## Facebook token setup (optional)

Set an access token before running harvest if you want Facebook Ad Library source data:

```bash
export FB_ACCESS_TOKEN='your-token-here'
python3 seed_harvest.py
```

If `FB_ACCESS_TOKEN` is missing, Facebook harvesting is skipped automatically.

## Output schema (`carrot_leads.csv`)

Columns:

- `domain`
- `business_name`
- `phone`
- `email`
- `city`
- `state`
- `is_carrot`
- `source`
- `detected_at`

## Compliance / responsible use

- Use a **separate dedicated/warmed sending domain** for outreach.
- Honor opt-outs and comply with **CAN-SPAM**.
- Respect `robots.txt` and service **rate limits**.
- **Do NOT cold-text scraped phone numbers**.

## Database setup

1. Create a **new Supabase project** for this prospecting database. Do **not** reuse any production project.
2. Open the Supabase **SQL Editor**.
3. Paste the full contents of `db/schema.sql`.
4. Run the SQL to create tables, views, enums, indexes, triggers, and RLS settings.

### Key auth note

The worker and UI server-side code must use the **service_role** key. Never use the **anon** key server-side, because RLS is enabled and anon requests will silently return zero rows.
