#!/usr/bin/env python3
"""
Gundam Media Hub - New-Volume Discovery (manga).

Watches for new English Gundam manga volumes from the sources that can be
checked automatically, and refreshes Upcoming entries from their live
publisher pages:

  1. PRH sitemap scan. Enumerates every Penguin Random House book page
     whose slug contains "gundam" from PRH's works sitemaps (PRH
     distributes Kodansha USA, so this covers The Origin, The Origin
     Deluxe, and Cucuruz Doan's Island). Pages whose ISBN matches a
     catalog entry get their canonical URL attached to that entry; pages
     for volumes not in the catalog are REPORTED as candidates, never
     appended -- a human verifies a candidate before it goes live.
  2. Series-page watch. Fetches the VIZ (Thunderbolt), Yen Press
     (Vanadis Heart), and Denpa (Beltorchika's Children) series pages
     and compares the highest volume number listed against the catalog.
     Report-only: a higher number prints a WATCH CANDIDATE line.
  3. Upcoming refresh. Re-reads live PRH pages for Upcoming entries with
     canonical PRH URLs and updates date/status/page-count from
     PRH-verified values, so slips and releases are caught the day PRH
     reflects them. Entries with no PRH page (VIZ/Yen/Denpa titles, which
     PRH never lists) fall back to their Indigo product page instead,
     report-only: date/ISBN mismatches are logged, never written.

Runs in the daily GitHub Action BEFORE check_buy_links.py, so attached
PRH URLs get link verification the same day. Stdlib only; no pip installs.

Fail-open throughout: any sitemap fetch failure, parse failure, or
validation failure aborts with the catalog untouched (exit 0, so the link
checker still runs). The series-page watch runs independently: a PRH
failure never silences it, and vice versa.

What this script NEVER does:
  * Append a new entry automatically (candidates are human-verified).
  * Delete or rename any entry.
  * Touch curated fields on existing entries: title, creators, publisher,
    format, subtitle, notes.
  * Write ISBNs for existing entries. ISBN diffs are logged as
    report-only lines.
  * Touch links/linksCheckedAt, except attaching a discovered canonical
    PRH URL for an entry that has none (the checker verifies it minutes
    later in the same Action run).

Status transitions are strict: an Upcoming entry flips only when its live
PRH page was actually read in this run (availability + date from the
page). A failed fetch never flips anything. Released entries are never
re-checked by this script.

Out of scope: discovering entirely new Gundam manga series or licenses.
That is a research task; this script watches volumes within series the
catalog already covers.
"""

import argparse
import html
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import date, datetime

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_FILE = os.path.join(BASE_DIR, "data", "gundam_manga.json")

PRH = "https://www.penguinrandomhouse.com"
SITEMAP_INDEX = PRH + "/sitemap.xml"
BOOK_RE = re.compile(r"https://(www\.)?penguinrandomhouse\.com/books/(\d+)/([a-z0-9\-\.]+)/")
GUNDAM_MARKER = "gundam"
USER_AGENT = {
    "User-Agent": "gundam-media-hub-discovery/1.0 (+https://github.com/Adeelshah001/gundam-media-hub)"
}

# Publisher-primary pins: values stated by the publisher itself that must
# survive PRH-gated refreshes. The publisher's own page is primary; PRH is
# secondary. Add an entry here (with its source in the comment) when a
# publisher page and PRH disagree. Empty: no such conflict is known.
PINNED = {
    # Beltorchika Vol. 4: retailer convergence says January 2027 (UK
    # wholesaler Jan 7 + preorder form); Indigo Canada lists the outlier
    # Jul 2, 2027. Never let a re-read overwrite January 2027.
    "beltorchikas-children-vol-4": {"releaseDate": "2027-01"},
}

# Series-page watch: (seriesId, page URL, volume-number pattern).
# Patterns match volume numbers in page-internal product URLs only. Plain
# "Vol. N" text is NOT used: series pages also list other series'
# volumes (recommendations), which pollutes text matches.
WATCH_SOURCES = [
    ("thunderbolt", "https://www.viz.com/mobile-suit-gundam-thunderbolt",
     re.compile(r"thunderbolt-volume-(\d+)")),
    ("vanadis-heart", "https://yenpress.com/series/mobile-suit-gundam-the-witch-from-mercury-vanadis-heart",
     re.compile(r"vanadis-heart-vol-(\d+)")),
    ("beltorchikas-children", "https://denpa.pub/book",
     re.compile(r"beltorchikas-children-(\d+)")),
]

