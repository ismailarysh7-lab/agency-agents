#!/usr/bin/env python3
"""Turn the Sukun Maldives Excel workbook into SQL for the sukunmaldives.com (Lovable) database.

Claude runs this, then executes each generated .sql file with the Lovable `query_database` tool.
It never talks to the network itself.

  daily   -> guesthouse_daily_rates (+ guesthouse_feed_properties mapping)
             source: All_Nights (full grid) if it has rows, else Website_Feed.
             Only nights whose values changed are written (IS DISTINCT FROM).
  contacts -> market_listings.details (contact, email, whatsapp) from the Contacts sheet.
             Separate from prices; blank cells never erase what the site has.
  monthly -> market_units (room category = name, view_types, capacity, quantity, price MVR,
             available, images). Photos are merged, never removed; rooms with < 5 photos in
             total are skipped and reported.

Usage:
  python3 sync_to_lovable.py daily   WORKBOOK.xlsx --site site.json --out DIR [--chunk 300]
  python3 sync_to_lovable.py monthly WORKBOOK.xlsx --site site.json --out DIR
  python3 sync_to_lovable.py contacts WORKBOOK.xlsx --site site.json --out DIR
  python3 sync_to_lovable.py site-query          # prints the SQL that produces site.json

site.json is the `site` value returned by the site-query, saved to a file.
Validation mirrors src/lib/guesthouse-feed.ts and src/lib/room-information.ts on the site.
"""
import argparse, datetime as dt, json, os, re, sys

import openpyxl

FEED_HEADERS = ["property_id", "guest_house_name", "island", "atoll", "website_slug", "date", "booking_status",
                "agoda_status", "available", "rooms_left", "lowest_price_per_night_usd", "cheapest_platform",
                "booking_url", "agoda_url", "last_updated"]
ROOM_HEADERS = ["property_id", "guest_house_name", "island", "room_category", "view_type_1", "view_type_2",
                "max_guests", "number_of_rooms", "price_mvr_per_night", "available", "valid_month"] + \
               [f"photo_{i}_url" for i in range(1, 9)] + ["photo_count", "photos_ok", "notes"]
STATUS = {"Yes", "No", "No data"}
PLATFORMS = {"Booking.com", "Agoda"}
ROOM_TYPES = ["Standard Room", "Deluxe Room", "Beach View Room", "Family Room", "Suite"]
ROOM_VIEWS = ["Sea View", "Beach View", "City View", "Garden View", "Pool View"]
CONTACT_HEADERS = ["property_id", "guest_house_name", "island", "phone", "whatsapp", "email", "contact_ok", "notes"]
MIN_ROOM_PHOTOS = 5
# Guest houses whose WhatsApp button comes from the site's built-in directory (src/lib/directory-data.ts),
# which wins over listing details until the site code changes. Phone and email still update.
DIRECTORY_WHATSAPP = {
    ("kudhi boli beach house", "thulusdhoo"), ("bibee surf thulusdhoo", "thulusdhoo"), ("golhaa view inn", "thulusdhoo"),
    ("dream inn thulusdhoo", "thulusdhoo"), ("whale shark beach", "dhigurah"), ("dhiguveli", "dhigurah"),
    ("akiri dhigurah", "dhigurah"), ("fulidhoo guesthouse", "fulidhoo"), ("ukulhas sands", "ukulhas"),
    ("island villa ukulhas", "ukulhas"), ("ras rana lodge", "rasdhoo"), ("acqua blu rasdhoo", "rasdhoo"),
    ("rv thoddoo maldives", "thoddoo"), ("amazing view thoddoo", "thoddoo"), ("chillax thoddoo", "thoddoo"),
    ("sunrise stay maldives", "hulhumalé"),
}
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[A-Za-z]{2,}$")
MAX_ROWS_PER_UPLOAD = 2000

