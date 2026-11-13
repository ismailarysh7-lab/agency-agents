# Maldives Guest House Tracker → sukunmaldives.com

Files that go with [`specialized/maldives-guest-house-tracker.md`](../../specialized/maldives-guest-house-tracker.md).

| File | What it is |
|---|---|
| `sukun-maldives-guest-houses-template.xlsx` | The workbook: daily prices and availability, plus the monthly `Room_Types` sheet |
| `sync_to_lovable.py` | Checks the workbook and turns it into SQL for the site's (Lovable / Supabase) database |
| `photo_review.py` | Photo clean-up: downloads guest house and room photos into numbered contact sheets for Claude to review, then applies a new order (best cover first) and removes photos with people, keeping a backup on each listing so it can be undone |

## Who does what

| Task | How often | Who |
|---|---|---|
| Site features (new pages, fields, upload screens) | one time | Lovable (uses Lovable credits) |
| Prices and availability per night → `guesthouse_daily_rates` | daily, changed nights only | Claude: Excel → `sync_to_lovable.py daily` → Lovable `query_database` |
| Room categories, views, guests, rooms, price (MVR), photos → `market_units` | monthly | Claude: Excel → `sync_to_lovable.py monthly` → Lovable `query_database` |
| Phone, WhatsApp and email shown on the website → `market_listings.details` | only when they change, as a separate upload | Claude: Excel `Contacts` sheet → `sync_to_lovable.py contacts` → Lovable `query_database` |
| Guest house details (name, island, links) | once, then only when they change | You, on the `Properties` sheet |

Writing data with `query_database` changes rows in the site's database directly. It does not ask Lovable's AI to do anything.

## Claude's routine

1. Get the latest site state: run the SQL from `python3 sync_to_lovable.py site-query` with `query_database`, then save the `site` value as `site.json`.
2. Generate the SQL:
   - daily: `python3 sync_to_lovable.py daily  WORKBOOK.xlsx --site site.json --out out/`
   - monthly: `python3 sync_to_lovable.py monthly WORKBOOK.xlsx --site site.json --out out/`
   - contacts: `python3 sync_to_lovable.py contacts WORKBOOK.xlsx --site site.json --out out/`
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
- Contacts: numbers are tidied to `+960 7771234` (other countries keep their `+` code), and email must look valid. A blank cell never erases what the site has.
- WhatsApp works with no site change. The site's call/WhatsApp logic reads `details.contact`: the first number is the Call number, and the number after the word "WhatsApp" is the WhatsApp number. So a different WhatsApp is written as `+960 7771234 · WhatsApp +960 9601234`, and also stored in `details.whatsapp`.
- Exception: about 15 guest houses in the site's built-in directory (`src/lib/directory-data.ts`) take their WhatsApp button from that directory. For those, the uploader reports that only phone and email change on screen.

The agent page (claude.ai artifact) does the same checks and publishing in the browser: load the workbook, check, confirm, publish.
