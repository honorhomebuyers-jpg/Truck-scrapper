#!/usr/bin/env python3
"""Scrape availability, occupancy, pricing, and booking activity for Truck
Parking Club listings.

Reads listing page URLs from urls.txt (plus whole-city market sweeps from
markets.txt), queries the site's public listing API (the same one the listing
page itself calls), and records the results:

  data/history.csv         — one row per listing per run, appended over time
  data/latest.json         — most recent snapshot per listing, overwritten each run
  data/listings/<slug>.csv — per-listing history, one file per URL, rebuilt
                             from history.csv every run (so they always match)
  data/reviews.csv         — append-only ledger of customer reviews; each review
                             is tied to a booking_id, so this is dated proof of
                             individual bookings
  data/events.csv          — append-only change log between runs: availability
                             drops (spots booked), rises (bookings ended),
                             price/status/booking-count changes
  data/markets/<slug>.csv  — whole-city sweep, one row per listing per run,
                             for market-wide occupancy

Uses only the Python standard library. Run: python3 scrape.py
"""

import csv
import json
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

API_BASE = "https://api-prod.truckparkingclub.com/api/v1/user/listing/public"
SITE_BASE = "https://truckparkingclub.com/truck-parking"

# The API rejects requests without browser-identifying headers (403).
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json",
    "Origin": "https://truckparkingclub.com",
    "Referer": "https://truckparkingclub.com/",
}

# plan_duration values observed in the API's `prices` array.
# Verified against the listing page: 1000/plan 2 = $10/day, 5000/plan 3 =
# $50/wk, 19000/plan 4 = $190/mo. Prices are in cents.
PLAN_DURATIONS = {1: "hourly", 2: "daily", 3: "weekly", 4: "monthly"}

# vehicle_type_allowed codes, verified against listing titles: listings with
# [0] are titled "Truck and Trailer Parking", listings with [1] are titled
# "Bobtail and Box Truck Parking".
VEHICLE_TYPES = {0: "Truck + Trailer", 1: "Bobtail / Box Truck"}

URL_RE = re.compile(
    r"truckparkingclub\.com/truck-parking/(?P<state>[^/]+)/(?P<city>[^/]+)/(?P<details>[^/?#]+)"
)

CSV_COLUMNS = [
    "scraped_at_utc",
    "url",
    "listing_id",
    "title",
    "full_address",
    "city",
    "state",
    "zip_code",
    "lat",
    "lng",
    "status",
    "vehicle_type",
    "total_available",
    "total_spaces_manual",
    "capacity_est",
    "occupied_est",
    "occupancy_pct_est",
    "booking_amount_label",
    "plans_offered",
    "reviews_count",
    "review_rating",
    "latest_review_date",
    "est_created_date",
    "available_seats_raw",
    "price_hourly_usd",
    "price_daily_usd",
    "price_weekly_usd",
    "price_monthly_usd",
    "prices_raw",
    "error",
]

# The trimmed, human-friendly columns written to the per-listing tables that
# feed the Google Sheet tabs. history.csv always keeps every column.
LISTING_COLUMNS = [
    "scraped_at_utc",
    "full_address",
    "vehicle_type",
    "total_available",
    "total_spaces_manual",
    "capacity_est",
    "occupied_est",
    "occupancy_pct_est",
    "booking_amount_label",
    "price_daily_usd",
    "price_weekly_usd",
    "price_monthly_usd",
    "reviews_count",
    "review_rating",
]

REVIEW_COLUMNS = [
    "first_seen_utc",
    "url",
    "listing_id",
    "booking_id",
    "review_date_utc",
    "rating",
    "reviewer",
    "reviewer_booking_count",
    "comment",
    "owner_reply",
    "owner_reply_date",
]

EVENT_COLUMNS = [
    "scraped_at_utc",
    "url",
    "metric",
    "previous",
    "current",
    "change",
    "note",
]

MARKET_COLUMNS = [
    "scraped_at_utc",
    "state_slug",
    "city_slug",
    "listing_id",
    "details_slug",
    "url",
    "status",
    "vehicle_type",
    "total_available",
    "price_hourly_usd",
    "price_daily_usd",
    "price_weekly_usd",
    "price_monthly_usd",
]

# Metrics in latest.json compared run-over-run to produce events.csv rows.
EVENT_METRICS = [
    "total_available",
    "booking_amount_label",
    "status",
    "price_hourly_usd",
    "price_daily_usd",
    "price_weekly_usd",
    "price_monthly_usd",
]