SITE_QUERY = """SELECT json_build_object(
 'listings', (SELECT coalesce(json_agg(json_build_object('id', id, 'title', title, 'island', island, 'status', status, 'details', coalesce(details, '{}'::jsonb))), '[]')
              FROM market_listings WHERE category::text IN ('guesthouse','homestay')),
 'mappings', (SELECT coalesce(json_object_agg(property_id, listing_id), '{}') FROM guesthouse_feed_properties),
 'units',    (SELECT coalesce(json_agg(json_build_object('id', u.id, 'listing_id', u.listing_id, 'name', u.name, 'images', u.images, 'image_url', u.image_url)), '[]')
              FROM market_units u JOIN market_listings l ON l.id = u.listing_id WHERE l.category::text IN ('guesthouse','homestay'))
) AS site;"""


def text(v):
    return "" if v is None else str(v).strip()


def norm(v):
    return text(v).lower()


def q(v):
    """SQL literal (text or NULL)."""
    if v is None or v == "":
        return "NULL"
    return "'" + str(v).replace("'", "''") + "'"


def q_array(values):
    return "ARRAY[" + ",".join(q(v) for v in values) + "]::text[]" if values else "'{}'::text[]"


def as_date(v):
    if isinstance(v, dt.datetime):
        return v.date().isoformat()
    if isinstance(v, dt.date):
        return v.isoformat()
    s = text(v)
    return s if re.fullmatch(r"\d{4}-\d{2}-\d{2}", s) else None


def as_timestamp(v):
    """Excel times are Maldives local time (UTC+5)."""
    if isinstance(v, dt.datetime):
        return v.strftime("%Y-%m-%dT%H:%M:%S") + "+05:00"
    s = text(v)
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}(:\d{2})?", s):
        s = s.replace(" ", "T")
        return (s if len(s) == 19 else s + ":00") + "+05:00"
    return s or None


def as_number(v):
    if v is None or text(v) == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return "bad"


def https(v):
    s = text(v)
    return s if s.lower().startswith("https://") else None


def combined(booking, agoda):
    if booking == "Yes" or agoda == "Yes":
        return "Yes"
    return "No" if booking == "No" and agoda == "No" else "No data"


def sheet_rows(wb, name, headers):
    if name not in wb.sheetnames:
        return None
    ws = wb[name]
    head = [text(c.value) for c in ws[1]][:len(headers)]
    if head != headers:
        raise SystemExit(f"{name}: headers must match the template exactly, in the same order.\n  expected {headers}\n  found    {head}")
    rows = []
    for i, cells in enumerate(ws.iter_rows(min_row=2, max_col=len(headers), values_only=True), start=2):
        if all(text(c) == "" for c in cells):
            continue
        rows.append((i, dict(zip(headers, cells))))
    return rows


def example_ids(wb):
    if "Properties" not in wb.sheetnames:
        return set()
    ws = wb["Properties"]
    head = [text(c.value) for c in ws[1]]
    if "property_id" not in head or "notes" not in head:
        return set()
    pid, notes = head.index("property_id"), head.index("notes")
    return {text(r[pid]) for r in ws.iter_rows(min_row=2, values_only=True) if r[pid] and re.search("example", text(r[notes]), re.I)}


def resolve_listings(rows, site):
    """property_id -> listing_id via saved mapping, else unique exact title+island match."""
    listings = {l["id"]: l for l in site["listings"]}
    mappings = site.get("mappings") or {}
    resolved, problems = {}, []
    for pid, name, island in sorted({(r["property_id"], r["guest_house_name"], r["island"]) for r in rows}):
        saved = mappings.get(pid)
        if saved and saved in listings:
            resolved[pid] = saved
            continue
        hits = [l for l in listings.values() if norm(l["title"]) == norm(name) and norm(l["island"]) == norm(island)]
        if len(hits) == 1:
            resolved[pid] = hits[0]["id"]
        else:
            problems.append(f"{pid} ({name}, {island}): {'no' if not hits else len(hits)} matching guest house listing(s) — "
                            "add the mapping to site.json 'mappings' after confirming the right listing")
    taken = {}
    for pid, lid in resolved.items():
        for other, olid in (mappings | resolved).items():
            if olid == lid and other != pid:
                problems.append(f"{pid}: listing {lid} is already linked to {other}")
        taken[lid] = pid
    return resolved, problems