REQUIRED_FIELDS = [
    "id", "title", "series", "seriesId", "creators", "publisher",
    "releaseDate", "isbn", "format", "status", "type",
]


class AbortDiscovery(Exception):
    """Fail-open abort: catalog stays untouched."""


class RateLimited(AbortDiscovery):
    pass


def fetch(url, timeout=30):
    req = urllib.request.Request(url, headers=USER_AGENT)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        if e.code == 429:
            raise RateLimited(f"HTTP 429 from {url}")
        return e.code, ""
    except Exception as e:
        raise AbortDiscovery(f"fetch failed for {url}: {e}")


# ---------------------------------------------------------------------------
# Sitemap enumeration
# ---------------------------------------------------------------------------

def sitemap_years(catalog):
    """Years to scan: every catalog release year, plus last/current/next.

    PRH works sitemaps are one file per publication year. Scanning the
    catalog's years covers attaching canonical pages to existing entries;
    the rolling window covers newly announced volumes. (107 files exist
    in total; the window keeps the daily run to ~15.)
    """
    years = set()
    for item in catalog:
        try:
            years.add(int(str(item.get("releaseDate", ""))[:4]))
        except (ValueError, TypeError):
            pass
    y = date.today().year
    years.update([y - 1, y, y + 1])
    return sorted(years)


def works_sitemaps_from_index():
    """Best-effort: read the sitemap index. Returns {year: url}, or None
    when the index cannot be used (caller falls back to built URLs)."""
    try:
        status, body = fetch(SITEMAP_INDEX)
    except AbortDiscovery:
        return None
    if status != 200:
        return None
    out = {}
    for m in re.finditer(
        r"<loc>\s*(https://www\.penguinrandomhouse\.com/sitemap-works-(\d+)\.xml)\s*</loc>",
        body,
    ):
        out[int(m.group(2))] = m.group(1)
    return out or None


def enumerate_sitemap_ids(catalog, delay):
    """Every "gundam" slug in the wanted works sitemaps.
    Returns {prh_id: sitemap_url}. A missing sitemap file for a year
    (e.g. next year's file does not exist yet) is skipped, not an error.
    Any other failure aborts (fail open)."""
    index = works_sitemaps_from_index()
    found = {}
    for year in sitemap_years(catalog):
        sitemap_url = (index or {}).get(year) or f"{PRH}/sitemap-works-{year}.xml"
        status, body = fetch(sitemap_url)
        if status == 404:
            print(f"  no sitemap for {year}; skipping")
            continue
        if status != 200:
            raise AbortDiscovery(f"sitemap {sitemap_url} -> HTTP {status}")
        for m in BOOK_RE.finditer(body):
            if GUNDAM_MARKER in m.group(3):
                found[m.group(2)] = m.group(0)
        time.sleep(max(delay, 0.5))
    if not found:
        raise AbortDiscovery("zero gundam matches in sitemaps; format changed?")
    return found


# ---------------------------------------------------------------------------
# Catalog helpers
# ---------------------------------------------------------------------------

def load_catalog():
    with open(DATA_FILE, encoding="utf-8") as f:
        return json.load(f)


def known_prh_ids(catalog):
    """PRH book id -> entry id, for entries with a canonical PRH link."""
    known = {}
    for item in catalog:
        for entry in item.get("links") or []:
            if entry.get("store") != "prh":
                continue
            m = BOOK_RE.search(entry.get("url") or "")
            if m:
                known[m.group(2)] = item["id"]
    return known


def normalize_title(title):
    t = title.lower()
    t = re.sub(r"[^a-z0-9]+", " ", t)
    return re.sub(r"\s+", " ", t).strip()


def title_map_without_prh(catalog, known):
    """Normalized title -> entry, for entries with no canonical PRH URL.
    Used to attach a newly published PRH page to an existing entry instead
    of reporting it as a new candidate. (ISBN matching is tried first;
    title formats differ between PRH and the catalog, so this is only a
    fallback.)"""
    known_ids = set(known.values())
    out = {}
    for item in catalog:
        if item["id"] in known_ids:
            continue
        out[normalize_title(item["title"])] = item
    return out


