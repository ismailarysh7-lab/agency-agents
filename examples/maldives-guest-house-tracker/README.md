# Maldives Guest House Tracker → sukunmaldives.com

Files that go with [`specialized/maldives-guest-house-tracker.md`](../../specialized/maldives-guest-house-tracker.md).

| File | What it is |
|---|---|
| `sukun-maldives-guest-houses-template.xlsx` | The workbook: daily prices and availability, plus the monthly `Room_Types` sheet |
| `sync_to_lovable.py` | Checks the workbook and turns it into SQL for the site's (Lovable / Supabase) database |

## Who does what

| Task | How often | Who |
|---|---|---|
| Site features (new pages, fields, upload screens) | one time | Lovable (uses Lovable credits) |
| Prices and availability per night → `guesthouse_daily_rates` | daily, changed nights only | Claude: Excel → `sync_to_lovable.py daily` → Lovable `query_database` |
| Room categories, views, guests, rooms, price (MVR), photos → `market_units` | monthly | Claude: Excel → `sync_to_lovable.py monthly` → Lovable `query_database` |
| Guest house details (name, island, links) | once, then only when they change | You, on the `Properties` sheet |

Writing data with `query_database` changes rows in the site's database directly. It does not ask Lovable's AI to do anything.

## Claude's routine

1. Get the latest site state: run the SQL from `python3 sync_to_lovable.py site-query` with `query_database`, then save the `site` value as `site.json`.
2. Generate the SQL:
   - daily: `python3 sync_to_lovable.py daily  WORKBOOK.xlsx --site site.json --out out/`
   - monthly: `python3 sync_to_lovable.py monthly WORKBOOK.xlsx --site site.json --out out/`
3. If the report says `fix_needed`, stop and show the user the errors and any unmatched guest houses. Some listings exist more than once (for example drafts), so a match is never guessed. The user confirms the right listing, and that `property_id → listing_id` pair goes into `site.json` → `mappings`.
4. Otherwise, run each `out/*.sql` file with `query_database`, in order, and add up the returned counts.
5. Report back: nights added and changed, rooms added and updated, rooms skipped for photos.

## Rules the script enforces (same as the site)

- Guest houses and homestays only. Rows still marked EXAMPLE, or showing "Unknown ID", are refused.
- `available` = Yes if either platform is open, No only if both are sold out, otherwise No data.
- A night that isn't available carries no price, rooms or cheapest platform. Links must be HTTPS.
- Daily: a night is written only when its values changed, and never over newer data.
- Monthly: room category is one of Standard Room, Deluxe Room, Beach View Room, Family Room or Suite. Views come from Sea View, Beach View, City View, Garden View or Pool View. Price is in MVR per night.
- Photos are merged and never removed. Photos already on the website count toward the minimum. A room with fewer than 5 photos in total is skipped and reported.
- Room types missing from the file are never deleted.