# ---------------------------------------------------------------- daily
def parse_daily(wb):
    ex = example_ids(wb)
    src = "All_Nights"
    rows = sheet_rows(wb, "All_Nights", FEED_HEADERS)
    if not rows:
        src = "Website_Feed"
        rows = sheet_rows(wb, "Website_Feed", FEED_HEADERS)
    if rows is None:
        raise SystemExit("Workbook has no All_Nights or Website_Feed sheet.")
    out, errors, keys = [], [], set()
    for i, v in rows:
        where = f"{src} row {i}"
        pid = text(v["property_id"])
        if not pid:
            errors.append(f"{where}: property_id is missing"); continue
        if pid in ex or re.search("example|unknown id", text(v["guest_house_name"]), re.I):
            errors.append(f"{where}: replace EXAMPLE / Unknown ID property {pid}"); continue
        b, a = text(v["booking_status"]), text(v["agoda_status"])
        if b not in STATUS or a not in STATUS:
            errors.append(f"{where}: booking_status/agoda_status must be Yes, No or No data"); continue
        avail = combined(b, a)
        date = as_date(v["date"])
        if not date:
            errors.append(f"{where}: date must be a real date"); continue
        last = as_timestamp(v["last_updated"])
        if not last:
            errors.append(f"{where}: last_updated is missing"); continue
        rooms = as_number(v["rooms_left"]) if avail == "Yes" else None
        price = as_number(v["lowest_price_per_night_usd"]) if avail == "Yes" else None
        cheap = (text(v["cheapest_platform"]) or None) if avail == "Yes" else None
        if rooms == "bad" or (rooms is not None and (rooms < 0 or rooms != int(rooms))):
            errors.append(f"{where}: rooms_left must be a whole number ≥ 0"); continue
        if price == "bad" or (price is not None and price <= 0):
            errors.append(f"{where}: lowest_price_per_night_usd must be > 0"); continue
        if cheap and (cheap not in PLATFORMS or (a if cheap == "Agoda" else b) != "Yes"):
            errors.append(f"{where}: cheapest_platform must be an available platform"); continue
        links = {}
        for k in ("booking_url", "agoda_url"):
            if text(v[k]) and not https(v[k]):
                errors.append(f"{where}: {k} must use HTTPS"); break
            links[k] = https(v[k])
        else:
            key = (pid, date)
            if key in keys:
                errors.append(f"{where}: duplicate property_id and date"); continue
            keys.add(key)
            out.append({"property_id": pid, "guest_house_name": text(v["guest_house_name"]), "island": text(v["island"]),
                        "date": date, "booking_status": b, "agoda_status": a, "available": avail,
                        "rooms_left": int(rooms) if rooms is not None else None, "price": price, "cheapest": cheap,
                        "booking_url": links["booking_url"], "agoda_url": links["agoda_url"], "last_updated": last})
    if not out and not errors:
        errors.append(f"{src} has no populated rows. If it has formulas, open and save it in Excel (or recalculate) first.")
    return src, out, errors