def attach_prh_url(entry, url):
    """Attach a canonical PRH URL to an entry's links array. Adds a PRH
    entry, or replaces a non-canonical (search) PRH URL. Never clobbers
    an existing canonical page. Returns True when the entry changed."""
    links = entry.setdefault("links", [])
    for e in links:
        if e.get("store") != "prh":
            continue
        if BOOK_RE.search(e.get("url") or ""):
            return False
        e["url"] = url
        e["status"] = "unknown"
        e["reason"] = "discovered"
        return True
    links.append({"store": "prh", "label": "Penguin", "url": url,
                  "status": "unknown", "reason": "discovered"})
    return True


# ---------------------------------------------------------------------------
# Book-page parsing (patterns verified live against PRH pages, 2026-11-14/15)
# ---------------------------------------------------------------------------

def parse_book_page(body, url):
    """Extract structured metadata from a PRH book page. Returns a dict or
    None when any critical field cannot be parsed (caller reports the id
    with less detail)."""
    text = html.unescape(body)

    title_m = re.search(r"<title>(.*?)</title>", body, re.S)
    if not title_m:
        return None
    raw_title = re.sub(r"\s+", " ", html.unescape(title_m.group(1))).strip()
    head = raw_title.split("|")[0].strip()
    if "gundam" not in head.lower():
        return None

    isbn_m = re.search(r":\s*(97[89]\d{10})\b", head)
    if not isbn_m:
        isbn_m = re.search(r'"isbn"\s*:\s*"(97[89]\d{10})"', text)
    if not isbn_m:
        return None
    isbn = isbn_m.group(1)

    # Head format: "Title: ISBN by Author". Strip the author, then the
    # trailing ": ISBN", to leave the title.
    title = head.split(" by ")[0].strip()
    title = re.sub(r":\s*97[89]\d{10}\s*$", "", title).strip()
    if not title:
        return None

    date_m = re.search(r'"datePublished"\s*:\s*"(\d{4}-\d{2}-\d{2})"', text)
    if not date_m:
        return None
    pub_date = date_m.group(1)

    pages_m = re.search(r'"numberOfPages"\s*:\s*"?(\d+)"?', text)
    if not pages_m:
        pages_m = re.search(r"(\d+)\s+Pages", text)
    if not pages_m:
        return None
    pages = int(pages_m.group(1))

    avail_m = re.search(
        r'<meta[^>]+property="og:availability"[^>]+content="([^"]+)"', body
    )
    availability = avail_m.group(1).lower() if avail_m else ""
    if not availability:
        avail_m = re.search(r'"availability"\s*:\s*"([^"]+)"', text)
        availability = avail_m.group(1).lower() if avail_m else ""

    creators = []
    author_m = re.search(r"\bby\s+(.+?)\s*$", head)
    if author_m:
        # Heads come in two orders: "TITLE: ISBN by AUTHOR" and
        # "TITLE by AUTHOR: ISBN". Strip a trailing ": ISBN" from the
        # captured author when the second order is used.
        author = re.sub(r":\s*97[89]\d{10}\s*$", "", author_m.group(1)).strip()
        if author:
            creators.append(author)
    ill_m = re.search(r"Illustrated by\s+([^\"<\n]{3,150})", text)
    if ill_m:
        for part in re.split(r",\s*|\s+and\s+", ill_m.group(1)):
            name = part.strip().rstrip(".")
            if name and "<" not in name and "http" not in name:
                creators.append(name)
    # Creators are best-effort: some PRH pages omit the author from the
    # <title> entirely (e.g. Origin Vols. 2, 7, 8). ISBN + date + pages
    # already gate the parse, and creators are only used in candidate
    # reports, never written to the catalog.

    return {
        "title": title,
        "isbn": isbn,
        "date": pub_date,
        "pages": pages,
        "availability": availability,
        "creators": ", ".join(creators),
    }