def read_urls(path: Path) -> list[str]:
    urls = []
    for line in path.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            urls.append(line)
    return urls


def read_markets(path: Path) -> list[tuple[str, str]]:
    """Read markets.txt lines of the form `<state-slug>/<city-slug>`."""
    markets = []
    if not path.exists():
        return markets
    for line in path.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "/" in line:
            state_slug, city_slug = line.split("/", 1)
            markets.append((state_slug.strip(), city_slug.strip()))
    return markets


def api_get(params: dict) -> dict:
    query = urllib.parse.urlencode(params)
    req = urllib.request.Request(f"{API_BASE}?{query}", headers=HEADERS)
    with urllib.request.urlopen(req, timeout=30) as res:
        return json.load(res)


def fetch_listing(url: str, retries: int = 3) -> dict:
    """Fetch the full API payload for one listing (listing + reviews + extras)."""
    m = URL_RE.search(url)
    if not m:
        raise ValueError(f"URL does not look like a listing page: {url}")
    params = {
        "state": m["state"],
        "city": m["city"],
        "details": m["details"],
        "page_size": 1,
        # Reviews are tied to booking_ids — dated proof of real bookings —
        # so pull the whole review feed every run.
        "review_limit": 100,
    }
    last_err: Exception = RuntimeError("unreachable")
    for attempt in range(retries):
        try:
            payload = api_get(params)
            if not (payload.get("data") or []):
                raise ValueError("API returned no listing for this URL")
            return payload
        except (urllib.error.URLError, ValueError, json.JSONDecodeError) as err:
            last_err = err
            if attempt < retries - 1:
                time.sleep(2 ** (attempt + 1))
    raise last_err


def fetch_market(state_slug: str, city_slug: str, retries: int = 3) -> list[dict]:
    """Fetch every listing in a city (no reviews, compact) for the market sweep."""
    listings: list[dict] = []
    seen_ids: set = set()
    page = 1
    while True:
        params = {
            "state": state_slug,
            "city": city_slug,
            "page_size": 100,
            "review_limit": 0,
            "page": page,
        }
        last_err: Exception = RuntimeError("unreachable")
        payload = None
        for attempt in range(retries):
            try:
                payload = api_get(params)
                break
            except (urllib.error.URLError, ValueError, json.JSONDecodeError) as err:
                last_err = err
                if attempt < retries - 1:
                    time.sleep(2 ** (attempt + 1))
        if payload is None:
            raise last_err
        batch = payload.get("data") or []
        new = [l for l in batch if l.get("listing_id") not in seen_ids]
        if not new:
            break
        listings.extend(new)
        seen_ids.update(l.get("listing_id") for l in new)
        if len(listings) >= int(payload.get("total") or 0):
            break
        page += 1
    return listings


def listing_slug(url: str) -> str:
    """Filename-safe identifier for a listing URL, e.g. 14015-florida-blvd-70819."""
    m = URL_RE.search(url)
    raw = f"{m['city']}-{m['details']}" if m else url
    return re.sub(r"[^A-Za-z0-9_-]+", "-", raw).strip("-").lower()


def listing_url(listing: dict) -> str:
    return (
        f"{SITE_BASE}/{listing.get('state_slug')}/{listing.get('city_slug')}/"
        f"{listing.get('details_slug')}"
    )


def total_seats(listing: dict) -> int:
    return sum(g.get("seats", 0) for g in listing.get("available_seats") or [])


def vehicle_type_str(listing: dict) -> str:
    return " & ".join(
        VEHICLE_TYPES.get(v, f"type {v}")
        for v in listing.get("vehicle_type_allowed") or []
    )


def price_columns(listing: dict) -> dict[str, str]:
    """Lowest advertised price per plan duration, in USD, as price_*_usd columns.

    A listing that allows both vehicle types can carry two prices per duration
    (one per vehicle type); the lowest is recorded and prices_raw keeps all.
    """
    cols = {f"price_{name}_usd": "" for name in PLAN_DURATIONS.values()}
    best: dict[str, int] = {}
    for price in listing.get("prices") or []:
        name = PLAN_DURATIONS.get(price.get("plan_duration"))
        if name and price.get("price") is not None:
            if name not in best or price["price"] < best[name]:
                best[name] = price["price"]
    for name, cents in best.items():
        cols[f"price_{name}_usd"] = f"{cents / 100:.2f}"
    return cols