def daily_sql(chunk, listing):
    values = ",\n".join(
        "(" + ",".join([q(r["property_id"]), q(listing[r["property_id"]]), q(r["date"]), q(r["available"]), q(r["booking_status"]),
                        q(r["agoda_status"]), q(r["rooms_left"]), q(r["price"]), q(r["cheapest"]), q(r["booking_url"]),
                        q(r["agoda_url"]), q(r["last_updated"])]) + ")" for r in chunk)
    return f"""WITH data(property_id, listing_id, date, available, booking_status, agoda_status, rooms_left, price, cheapest, booking_url, agoda_url, last_updated) AS (VALUES
{values}
), d AS (
 SELECT property_id, listing_id::uuid AS listing_id, date::date AS date, available, booking_status, agoda_status,
        rooms_left::integer AS rooms_left, price::numeric(12,2) AS price, cheapest, booking_url, agoda_url, last_updated::timestamptz AS last_updated
 FROM data d0 WHERE EXISTS (SELECT 1 FROM market_listings l WHERE l.id = d0.listing_id::uuid AND l.category::text IN ('guesthouse','homestay'))
), m AS (
 INSERT INTO guesthouse_feed_properties(property_id, listing_id)
 SELECT DISTINCT property_id, listing_id FROM d
 ON CONFLICT (property_id) DO UPDATE SET listing_id = EXCLUDED.listing_id, updated_at = now()
 WHERE guesthouse_feed_properties.listing_id IS DISTINCT FROM EXCLUDED.listing_id
 RETURNING 1
), up AS (
 INSERT INTO guesthouse_daily_rates(listing_id, date, available, booking_status, agoda_status, rooms_left, lowest_price_per_night_usd, cheapest_platform, booking_url, agoda_url, last_updated)
 SELECT listing_id, date, available, booking_status, agoda_status, rooms_left, price, cheapest, booking_url, agoda_url, last_updated FROM d
 ON CONFLICT (listing_id, date) DO UPDATE SET available = EXCLUDED.available, booking_status = EXCLUDED.booking_status,
   agoda_status = EXCLUDED.agoda_status, rooms_left = EXCLUDED.rooms_left, lowest_price_per_night_usd = EXCLUDED.lowest_price_per_night_usd,
   cheapest_platform = EXCLUDED.cheapest_platform, booking_url = EXCLUDED.booking_url, agoda_url = EXCLUDED.agoda_url,
   last_updated = EXCLUDED.last_updated, imported_at = now()
 WHERE guesthouse_daily_rates.last_updated <= EXCLUDED.last_updated
   AND (guesthouse_daily_rates.available, guesthouse_daily_rates.booking_status, guesthouse_daily_rates.agoda_status,
        guesthouse_daily_rates.rooms_left, guesthouse_daily_rates.lowest_price_per_night_usd, guesthouse_daily_rates.cheapest_platform,
        guesthouse_daily_rates.booking_url, guesthouse_daily_rates.agoda_url)
       IS DISTINCT FROM
       (EXCLUDED.available, EXCLUDED.booking_status, EXCLUDED.agoda_status, EXCLUDED.rooms_left,
        EXCLUDED.lowest_price_per_night_usd, EXCLUDED.cheapest_platform, EXCLUDED.booking_url, EXCLUDED.agoda_url)
 RETURNING (xmax = 0) AS inserted
)
SELECT {len(chunk)} AS rows_sent,
       (SELECT count(*) FROM d) AS rows_valid,
       (SELECT count(*) FROM up WHERE inserted) AS nights_added,
       (SELECT count(*) FROM up WHERE NOT inserted) AS nights_changed,
       (SELECT count(*) FROM m) AS mappings_saved;
"""


# ---------------------------------------------------------------- monthly
def parse_monthly(wb):
    ex = example_ids(wb)
    rows = sheet_rows(wb, "Room_Types", ROOM_HEADERS)
    if rows is None:
        raise SystemExit("Workbook has no Room_Types sheet.")
    out, errors, keys = [], [], set()
    for i, v in rows:
        where = f"Room_Types row {i}"
        pid = text(v["property_id"])
        if not pid:
            errors.append(f"{where}: property_id is missing"); continue
        if pid in ex or re.search("example", text(v["notes"]), re.I) or re.search("example|unknown id", text(v["guest_house_name"]), re.I):
            errors.append(f"{where}: replace EXAMPLE / Unknown ID property {pid}"); continue
        cat = text(v["room_category"])
        if cat not in ROOM_TYPES:
            errors.append(f"{where}: room_category must be one of {ROOM_TYPES}"); continue
        views = []
        for k in ("view_type_1", "view_type_2"):
            s = text(v[k])
            if s and s not in ROOM_VIEWS:
                errors.append(f"{where}: {k} must be one of {ROOM_VIEWS}"); views = None; break
            if s and s not in views:
                views.append(s)
        if views is None:
            continue
        guests, rooms, price = as_number(v["max_guests"]), as_number(v["number_of_rooms"]), as_number(v["price_mvr_per_night"])
        if guests in (None, "bad") or guests < 1 or guests != int(guests):
            errors.append(f"{where}: max_guests must be a whole number ≥ 1"); continue
        if rooms in (None, "bad") or rooms < 0 or rooms != int(rooms):
            errors.append(f"{where}: number_of_rooms must be a whole number ≥ 0"); continue
        if price in (None, "bad") or price < 0:
            errors.append(f"{where}: price_mvr_per_night must be a number ≥ 0"); continue
        avail = text(v["available"]) or "Yes"
        if avail not in ("Yes", "No"):
            errors.append(f"{where}: available must be Yes or No"); continue
        photos, bad = [], False
        for n in range(1, 9):
            s = text(v[f"photo_{n}_url"])
            if s and not https(s):
                errors.append(f"{where}: photo_{n}_url must be an https link"); bad = True; break
            if s and s not in photos:
                photos.append(s)
        if bad:
            continue
        if (pid, cat) in keys:
            errors.append(f"{where}: {cat} appears twice for {pid}"); continue
        keys.add((pid, cat))
        out.append({"row": i, "property_id": pid, "guest_house_name": text(v["guest_house_name"]), "island": text(v["island"]),
                    "name": cat, "views": views, "capacity": int(guests), "quantity": int(rooms), "price": round(price, 2),
                    "available": avail == "Yes", "photos": photos})
    if not out and not errors:
        errors.append("Room_Types has no populated rows.")
    return out, errors