def parse_indigo_page(body):
    """Extract Published date + ISBN from an Indigo product page. Returns
    {"date": "YYYY-MM-DD", "isbn": "..."} or None when either field cannot
    be parsed (caller skips the entry). Patterns verified live against
    Indigo pages, 2026-11-23."""
    date_m = re.search(
        r"Published date:\s*<span[^>]*>([A-Z][a-z]{2} \d{1,2}, \d{4})</span>", body
    )
    isbn_m = re.search(r"ISBN:\s*<span[^>]*>(97[89]\d{10})</span>", body)
    if not date_m or not isbn_m:
        return None
    try:
        pub_date = datetime.strptime(date_m.group(1), "%b %d, %Y").date().isoformat()
    except ValueError:
        return None
    return {"date": pub_date, "isbn": isbn_m.group(1)}


def status_from(availability, pub_date, today):
    if availability == "preorder":
        return "Upcoming"
    if availability == "instock":
        return "Released" if pub_date <= today else "Upcoming"
    return "Upcoming" if pub_date > today else "Released"


def enrich(prh_id, sitemap_url, delay):
    """Fetch one book page and build a candidate record. Returns
    (candidate, canonical_url), or (None, None) when the page cannot be
    parsed (the id retries tomorrow). A 429 propagates so the whole run
    backs off."""
    time.sleep(delay)
    try:
        status, body = fetch(sitemap_url)
    except RateLimited:
        raise
    except AbortDiscovery as e:
        print(f"  enrich {prh_id}: {e}; skipping this run")
        return None, None
    if status != 200:
        print(f"  enrich {prh_id}: HTTP {status}; skipping this run")
        return None, None
    meta = parse_book_page(body, sitemap_url)
    if not meta:
        print(f"  enrich {prh_id}: unparseable page; skipping this run")
        return None, None

    slug = BOOK_RE.search(sitemap_url).group(3)
    canonical = f"{PRH}/books/{prh_id}/{slug}/{meta['isbn']}/"
    page_url = canonical
    time.sleep(delay)
    try:
        v_status, v_body = fetch(canonical)
        if v_status != 200 or meta["isbn"] not in v_body:
            page_url = sitemap_url  # checker re-verifies the base URL
            print(f"  enrich {prh_id}: canonical ISBN URL unverified; keeping base URL")
    except RateLimited:
        raise
    except AbortDiscovery:
        page_url = sitemap_url
        print(f"  enrich {prh_id}: canonical check failed; keeping base URL")

    today = date.today().isoformat()
    candidate = {
        "prh_id": prh_id,
        "url": page_url,
        "title": meta["title"],
        "isbn": meta["isbn"],
        "date": meta["date"],
        "pages": meta["pages"],
        "creators": meta["creators"],
        "status": status_from(meta["availability"], meta["date"], today),
    }
    return candidate, page_url


# ---------------------------------------------------------------------------
# Transitions for Upcoming entries
# ---------------------------------------------------------------------------

def refresh_upcoming(catalog, known, delay, dry_run):
    """Re-read live PRH pages for Upcoming entries with canonical URLs.
    Updates date/status/page-count from PRH-verified values; reports ISBN
    diffs without writing them. Returns a list of change descriptions."""
    by_prh = {v: k for k, v in known.items()}
    changes = []
    today = date.today().isoformat()
    for item in catalog:
        if item.get("status") != "Upcoming":
            continue
        prh_id = by_prh.get(item["id"])
        url = ""
        if prh_id:
            for entry in item.get("links") or []:
                if entry.get("store") == "prh":
                    url = entry.get("url") or ""
                    break
        if not url:
            # No PRH page: fall back to the Indigo product page,
            # report-only (never writes). PRH never lists VIZ/Yen/Denpa
            # titles, so without this these entries are never re-read.
            changes.extend(refresh_indigo(item, delay))
            continue
        time.sleep(delay)
        try:
            status, body = fetch(url)
        except RateLimited:
            raise
        except AbortDiscovery as e:
            print(f"  refresh {item['id']}: {e}; skipping")
            continue
        if status != 200:
            print(f"  refresh {item['id']}: HTTP {status}; skipping")
            continue
        meta = parse_book_page(body, url)
        if not meta:
            print(f"  refresh {item['id']}: unparseable page; skipping")
            continue
        if meta["isbn"] != item.get("isbn"):
            changes.append(
                f"ISBN-DIFF {item['id']}: catalog {item.get('isbn')} vs PRH "
                f"{meta['isbn']} (report-only; correction pass pending)"
            )
        new_status = status_from(meta["availability"], meta["date"], today)
        updates = {}
        if meta["date"] != item.get("releaseDate"):
            updates["releaseDate"] = meta["date"]
        if meta["pages"] != item.get("pageCount"):
            updates["pageCount"] = meta["pages"]
        if new_status != item.get("status"):
            updates["status"] = new_status
        # Publisher pins win over PRH-gated updates; re-assert a pin if
        # the catalog has drifted from the publisher-verified value.
        for field, pinned in PINNED.get(item["id"], {}).items():
            if field in updates:
                if updates[field] != pinned:
                    changes.append(
                        f"PIN-HOLD {item['id']}: {field} kept at publisher-verified "
                        f"{pinned} (PRH says {updates[field]})"
                    )
                del updates[field]
            elif item.get(field) != pinned:
                updates[field] = pinned
        if updates:
            desc = ", ".join(f"{k}: {item.get(k)} -> {v}" for k, v in updates.items())
            changes.append(f"UPDATE {item['id']}: {desc}")
            if not dry_run:
                item.update(updates)
    return changes