def est_created_date(listing: dict) -> str:
    """Estimated listing creation date (YYYY-MM-DD), from its photos.

    The API exposes no created-at field, but image filenames are epoch-ms
    upload timestamps and owners upload photos when creating a listing, so
    the earliest photo approximates the creation date. Sequential listing_ids
    corroborate the ordering. Can drift later if an owner replaces all the
    original photos.
    """
    stamps = []
    for img in listing.get("images") or []:
        name = img.rsplit("/", 1)[-1].split(".")[0]
        if name.isdigit() and len(name) == 13:
            stamps.append(int(name) / 1000)
    if not stamps:
        return ""
    return datetime.fromtimestamp(min(stamps), timezone.utc).strftime("%Y-%m-%d")


def plans_offered(listing: dict) -> str:
    """Which booking types the listing sells, e.g. `daily, weekly, monthly`."""
    durations = {
        p.get("plan_duration")
        for p in listing.get("prices") or []
        if p.get("price") is not None
    }
    return ", ".join(
        name for code, name in sorted(PLAN_DURATIONS.items()) if code in durations
    )


def write_per_listing_files(history_path: Path, listings_dir: Path) -> int:
    """Rebuild one CSV per listing from history.csv so each URL has its own table."""
    by_url: dict[str, list[dict]] = {}
    with history_path.open(newline="") as f:
        for row in csv.DictReader(f):
            if row.get("url"):
                # Rows scraped before the vehicle_type column existed: the
                # listing titles state the type, so recover it from there.
                if not row.get("vehicle_type"):
                    title = row.get("title") or ""
                    if "Truck and Trailer" in title:
                        row["vehicle_type"] = VEHICLE_TYPES[0]
                    elif "Bobtail and Box Truck" in title:
                        row["vehicle_type"] = VEHICLE_TYPES[1]
                by_url.setdefault(row["url"], []).append(row)
    listings_dir.mkdir(exist_ok=True)
    for url, rows in by_url.items():
        with (listings_dir / f"{listing_slug(url)}.csv").open("w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=LISTING_COLUMNS, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(rows)
    return len(by_url)


def migrate_history_schema(history_path: Path) -> None:
    """Rewrite history.csv in the current column layout if it predates it.

    Rows keep their values by column name; columns added since are blank.
    Keeps appends aligned when the schema gains a column (e.g. vehicle_type).
    """
    if not history_path.exists():
        return
    with history_path.open(newline="") as f:
        reader = csv.DictReader(f)
        if reader.fieldnames == CSV_COLUMNS:
            return
        rows = list(reader)
    with history_path.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_COLUMNS, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({col: row.get(col, "") for col in CSV_COLUMNS})


def load_manual_spaces(history_path: Path) -> dict[str, str]:
    """Latest manually-entered total_spaces_manual per URL from history.csv.

    The column is meant to be filled in by hand; each run carries the most
    recent non-empty value forward into new rows so it only needs typing once.
    """
    manual: dict[str, str] = {}
    if not history_path.exists():
        return manual
    with history_path.open(newline="") as f:
        for row in csv.DictReader(f):
            value = (row.get("total_spaces_manual") or "").strip()
            if value and row.get("url"):
                manual[row["url"]] = value
    return manual


def load_observed_max(history_path: Path) -> dict[str, int]:
    """Highest total_available ever seen per URL — a hard floor on capacity.

    A lot can never show more open spots than it has, so the max availability
    observed across all runs is a defensible minimum capacity even when nobody
    has typed total_spaces_manual in yet.
    """
    observed: dict[str, int] = {}
    if not history_path.exists():
        return observed
    with history_path.open(newline="") as f:
        for row in csv.DictReader(f):
            value = (row.get("total_available") or "").strip()
            if value.isdigit() and row.get("url"):
                observed[row["url"]] = max(observed.get(row["url"], 0), int(value))
    return observed


def load_review_keys(reviews_path: Path) -> set[tuple[str, str]]:
    keys: set[tuple[str, str]] = set()
    if not reviews_path.exists():
        return keys
    with reviews_path.open(newline="") as f:
        for row in csv.DictReader(f):
            keys.add((row.get("listing_id", ""), row.get("booking_id", "")))
    return keys


def new_review_rows(
    url: str, listing_id, payload: dict, seen: set[tuple[str, str]], scraped_at: str
) -> list[dict]:
    """Reviews not yet in the ledger. Each review carries the booking_id it came
    from, so every row is dated evidence of one completed booking."""
    rows = []
    summary = payload.get("summary") or {}
    for rv in summary.get("reviews") or []:
        key = (str(listing_id), str(rv.get("booking_id", "")))
        if key in seen:
            continue
        seen.add(key)
        rows.append(
            {
                "first_seen_utc": scraped_at,
                "url": url,
                "listing_id": listing_id,
                "booking_id": rv.get("booking_id"),
                "review_date_utc": rv.get("customer_review_date"),
                "rating": rv.get("customer_rating"),
                "reviewer": rv.get("customer_review_by"),
                "reviewer_booking_count": rv.get("booking_count"),
                "comment": rv.get("customer_comment"),
                "owner_reply": rv.get("owner_reply"),
                "owner_reply_date": rv.get("owner_reply_date"),
            }
        )
    return rows


def append_csv(path: Path, columns: list[str], rows: list[dict]) -> None:
    if not rows:
        return
    write_header = not path.exists()
    with path.open("a", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=columns, extrasaction="ignore")
        if write_header:
            writer.writeheader()
        writer.writerows(rows)


def change_events(prev_latest: dict, rows: list[dict], scraped_at: str) -> list[dict]:
    """Diff this run against the previous latest.json snapshot.

    An availability drop means spots were booked since the last run; a rise
    means bookings ended. Price, status, and booking-count changes are logged
    too, so the events file is a full audit trail of what moved and when.
    """
    events = []
    for row in rows:
        prev = prev_latest.get(row["url"])
        if not prev or row.get("error") or prev.get("error"):
            continue
        for metric in EVENT_METRICS:
            old, new = str(prev.get(metric, "")), str(row.get(metric, ""))
            if old == new:
                continue
            change, note = "", ""
            if metric == "total_available" and old.lstrip("-").isdigit() and new.lstrip("-").isdigit():
                delta = int(new) - int(old)
                change = str(delta)
                if delta < 0:
                    note = f"{-delta} spot(s) newly occupied (booked) since last run"
                else:
                    note = f"{delta} spot(s) freed (booking ended) since last run"
            elif metric == "booking_amount_label":
                note = "listing's total-bookings badge changed"
            elif metric == "status":
                note = "listing status changed"
            events.append(
                {
                    "scraped_at_utc": scraped_at,
                    "url": row["url"],
                    "metric": metric,
                    "previous": old,
                    "current": new,
                    "change": change,
                    "note": note,
                }
            )
    return events


def market_rows(
    state_slug: str, city_slug: str, listings: list[dict], scraped_at: str
) -> list[dict]:
    rows = []
    for listing in listings:
        row = {
            "scraped_at_utc": scraped_at,
            "state_slug": state_slug,
            "city_slug": city_slug,
            "listing_id": listing.get("listing_id"),
            "details_slug": listing.get("details_slug"),
            "url": listing_url(listing),
            "status": listing.get("status"),
            "vehicle_type": vehicle_type_str(listing),
            "total_available": total_seats(listing),
        }
        row.update(price_columns(listing))
        rows.append(row)
    return rows


def append_discovered_urls(urls_path: Path, urls: list[str], new_urls: list[str]) -> None:
    """Add listings found by the market sweep but missing from urls.txt, so
    every listing in a tracked market gets full detail tracking automatically."""
    if not new_urls:
        return
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    with urls_path.open("a") as f:
        f.write(f"\n# auto-discovered {today} by market sweep\n")
        for url in new_urls:
            f.write(url + "\n")
    urls.extend(new_urls)


def to_row(
    url: str, payload: dict, scraped_at: str, manual: str, observed_max: int
) -> dict:
    listing = payload["data"][0]
    total_available = total_seats(listing)

    # Capacity: trust the hand-entered number when present, but never let the
    # estimate fall below availability we have actually observed.
    cap_candidates = [total_available, observed_max]
    if manual.strip().isdigit():
        cap_candidates.append(int(manual.strip()))
    capacity = max(cap_candidates)
    occupied = capacity - total_available
    occupancy_pct = f"{occupied / capacity * 100:.1f}" if capacity else ""

    summary = payload.get("summary") or {}
    review_dates = [
        rv.get("customer_review_date") or "" for rv in summary.get("reviews") or []
    ]

    row = {
        "scraped_at_utc": scraped_at,
        "url": url,
        "listing_id": listing.get("listing_id"),
        "title": listing.get("title"),
        "full_address": listing.get("full_address"),
        "city": listing.get("city"),
        "state": listing.get("state"),
        "zip_code": listing.get("zip_code"),
        "lat": listing.get("lat"),
        "lng": listing.get("lng"),
        "status": listing.get("status"),
        "vehicle_type": vehicle_type_str(listing),
        "total_available": total_available,
        "total_spaces_manual": manual,
        "capacity_est": capacity if capacity else "",
        "occupied_est": occupied if capacity else "",
        "occupancy_pct_est": occupancy_pct,
        "booking_amount_label": payload.get("booking_amount_label") or "",
        "plans_offered": plans_offered(listing),
        "reviews_count": listing.get("reviews_count"),
        "review_rating": listing.get("average_ratings"),
        "latest_review_date": max(review_dates) if review_dates else "",
        "est_created_date": est_created_date(listing),
        "available_seats_raw": json.dumps(
            listing.get("available_seats") or [], separators=(",", ":")
        ),
        "prices_raw": json.dumps(listing.get("prices") or [], separators=(",", ":")),
        "error": "",
    }
    row.update(price_columns(listing))
    return row


def error_row(url: str, err: Exception, scraped_at: str) -> dict:
    row = {col: "" for col in CSV_COLUMNS}
    row.update(scraped_at_utc=scraped_at, url=url, error=f"{type(err).__name__}: {err}")
    return row


def main() -> int:
    base = Path(__file__).resolve().parent
    urls_path = base / "urls.txt"
    urls = read_urls(urls_path)
    markets = read_markets(base / "markets.txt")
    if not urls and not markets:
        print("No URLs configured in urls.txt", file=sys.stderr)
        return 1

    data_dir = base / "data"
    data_dir.mkdir(exist_ok=True)
    history_path = data_dir / "history.csv"
    latest_path = data_dir / "latest.json"
    reviews_path = data_dir / "reviews.csv"
    events_path = data_dir / "events.csv"
    markets_dir = data_dir / "markets"

    scraped_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    # Market sweeps first: whole-city occupancy plus discovery of listings
    # that are not in urls.txt yet.
    for state_slug, city_slug in markets:
        try:
            listings = fetch_market(state_slug, city_slug)
        except Exception as err:  # noqa: BLE001 — a down market must not stop the run
            print(f"FAIL  market {state_slug}/{city_slug}: {err}", file=sys.stderr)
            continue
        markets_dir.mkdir(exist_ok=True)
        market_path = markets_dir / f"{state_slug}-{city_slug}.csv"
        append_csv(
            market_path, MARKET_COLUMNS, market_rows(state_slug, city_slug, listings, scraped_at)
        )
        discovered = [
            listing_url(l)
            for l in listings
            if l.get("details_slug") and listing_url(l) not in urls
        ]
        append_discovered_urls(urls_path, urls, discovered)
        print(
            f"market {state_slug}/{city_slug}: {len(listings)} listings"
            + (f", {len(discovered)} newly discovered" if discovered else "")
        )
        time.sleep(1)

    prev_latest: dict = {}
    if latest_path.exists():
        try:
            prev_latest = json.loads(latest_path.read_text())
        except json.JSONDecodeError:
            prev_latest = {}

    migrate_history_schema(history_path)
    manual_spaces = load_manual_spaces(history_path)
    observed_max = load_observed_max(history_path)
    seen_reviews = load_review_keys(reviews_path)

    rows, review_rows, failures = [], [], 0
    for url in urls:
        manual = manual_spaces.get(url, "")
        try:
            payload = fetch_listing(url)
            row = to_row(url, payload, scraped_at, manual, observed_max.get(url, 0))
            review_rows.extend(
                new_review_rows(url, row["listing_id"], payload, seen_reviews, scraped_at)
            )
            rows.append(row)
            print(f"ok    {url}")
        except Exception as err:  # noqa: BLE001 — record and keep going
            failures += 1
            row = error_row(url, err, scraped_at)
            row["total_spaces_manual"] = manual
            rows.append(row)
            print(f"FAIL  {url}: {err}", file=sys.stderr)
        time.sleep(1)  # be polite between listings

    append_csv(history_path, CSV_COLUMNS, rows)
    append_csv(reviews_path, REVIEW_COLUMNS, review_rows)

    events = change_events(prev_latest, rows, scraped_at)
    append_csv(events_path, EVENT_COLUMNS, events)

    latest_path.write_text(json.dumps({r["url"]: r for r in rows}, indent=2) + "\n")

    n_files = write_per_listing_files(history_path, data_dir / "listings")

    print(f"\n{len(rows) - failures}/{len(rows)} listings scraped -> {history_path}")
    print(f"{n_files} per-listing tables -> {data_dir / 'listings'}/")
    if review_rows:
        print(f"{len(review_rows)} new booking review(s) -> {reviews_path}")
    if events:
        print(f"{len(events)} change event(s) -> {events_path}")
    return 1 if rows and failures == len(rows) else 0


if __name__ == "__main__":
    sys.exit(main())