def monthly_sql(rows, listing, site):
    units = {(u["listing_id"], u["name"]): u for u in site["units"]}
    stmts, skipped, plan = [], [], []
    for r in rows:
        lid = listing[r["property_id"]]
        u = units.get((lid, r["name"]))
        existing = (u.get("images") or ([u["image_url"]] if u.get("image_url") else [])) if u else []
        images = list(dict.fromkeys([*existing, *r["photos"]]))
        if len(images) < MIN_ROOM_PHOTOS:
            skipped.append(f"{r['property_id']} {r['name']}: {len(images)}/{MIN_ROOM_PHOTOS} photos — add photo links or upload photos on the website")
            continue
        cols = (f"price = {r['price']}, capacity = {r['capacity']}, quantity = {r['quantity']}, available = {str(r['available']).lower()}, "
                f"view_types = {q_array(r['views'])}, images = {q_array(images)}, image_url = {q(images[0])}")
        if u:
            stmts.append(f"UPDATE market_units SET {cols} WHERE id = {q(u['id'])}::uuid RETURNING 'updated' AS action, name;")
            plan.append(("update", r))
        else:
            stmts.append(
                "INSERT INTO market_units(listing_id, name, price, price_unit, capacity, quantity, available, view_types, images, image_url) "
                f"SELECT {q(lid)}::uuid, {q(r['name'])}, {r['price']}, l.price_unit, {r['capacity']}, {r['quantity']}, {str(r['available']).lower()}, "
                f"{q_array(r['views'])}, {q_array(images)}, {q(images[0])} FROM market_listings l "
                f"WHERE l.id = {q(lid)}::uuid AND l.category::text IN ('guesthouse','homestay') "
                f"AND NOT EXISTS (SELECT 1 FROM market_units x WHERE x.listing_id = l.id AND x.name = {q(r['name'])}) RETURNING 'added' AS action, name;")
            plan.append(("add", r))
    mapping = sorted({(r["property_id"], listing[r["property_id"]]) for r in rows})
    if mapping:
        stmts.insert(0, "INSERT INTO guesthouse_feed_properties(property_id, listing_id) VALUES "
                     + ",".join(f"({q(p)}, {q(l)}::uuid)" for p, l in mapping)
                     + " ON CONFLICT (property_id) DO UPDATE SET listing_id = EXCLUDED.listing_id, updated_at = now() "
                       "WHERE guesthouse_feed_properties.listing_id IS DISTINCT FROM EXCLUDED.listing_id RETURNING property_id;")
    return stmts, skipped, plan


