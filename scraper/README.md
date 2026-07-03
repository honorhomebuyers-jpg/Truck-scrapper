# Truck Parking Club scraper

Periodically records **availability, pricing, and location** for Truck Parking
Club listings.

## How it works

The listing pages themselves are behind a Cloudflare browser challenge, but the
data on them comes from a public JSON API
(`api-prod.truckparkingclub.com/api/v1/user/listing/public`) that accepts plain
requests with browser-like headers. `scrape.py` converts each listing URL into
an API query, so no headless browser is needed.

- `urls.txt` — the listing URLs to track (one per line, `#` for comments)
- `scrape.py` — the scraper (Python 3, standard library only)
- `data/history.csv` — one row per listing per run, appended forever
- `data/latest.json` — the most recent snapshot per listing

## What gets recorded

Per listing per run: UTC timestamp, **listing URL**, listing id, title, **full
address**, city, state, zip, latitude/longitude, listing status, **total
available spots**, **number of reviews** (`reviews_count`), **review rating**
(`review_rating`, average out of 5), the raw per-vehicle-type availability, and
**daily / weekly / monthly prices in USD** (plus the raw prices array; prices
come from the API in cents). Listings with no reviews yet leave the review
columns blank. Failed fetches are recorded with an `error` column so gaps are
visible.

### Manual `total_spaces_manual` column

Next to `total_available` there is a `total_spaces_manual` column for the
lot's total capacity, which the API doesn't expose. Type the number into that
column on any row for a listing (the latest row is easiest); every future run
copies the most recent value you entered forward automatically, so each
listing only needs it typed once (re-enter to change it).

## Running it

```bash
python3 scraper/scrape.py
```

## Scheduling

`.github/workflows/scrape-parking.yml` runs the scraper **every 6 hours** on
GitHub Actions and commits any new data back to the repository. Edit the `cron`
line to change the frequency.

Notes:

- GitHub only triggers `schedule` workflows from the **default branch**, so the
  schedule starts once this workflow lands there. You can also run it manually
  anytime from the Actions tab (`workflow_dispatch`).
- GitHub pauses scheduled workflows in repositories with no activity for 60
  days; the scraper's own commits normally count as activity.

## Adding listings

Append the listing's page URL to `urls.txt`, e.g.:

```
https://truckparkingclub.com/truck-parking/texas/houston/some-listing-slug
```

Any URL of the form
`truckparkingclub.com/truck-parking/<state>/<city>/<details-slug>` works.