def refresh_indigo(item, delay):
    """Re-read the Indigo product page for an Upcoming entry with no PRH
    page. Report-only: date/ISBN mismatches are logged, never written.
    A pinned date suppresses the known-outlier diff. Returns a list of
    change descriptions."""
    url = ""
    for entry in item.get("links") or []:
        if entry.get("store") == "indigo":
            url = entry.get("url") or ""
            break
    if not url:
        return []
    time.sleep(delay)
    try:
        status, body = fetch(url)
    except RateLimited:
        raise
    except AbortDiscovery as e:
        print(f"  refresh {item['id']}: {e}; skipping")
        return []
    if status != 200:
        print(f"  refresh {item['id']}: HTTP {status}; skipping")
        return []
    meta = parse_indigo_page(body)
    if not meta:
        print(f"  refresh {item['id']}: unparseable Indigo page; skipping")
        return []
    changes = []
    if meta["isbn"] != item.get("isbn"):
        changes.append(
            f"ISBN-DIFF {item['id']}: catalog {item.get('isbn')} vs Indigo "
            f"{meta['isbn']} (report-only; correction pass pending)"
        )
    cat_date = str(item.get("releaseDate") or "")
    # Compare at the catalog's precision: a month-precision date matches
    # any day in the same month.
    same = meta["date"][:7] == cat_date if len(cat_date) == 7 else meta["date"] == cat_date
    if not same:
        pinned = PINNED.get(item["id"], {}).get("releaseDate")
        if pinned is None or cat_date != pinned:
            changes.append(
                f"INDIGO-DIFF {item['id']}: catalog {cat_date} vs Indigo "
                f"{meta['date']} (report-only; correction pass pending)"
            )
    return changes


# ---------------------------------------------------------------------------
# Series-page watch (report-only)
# ---------------------------------------------------------------------------

def watch_series_pages(catalog, delay):
    """Compare the highest volume number on each publisher series page
    against the catalog. Prints WATCH lines; never writes anything."""
    catalog_max = {}
    for item in catalog:
        vol = item.get("volume")
        if isinstance(vol, int):
            catalog_max[item["seriesId"]] = max(catalog_max.get(item["seriesId"], 0), vol)
    for series_id, url, pattern in WATCH_SOURCES:
        time.sleep(delay)
        try:
            status, body = fetch(url)
        except AbortDiscovery as e:
            print(f"  WATCH {series_id}: fetch failed ({e}); skipping")
            continue
        if status != 200:
            print(f"  WATCH {series_id}: HTTP {status}; skipping")
            continue
        nums = [int(n) for n in pattern.findall(body)]
        if not nums:
            print(f"  WATCH {series_id}: no volume numbers found on page; skipping")
            continue
        page_max = max(nums)
        known_max = catalog_max.get(series_id, 0)
        if page_max > known_max:
            print(f"  WATCH CANDIDATE {series_id}: series page shows Vol. {page_max}, "
                  f"catalog max is Vol. {known_max} -- verify before adding")
        else:
            print(f"  WATCH OK {series_id}: page max Vol. {page_max}, catalog max Vol. {known_max}")


# ---------------------------------------------------------------------------
# Validation + write
# ---------------------------------------------------------------------------