# ---------------------------------------------------------------- contacts
def phone_e164(v):
    """'+960 777 1234', '7771234', '960-7771234' -> '+960 7771234'; other countries keep their + code. None if invalid."""
    s = text(v)
    if not s:
        return ""
    plus = s.startswith("+")
    digits = re.sub(r"\D", "", s)
    if not plus and len(digits) == 7:
        digits = "960" + digits
    elif not plus and not digits.startswith("960"):
        return None
    if digits.startswith("960"):
        return "+960 " + digits[3:] if len(digits) == 10 else None
    return "+" + digits if 8 <= len(digits) <= 15 else None


def parse_contacts(wb):
    ex = example_ids(wb)
    rows = sheet_rows(wb, "Contacts", CONTACT_HEADERS)
    if rows is None:
        raise SystemExit("Workbook has no Contacts sheet.")
    out, errors, keys = [], [], set()
    for i, v in rows:
        where = f"Contacts row {i}"
        pid = text(v["property_id"])
        if not pid:
            errors.append(f"{where}: property_id is missing"); continue
        if pid in ex or re.search("example", text(v["notes"]), re.I) or re.search("example|unknown id", text(v["guest_house_name"]), re.I):
            errors.append(f"{where}: replace EXAMPLE / Unknown ID property {pid}"); continue
        phone, wa, email = phone_e164(v["phone"]), phone_e164(v["whatsapp"]), text(v["email"]).lower()
        if phone is None:
            errors.append(f"{where}: phone '{text(v['phone'])}' is not a valid number (use +960 and 7 digits, or + and country code)"); continue
        if wa is None:
            errors.append(f"{where}: whatsapp '{text(v['whatsapp'])}' is not a valid number (use +960 and 7 digits, or + and country code)"); continue
        if email and not EMAIL_RE.match(email):
            errors.append(f"{where}: email '{email}' doesn't look valid"); continue
        if not (phone or wa or email):
            continue
        if pid in keys:
            errors.append(f"{where}: {pid} appears twice"); continue
        keys.add(pid)
        out.append({"property_id": pid, "guest_house_name": text(v["guest_house_name"]), "island": text(v["island"]),
                    "phone": phone, "whatsapp": wa, "email": email})
    if not out and not errors:
        errors.append("Contacts has no rows with a phone, WhatsApp or email.")
    return out, errors


def contact_text(phone, wa):
    """What the site's providerContact() reads: the first number is the Call number,
    the number after the word 'WhatsApp' is the WhatsApp number."""
    if phone and wa and re.sub(r"\D", "", phone) != re.sub(r"\D", "", wa):
        return f"{phone} · WhatsApp {wa}"
    if phone:
        return phone
    return f"WhatsApp {wa}" if wa else ""


def contacts_sql(rows, listing, site):
    by_id = {l["id"]: l for l in site["listings"]}
    patches, notes = [], []
    for r in rows:
        lid = listing[r["property_id"]]
        l = by_id[lid]
        old = l.get("details") or {}
        old_contact = text(old.get("contact"))
        # keep the other half of the contact when only one number is given
        old_phone = re.split(r"\s*·?\s*whatsapp", old_contact, flags=re.I)[0].strip(" ·/")
        m = re.search(r"whatsapp\s*(\+?[\d\s-]{7,})", old_contact, re.I)
        old_wa = phone_e164(m.group(1)) if m else ""
        phone = r["phone"] or phone_e164(old_phone) or ""
        wa = r["whatsapp"] or old_wa or ""
        patch = {}
        new_contact = contact_text(phone, wa)
        if new_contact and new_contact != old_contact:
            patch["contact"] = new_contact
        if r["whatsapp"] and r["whatsapp"] != text(old.get("whatsapp")):
            patch["whatsapp"] = r["whatsapp"]
        if r["email"] and r["email"] != text(old.get("email")).lower():
            patch["email"] = r["email"]
        if (norm(l["title"]), norm(l["island"])) in DIRECTORY_WHATSAPP and r["whatsapp"]:
            notes.append(f"{r['property_id']} ({l['title']}): the WhatsApp button keeps the site's directory number until the site code reads details.whatsapp; phone and email still update")
        if patch:
            patches.append((lid, r["property_id"], patch))
    if not patches:
        return [], notes, 0
    values = ",\n ".join(f"({q(lid)}, {q(json.dumps(p, ensure_ascii=False))})" for lid, _, p in patches)
    maps = sorted({(pid, lid) for lid, pid, _ in patches})
    maps_sql = ", ".join(f"({q(p)}, {q(l)}::uuid)" for p, l in maps)
    stmt = f"""WITH v(listing_id, patch) AS (VALUES
 {values}
), m AS (
 INSERT INTO guesthouse_feed_properties(property_id, listing_id) VALUES {maps_sql}
 ON CONFLICT (property_id) DO UPDATE SET listing_id = EXCLUDED.listing_id, updated_at = now()
 WHERE guesthouse_feed_properties.listing_id IS DISTINCT FROM EXCLUDED.listing_id
 RETURNING 1
), u AS (
 UPDATE market_listings l SET details = coalesce(l.details, '{{}}'::jsonb) || v.patch::jsonb
 FROM v WHERE l.id = v.listing_id::uuid AND l.category::text IN ('guesthouse','homestay')
   AND NOT (coalesce(l.details, '{{}}'::jsonb) @> v.patch::jsonb)
 RETURNING l.id
)
SELECT (SELECT count(*) FROM u) AS listings_updated, (SELECT count(*) FROM m) AS mappings_saved;
"""
    return [stmt], notes, len(patches)


