# Auction Scraper Healthchecker

Detects auction-site or data changes that may break the Sotheby's, Christie's, and Phillips scrapers.

The checker compares live pages and scraped JSON against the selectors, fields, URL formats, and image rules used by each scraper. When it finds a confirmed problem, it can send a Microsoft Teams alert and maintain an alert history in a local file or S3.

## Install

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt

# Required for --check-search (used playwright because of extensive tools)
python -m playwright install chromium
```

## Quick start

Run all commands from the project directory:

```bash
# Check known lots for one auction house
python healthcheck.py phillips --canary

# Test the Teams webhook
export TEAMS_WEBHOOK_URL="https://..."
python healthcheck.py phillips --teams-test
```

For repeatable monitoring, store state so the checker can confirm problems and recognize when they resolve:

```bash
python healthcheck.py phillips \
  --canary \
  --check-search \
  --state s3://art-dev-database/healthcheck/phillips/state.json \
  --report report.json
```

Use `sothebys`, `christies`, or `phillips` after `healthcheck.py`.

## Supported checks

| Check | What it verifies |
|---|---|
| `--canary` | 6 known past lots still contain the fields, HTML elements, JSON, and images the scraper reads |
| `--check-search` | A search page still exposes the scraper's result structure |
| `--check-pages` | A sample of scraped lot links still points to usable lot pages |
| `--live` | A sample of scraped image URLs still resolves to valid, non-placeholder images |
| `source.json` | A local or S3 artwork file has valid links and images |
| `--data-prefix` | Artist JSON files changed since the previous run remain healthy |

The checker uses two-pass filtering to avoid false alarms: online failures are grouped by failure type, and an online issue normally must repeat on two consecutive runs before it becomes an alert. Data-file failures are evaluated against each file's baseline and alerted immediately.

## examples

Check one artwork file:

```bash
python healthcheck.py phillips path/to/artworks.json \
  --check-pages --live --limit 100
```

Check all recently changed artist files and their next-stage images:

```bash
export AWS_PROFILE=compagnie

python healthcheck.py phillips \
  --data-prefix s3://art-dev-database/artworks_raw/artworks_raw_phillips/ \
  --compare-prefix s3://art-dev-database/artworks_processed/phillips/ \
  --max-files 100 \
  --state s3://art-dev-database/healthcheck/phillips/state.json
```

Run a broad daily check:

```bash
python healthcheck.py phillips \
  --canary --check-search \
  --data-prefix s3://art-dev-database/artworks_raw/artworks_raw_phillips/ \
  --check-pages --live --sample 30 \
  --state s3://art-dev-database/healthcheck/phillips/state.json \
  --report report.json
```

## Options

```text
source                 Local or s3:// JSON artwork file
--compare FILE         Later-stage copy of source, to verify that images survive
--data-prefix FOLDER   Check recently changed artist JSON files
--compare-prefix FOLDER
                       Later-stage folder, to verify that images survive
--max-files N          Maximum number of files to check

--canary               Check the 6 known lots
--check-search         Check the search page
--check-pages          Download a sample of lot pages
--live                 Download a sample of image URLs
--sample N             Number of online items to sample
--limit N              Limit a source file to its first N artworks

--state FILE           Local or s3:// alert history
--confirm-runs N       Consecutive runs required for an online alert (default: 2)
--cooldown-hours H     Minimum time between repeat Teams alerts (default: 12)
--report FILE          Write the full JSON report
--teams-webhook URL    Teams webhook (defaults to TEAMS_WEBHOOK_URL)
--teams-test           Send a test Teams card and exit

--delay SECONDS        Pause between requests (default: 0.5)
--min-side PIXELS      Low-resolution image threshold (default: 1024)
```

Run `python healthcheck.py <site> --help` for the authoritative option list.

## Reading a run

- **Canary status:** `OK`, `FAILED`, or `INCONCLUSIVE`
- **Signal:** a problem detected during the current run
- **Alert:** a confirmed signal that Teams or exit code `1` reports
- **Resolved:** a previously alerted problem that is no longer detected

Exit codes:

| Code | Meaning |
|---|---|
| `0` | No alert |
| `1` | One or more alerts |
| `2` | Invalid input or a file, S3, state, or configuration error |

The JSON report contains the checks run, canary and search results, data counts, signals, alerts, and limited error evidence.

## Teams

Set a webhook with either command-line or environment configuration:

```bash
export TEAMS_WEBHOOK_URL="https://..."
python healthcheck.py phillips --canary --teams-test
```

Create the webhook in Teams with the **Workflows** app, copy the channel's webhook link, and keep it out of source control. A successful run sends no message; new alerts, reminders, and resolved alerts are grouped into the run's Teams card. Sending failure leaves the alert pending for a later run.

## State and S3

Use a separate state file for each site. State is required for repeated-run confirmation, cooldowns, data baselines, and resolved-alert notifications:

```text
healthcheck/
  sothebys/state.json
  christies/state.json
  phillips/state.json
```

For S3 paths:

```bash
export AWS_PROFILE=compagnie
aws sts get-caller-identity
```

Without `--state`, online problems are treated as immediately alertable.

## Project structure

```text
healthcheck.py          CLI entry point
healthcheck_core.py     Shared checks, state, reporting, and Teams logic
sites/
  sothebys.py           Sotheby's scraper rules
  christies.py          Christie's scraper rules
  phillips.py           Phillips scraper rules
requirements.txt
```

## Maintenance

- **Replace a canary:** update `CANARY_LOTS` in `sites/<site>.py`. Use a past, sold lot with the fields and image the scraper requires.
- **Scraper changed:** update the matching site's selectors or format rules in `sites/<site>.py`.
- **Alert received:** inspect the report and example URLs, update the scraper, then rerun the check. It should report `Resolved` when the problem is gone.
- **Add a site:** create `sites/<name>.py`, implement the shared site hooks, and add the name to `SITES` in `healthcheck.py`.

## Current limitations

- Christie's `--check-search` is currently skipped until `SEARCH_CANARY_URL` is configured in `sites/christies.py`.
- Sotheby's and Christie's search checks require Playwright.
- Health history and alerts are separate from scraper execution: this repository does not modify scraped data.