def validate(old_ids, catalog):
    problems = []
    try:
        json.dumps(catalog)
    except (TypeError, ValueError) as e:
        problems.append(f"catalog not JSON-serializable: {e}")
    ids = [x.get("id") for x in catalog]
    if len(ids) != len(set(ids)):
        problems.append("duplicate ids present")
    missing = old_ids - set(ids)
    if missing:
        problems.append(f"entries removed: {sorted(missing)}")
    for item in catalog:
        for field in REQUIRED_FIELDS:
            if item.get(field) in (None, ""):
                problems.append(f"{item.get('id')}: empty required field {field}")
        if not isinstance(item.get("links"), list):
            problems.append(f"{item.get('id')}: links is not an array")
    return problems


def atomic_write(catalog):
    tmp = DATA_FILE + ".tmp-discovery"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(catalog, f, indent=2, ensure_ascii=False)
    os.replace(tmp, DATA_FILE)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Discover new Gundam manga volumes from PRH sitemaps and publisher series pages.")
    parser.add_argument("--dry-run", action="store_true", help="Report what would happen; write nothing.")
    parser.add_argument("--delay", type=float, default=1.5, help="Seconds between page fetches.")
    args = parser.parse_args()

    catalog = load_catalog()
    old_ids = {x["id"] for x in catalog}
    old_count = len(catalog)

    attached, candidates = [], []
    changes = []
    try:
        sitemap_ids = enumerate_sitemap_ids(catalog, args.delay)
    except AbortDiscovery as e:
        print(f"sitemap scan aborted (catalog untouched): {e}")
        sitemap_ids = None
    if sitemap_ids is not None:
        print(f"sitemap enumeration: {len(sitemap_ids)} gundam pages")
        known = known_prh_ids(catalog)
        new_ids = sorted(set(sitemap_ids) - set(known))
        print(f"known PRH ids in catalog: {len(known)}; new ids: {len(new_ids)}")

        isbn_map = {item.get("isbn"): item for item in catalog if item.get("isbn")}
        title_map = title_map_without_prh(catalog, known)
        try:
            for prh_id in new_ids:
                sitemap_url = sitemap_ids[prh_id]
                candidate, page_url = enrich(prh_id, sitemap_url, args.delay)
                if candidate is None:
                    continue
                existing = isbn_map.get(candidate["isbn"])
                if existing is None:
                    existing = title_map.get(normalize_title(candidate["title"]))
                if existing is not None:
                    # The volume is already catalogued without a PRH page:
                    # attach the URL instead of reporting a duplicate.
                    # ISBN diffs stay report-only.
                    if candidate["isbn"] != existing.get("isbn"):
                        print(f"  ISBN-DIFF {existing['id']}: catalog {existing.get('isbn')} "
                              f"vs PRH {candidate['isbn']} (report-only; correction pass pending)")
                    if not args.dry_run:
                        changed = attach_prh_url(existing, page_url)
                    else:
                        changed = True
                    if changed:
                        attached.append(existing["id"])
                        print(f"  {'would attach' if args.dry_run else 'attached'} PRH page to existing entry: {existing['id']}")
                    continue
                candidates.append(candidate)
                print(f"  CANDIDATE (verify before adding): {candidate['title']} | "
                      f"ISBN {candidate['isbn']} | {candidate['date']} | "
                      f"{candidate['pages']}pp | {candidate['status']} | "
                      f"{candidate['creators']} | {candidate['url']}")

            changes = refresh_upcoming(catalog, known, args.delay, args.dry_run)
        except AbortDiscovery as e:
            print(f"discovery aborted mid-run (catalog untouched): {e}")
            return 0
        for line in changes:
            print(f"  {line}")

    # The series-page watch runs independently of the PRH scan.
    try:
        watch_series_pages(catalog, args.delay)
    except AbortDiscovery as e:
        print(f"series-page watch aborted: {e}")

    if args.dry_run:
        print(f"dry run: {len(attached)} attached, {len(candidates)} candidates, "
              f"{len(changes)} updates reported; nothing written")
        return 0

    if attached or changes:
        problems = validate(old_ids, catalog)
        if problems:
            print("VALIDATION FAILED; catalog untouched:")
            for p in problems:
                print(f"  - {p}")
            return 0
        atomic_write(catalog)
    print(f"discovery: {len(attached)} attached, {len(candidates)} candidates, "
          f"{len(changes)} updates; {len(catalog)} volumes total (was {old_count})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