# ---------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("mode", choices=["daily", "monthly", "contacts", "site-query"])
    ap.add_argument("workbook", nargs="?")
    ap.add_argument("--site", help="JSON from the site-query")
    ap.add_argument("--out", help="directory for generated .sql files")
    ap.add_argument("--chunk", type=int, default=300, help="rows per SQL file (daily)")
    a = ap.parse_args()
    if a.mode == "site-query":
        print(SITE_QUERY); return
    if not (a.workbook and a.site and a.out):
        ap.error("workbook, --site and --out are required")
    site = json.load(open(a.site))
    site = site.get("site", site)
    wb = openpyxl.load_workbook(a.workbook, data_only=True, read_only=False)
    os.makedirs(a.out, exist_ok=True)
    report = {"mode": a.mode}

    if a.mode == "daily":
        src, rows, errors = parse_daily(wb)
        report.update(source=src, rows=len(rows))
    elif a.mode == "contacts":
        rows, errors = parse_contacts(wb)
        report.update(source="Contacts", rows=len(rows))
    else:
        rows, errors = parse_monthly(wb)
        report.update(source="Room_Types", rows=len(rows))
    listing, problems = resolve_listings(rows, site) if rows else ({}, [])
    report["errors"] = errors
    report["unmatched_properties"] = problems
    if errors or problems:
        report["status"] = "fix_needed"
        print(json.dumps(report, indent=2, ensure_ascii=False)); sys.exit(1)

    files = []
    if a.mode == "daily":
        for n in range(0, len(rows), a.chunk):
            path = os.path.join(a.out, f"daily_{n // a.chunk + 1:03d}.sql")
            open(path, "w").write(daily_sql(rows[n:n + a.chunk], listing))
            files.append(path)
        report["nights"] = len(rows)
        report["guest_houses"] = len({r["property_id"] for r in rows})
    elif a.mode == "contacts":
        stmts, notes, n = contacts_sql(rows, listing, site)
        for k, st in enumerate(stmts, 1):
            path = os.path.join(a.out, f"contacts_{k:03d}.sql")
            open(path, "w").write(st)
            files.append(path)
        report["listings_to_update"] = n
        report["unchanged"] = len(rows) - n
        report["whatsapp_directory_notes"] = notes
    else:
        stmts, skipped, plan = monthly_sql(rows, listing, site)
        for n, s in enumerate(stmts, 1):
            path = os.path.join(a.out, f"monthly_{n:03d}.sql")
            open(path, "w").write(s + "\n")
            files.append(path)
        report["rooms_to_add"] = sum(1 for k, _ in plan if k == "add")
        report["rooms_to_update"] = sum(1 for k, _ in plan if k == "update")
        report["rooms_skipped_photos"] = skipped
    report["status"] = "ready"
    report["sql_files"] = files
    print(json.dumps(report, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
