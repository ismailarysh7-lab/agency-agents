#!/usr/bin/env python3
"""Photo review for sukunmaldives.com guest house listings.

Claude runs this in two steps around its own look at the photos:

  1. fetch   photos.json -> downloads every gallery and room photo, writes one numbered
             contact sheet per guest house (G1, G2… = gallery; R1-1, R1-2… = room 1) and
             index.json (label -> URL).
  2. (Claude looks at each sheet and writes decisions.json)
  3. apply   photos.json + decisions.json -> SQL that reorders photos (cover first),
             removes rejected ones, and saves a backup on the listing so it can be undone.
  4. undo    photos.json (fresh export) -> SQL that puts back the backed-up photo lists.

photos.json is the `photos` value from `python3 photo_review.py export-query`, run with
the Lovable `query_database` tool.

decisions.json:
  { "<listing_id>": {
      "cover": "G3",                       # first photo shown for the guest house
      "remove": {"G2": "people", "G7": "blurry"},
      "rooms": { "<unit_id>": {"cover": "R1-2", "remove": {"R1-4": "people"}} } } }

Rules (enforced here, not only in Claude's judgement):
  - a removed photo is never deleted from storage; the listing keeps a backup in
    details.photo_review (previous gallery, previous room photos, removed + reasons)
  - a gallery is never emptied; a room is never left with fewer photos than before
    unless the decision removes them, and rooms under 5 photos are reported
  - each update only applies if the photo list is still exactly what was exported,
    so a change made on the website in the meantime is never overwritten
  - one database statement per batch of 8 guest houses (the Lovable connection limits
    how many database actions run per hour)
"""
import argparse, hashlib, io, json, os, re, sys
from concurrent.futures import ThreadPoolExecutor

import requests
from PIL import Image, ImageDraw, ImageFont, ImageOps

MIN_ROOM_PHOTOS = 5
TILE = 300
COLS = 4

EXPORT_QUERY = """SELECT json_agg(json_build_object(
  'id', l.id, 'title', l.title, 'island', l.island, 'status', l.status, 'images', l.images,
  'units', (SELECT coalesce(json_agg(json_build_object('id', u.id, 'name', u.name, 'images', u.images) ORDER BY u.created_at), '[]')
            FROM market_units u WHERE u.listing_id = l.id)
) ORDER BY l.island, l.title) AS photos
FROM market_listings l
WHERE l.category::text IN ('guesthouse','homestay') AND l.status::text = 'approved';"""


def q(v):
    return "NULL" if v is None else "'" + str(v).replace("'", "''") + "'"


def q_array(values):
    return "ARRAY[" + ",".join(q(v) for v in values) + "]::text[]" if values else "'{}'::text[]"


def slug(s):
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")[:50] or "listing"


def font(size):
    for path in ("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", "/usr/share/fonts/dejavu/DejaVuSans-Bold.ttf"):
        if os.path.exists(path):
            return ImageFont.truetype(path, size)
    return ImageFont.load_default()


def download(url, dest):
    if os.path.exists(dest):
        return dest, None
    try:
        r = requests.get(url, timeout=30, stream=True, headers={"User-Agent": "Mozilla/5.0 (compatible; SukunPhotoReview/1.0)", "Accept": "image/*,*/*;q=0.8"})
        r.raise_for_status()
        data = r.raw.read(15 * 1024 * 1024 + 1, decode_content=True)
        if len(data) > 15 * 1024 * 1024:
            return None, "larger than 15 MB"
        img = Image.open(io.BytesIO(data))
        img = ImageOps.exif_transpose(img).convert("RGB")
        img.thumbnail((900, 900))
        img.save(dest, "JPEG", quality=85)
        return dest, None
    except Exception as e:  # network, HTTP, or not an image
        return None, f"{type(e).__name__}: {str(e)[:120]}"


def labels_for(listing):
    out = [(f"G{i}", url, None) for i, url in enumerate(listing.get("images") or [], 1)]
    for r, unit in enumerate(listing.get("units") or [], 1):
        out += [(f"R{r}-{i}", url, unit["id"]) for i, url in enumerate(unit.get("images") or [], 1)]
    return out


