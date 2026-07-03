# Truck Parking Club scraper

Periodically records **availability, occupancy, pricing, booking activity, and
location** for Truck Parking Club listings — per listing and per market
(whole city).

## How it works

The listing pages themselves are behind a Cloudflare browser challenge, but the
data on them comes from a public JSON API
(`api-prod.truckparkingclub.com/api/v1/user/listing/public`) that accepts plain
requests with browser-like headers. `scrape.py` converts each listing URL into
an API query, so no headless browser is needed.

- `urls.txt` — the listing URLs to track (one per line, `#` for comments)
- `markets.txt` — whole cities to sweep (`<state-slug>/<city-slug>` per line)
- `scrape.py` — the scraper (Python 3, standard library only)
- `data/history.csv` — one row per listing per run, appended forever
- `data/latest.json` — the most recent snapshot per listing
- `data/listings/<slug>.csv` — per-listing table, rebuilt from history each run
- `data/reviews.csv` — append-only booking-review ledger (proof of bookings)
- `data/events.csv` — append-only change log between runs
- `data/markets/<slug>.csv` — every listing in a swept city, per run

## What gets recorded

Per listing per run: UTC timestamp, **listing URL**, listing id, title, **full
address**, city, state, zip, latitude/longitude, listing status, **total
available spots**, **estimated capacity / occupied spots / occupancy %**,
the listing's **total-bookings badge** (`booking_amount_label`, e.g. `100+`),
which **booking plans it sells** (`plans_offered`: hourly / daily / weekly /
monthly), **number of reviews**, **review rating**, date of the newest review,
the raw per-vehicle-type availability, and **hourly / daily / weekly / monthly
prices in USD** (lowest across vehicle types when a listing prices both; the
raw prices array keeps every price — prices come from the API in cents).
Failed fetches are recorded with an `error` column so gaps are visible.

### Occupancy: `capacity_est`, `occupied_est`, `occupancy_pct_est`

The API only exposes *available* spots, not lot capacity, so capacity is
estimated as the larger of:

- the hand-entered `total_spaces_manual` value (see below), and
- the **highest availability ever observed** for that listing — a lot can
  never show more open spots than it has, so this is a hard floor.

`occupied_est = capacity_est − total_available` and `occupancy_pct_est` follow
from that. Until a manual capacity is entered, treat these as **lower bounds**:
if a lot has never been seen empty, part of its capacity stays invisible.
Entering `total_spaces_manual` once per listing makes the numbers exact.

### Proof of bookings: `data/reviews.csv`

Every customer review on a listing is tied to a `booking_id`, so the review
feed is dated evidence of individual completed bookings. Each run pulls the
full review feed and appends any reviews not seen before (deduplicated by
listing + booking id), with the review date, rating, reviewer name, the
reviewer's own booking count, comment, and owner reply. The
`booking_amount_label` column in history.csv tracks the listing's public
total-bookings badge over time.

### Booking activity between runs: `data/events.csv`

After each run the new snapshot is diffed against the previous one. Any change
in availability (**a drop = spots booked, a rise = bookings ended**), price,
listing status, or the bookings badge is appended as one event row with the
previous value, the new value, and the delta. Comparing how long a spot stays
occupied across events indicates the booking type (an overnight disappearance
suggests a daily booking; weeks-long suggests weekly/monthly).

### Market sweeps: `markets.txt` → `data/markets/`

Each city in `markets.txt` is queried in full every run, recording status,
availability, vehicle type, and prices for **every listing in that market** —
this is the market-wide occupancy picture, and it also catches competitors
raising or dropping prices. Any swept listing missing from `urls.txt` is
**auto-appended** there (with an `# auto-discovered` comment), so new supply
in a tracked market starts getting full detail tracking on the same run.

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

`.github/workflows/scrape-parking.yml` runs the scraper **every 2 hours** on
GitHub Actions and commits any new data back to the repository. Edit the `cron`
line to change the frequency. The tighter the cadence, the fewer short
(hourly/daily) bookings can start *and* end unseen between snapshots.

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

To track a whole city, add `<state-slug>/<city-slug>` to `markets.txt`
instead — its listings are auto-added to `urls.txt` on the next run.