def contact_sheet(listing, files, dest):
    """One image per guest house: gallery tiles, then a row block per room, each labelled."""
    big, small = font(30), font(20)
    sections = [("Guest house gallery", [(lab, files.get(lab)) for lab, _, uid in labels_for(listing) if uid is None])]
    for r, unit in enumerate(listing.get("units") or [], 1):
        tiles = [(lab, files.get(lab)) for lab, _, uid in labels_for(listing) if uid == unit["id"]]
        sections.append((f"Room {r}: {unit['name'][:60]}", tiles))
    heights = [44 + max(1, -(-len(t) // COLS)) * (TILE + 8) for _, t in sections]
    sheet = Image.new("RGB", (COLS * (TILE + 8) + 8, 60 + sum(heights)), "white")
    d = ImageDraw.Draw(sheet)
    d.text((10, 14), f"{listing['title'][:70]} · {listing['island']}", fill="black", font=big)
    y = 60
    for (title, tiles), h in zip(sections, heights):
        d.text((10, y + 8), title, fill=(13, 124, 122), font=small)
        for k, (lab, path) in enumerate(tiles):
            x0 = 8 + (k % COLS) * (TILE + 8)
            y0 = y + 40 + (k // COLS) * (TILE + 8)
            if path:
                im = Image.open(path)
                im.thumbnail((TILE, TILE))
                sheet.paste(im, (x0 + (TILE - im.width) // 2, y0 + (TILE - im.height) // 2))
            else:
                d.rectangle([x0, y0, x0 + TILE, y0 + TILE], outline="red", width=3)
                d.text((x0 + 10, y0 + TILE // 2), "could not load", fill="red", font=small)
            d.rectangle([x0, y0, x0 + 86, y0 + 32], fill="black")
            d.text((x0 + 6, y0 + 4), lab, fill="white", font=small)
        if not tiles:
            d.text((20, y + 50), "no photos", fill="gray", font=small)
        y += h
    sheet.save(dest, "JPEG", quality=80)


def cmd_fetch(photos, out):
    os.makedirs(os.path.join(out, "img"), exist_ok=True)
    os.makedirs(os.path.join(out, "sheets"), exist_ok=True)
    jobs, index, failures = [], {}, []
    for n, listing in enumerate(photos, 1):
        for lab, url, uid in labels_for(listing):
            name = hashlib.sha1(url.encode()).hexdigest()[:16] + ".jpg"
            jobs.append((listing["id"], lab, url, os.path.join(out, "img", name)))
    with ThreadPoolExecutor(8) as pool:
        results = list(pool.map(lambda j: (j, download(j[2], j[3])), jobs))
    files = {}
    for (lid, lab, url, _), (path, err) in results:
        files.setdefault(lid, {})[lab] = path
        index.setdefault(lid, {})[lab] = url
        if err:
            failures.append({"listing_id": lid, "label": lab, "url": url[:120], "error": err})
    sheets = []
    for n, listing in enumerate(photos, 1):
        dest = os.path.join(out, "sheets", f"{n:03d}-{slug(listing['title'])}.jpg")
        contact_sheet(listing, files.get(listing["id"], {}), dest)
        sheets.append({"n": n, "listing_id": listing["id"], "title": listing["title"], "island": listing["island"], "sheet": dest,
                       "gallery": len(listing.get("images") or []), "rooms": [len(u.get("images") or []) for u in listing.get("units") or []]})
    json.dump({"index": index, "sheets": sheets, "failures": failures}, open(os.path.join(out, "index.json"), "w"), indent=1)
    print(json.dumps({"listings": len(photos), "photos": len(jobs), "failed": len(failures), "sheets_dir": os.path.join(out, "sheets"),
                      "failures_by_host": _hosts(failures)}, indent=2))


def _hosts(failures):
    out = {}
    for f in failures:
        h = (re.match(r"https?://([^/]+)", f["url"]) or [None, "?"])[1]
        out[h] = out.get(h, 0) + 1
    return out


def reorder(urls, labels, decision, prefix_ok):
    """Return (new_list, removed{url: reason}). Labels map position i -> label."""
    remove = {k: v for k, v in (decision.get("remove") or {}).items() if prefix_ok(k)}
    kept = [(lab, u) for lab, u in zip(labels, urls) if lab not in remove]
    cover = decision.get("cover")
    if cover and any(lab == cover for lab, _ in kept):
        kept = [x for x in kept if x[0] == cover] + [x for x in kept if x[0] != cover]
    removed = {u: remove[lab] for lab, u in zip(labels, urls) if lab in remove}
    return [u for _, u in kept], removed


def batch_sql(items):
    """One statement for a batch of guest houses. A listing is updated only if its gallery is unchanged
    since export; its rooms are updated only if the listing (and so its backup) was."""
    g_rows = ",\n ".join(f"({q(i['id'])}, {q_array(i['old'])}, {q_array(i['new'])}, {q(json.dumps(i['backup'], ensure_ascii=False))})" for i in items)
    r_items = [(i["id"], r) for i in items for r in i["rooms"]]
    r_cte = ""
    if r_items:
        r_rows = ",\n ".join(f"({q(lid)}, {q(r['id'])}, {q_array(r['old'])}, {q_array(r['new'])})" for lid, r in r_items)
        r_cte = f""", r(listing_id, id, old, new) AS (VALUES
 {r_rows}
), ur AS (
 UPDATE market_units u SET images = r.new, image_url = r.new[1]
 FROM r WHERE u.id = r.id::uuid AND u.images = r.old AND r.listing_id::uuid IN (SELECT id FROM ug)
 RETURNING u.id
)"""
    return f"""WITH g(id, old, new, backup) AS (VALUES
 {g_rows}
), ug AS (
 UPDATE market_listings l SET images = g.new,
   details = coalesce(l.details, '{{}}'::jsonb) || jsonb_build_object('photo_review', g.backup::jsonb || jsonb_build_object('reviewed_at', now()))
 FROM g WHERE l.id = g.id::uuid AND l.images = g.old AND l.category::text IN ('guesthouse','homestay')
 RETURNING l.id
){r_cte}
SELECT (SELECT count(*) FROM ug) AS listings_updated, {"(SELECT count(*) FROM ur)" if r_items else "0"} AS rooms_updated, {len(items)} AS listings_sent;
"""


def cmd_apply(photos, decisions, out, per_batch=8):
    os.makedirs(out, exist_ok=True)
    items, report = [], {"listings_changed": 0, "gallery_removed": 0, "room_photos_removed": 0, "covers_changed": 0,
                         "skipped": [], "rooms_under_5": []}
    for listing in photos:
        dec = decisions.get(listing["id"])
        if not dec:
            continue
        g_old = listing.get("images") or []
        g_new, g_removed = reorder(g_old, [f"G{i}" for i in range(1, len(g_old) + 1)], dec, lambda k: k.startswith("G"))
        if g_old and not g_new:
            report["skipped"].append(f"{listing['title']}: decision would remove every gallery photo — not applied")
            continue
        rooms, room_backup = [], {}
        for r, unit in enumerate(listing.get("units") or [], 1):
            udec = (dec.get("rooms") or {}).get(unit["id"])
            u_old = unit.get("images") or []
            if not udec or not u_old:
                continue
            u_new, u_removed = reorder(u_old, [f"R{r}-{i}" for i in range(1, len(u_old) + 1)], udec, lambda k: k.startswith(f"R{r}-"))
            if u_new == u_old:
                continue
            if not u_new:
                report["skipped"].append(f"{listing['title']} · {unit['name']}: would remove every room photo — not applied")
                continue
            room_backup[unit["id"]] = {"name": unit["name"], "previous": u_old, "removed": u_removed}
            rooms.append({"id": unit["id"], "old": u_old, "new": u_new})
            report["room_photos_removed"] += len(u_removed)
            if len(u_new) < MIN_ROOM_PHOTOS:
                report["rooms_under_5"].append(f"{listing['title']} · {unit['name']}: {len(u_new)} photos left")
        if g_new == g_old and not rooms:
            continue
        backup = {"reviewed_by": "Maldives Guest House Agent", "previous_images": g_old, "removed": g_removed, "rooms": room_backup}
        items.append({"id": listing["id"], "old": g_old, "new": g_new, "backup": backup, "rooms": rooms})
        report["listings_changed"] += 1
        report["gallery_removed"] += len(g_removed)
        report["covers_changed"] += int(bool(g_new) and bool(g_old) and g_new[0] != g_old[0])
    files = []
    for k in range(0, len(items), per_batch):
        path = os.path.join(out, f"photos_{k // per_batch + 1:03d}.sql")
        open(path, "w").write(batch_sql(items[k:k + per_batch]))
        files.append(path)
    report["sql_files"] = files
    print(json.dumps(report, indent=2, ensure_ascii=False))


def cmd_undo(listing_ids, out):
    """One statement that restores the backed-up gallery and room photos for the given listings."""
    os.makedirs(out, exist_ok=True)
    ids = ", ".join(f"{q(i)}::uuid" for i in listing_ids)
    sql = f"""WITH b AS (
 SELECT id, details->'photo_review' AS pr FROM market_listings WHERE id IN ({ids}) AND details ? 'photo_review'
), ur AS (
 UPDATE market_units u SET images = ARRAY(SELECT jsonb_array_elements_text(b.pr->'rooms'->(u.id::text)->'previous')),
        image_url = b.pr->'rooms'->(u.id::text)->'previous'->>0
 FROM b WHERE u.listing_id = b.id AND b.pr->'rooms' ? u.id::text
 RETURNING u.id
), ul AS (
 UPDATE market_listings l SET images = ARRAY(SELECT jsonb_array_elements_text(b.pr->'previous_images')), details = l.details - 'photo_review'
 FROM b WHERE l.id = b.id
 RETURNING l.id
)
SELECT (SELECT count(*) FROM ul) AS listings_restored, (SELECT count(*) FROM ur) AS rooms_restored;
"""
    path = os.path.join(out, "undo.sql")
    open(path, "w").write(sql)
    print(path)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("mode", choices=["export-query", "fetch", "apply", "undo"])
    ap.add_argument("--photos", help="photos.json from the export query")
    ap.add_argument("--decisions", help="decisions.json written after reviewing the sheets")
    ap.add_argument("--ids", nargs="*", help="listing ids to undo")
    ap.add_argument("--out", help="output directory")
    a = ap.parse_args()
    if a.mode == "export-query":
        print(EXPORT_QUERY); return
    if a.mode == "undo":
        cmd_undo(a.ids or [], a.out); return
    photos = json.load(open(a.photos))
    photos = photos.get("photos", photos) if isinstance(photos, dict) else photos
    if a.mode == "fetch":
        cmd_fetch(photos, a.out)
    else:
        cmd_apply(photos, json.load(open(a.decisions)), a.out)


if __name__ == "__main__":
    main()
