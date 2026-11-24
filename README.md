![Gundam Media Hub logo](gmh-logo.png)

# Gundam Media Hub

Buyable Gundam media across North America — manga, video games, and Blu-ray / DVD / 4K discs you can actually buy online in the USA, Canada, and Mexico, with buy links verified daily.

Live site: https://gundam-media-hub.vercel.app

## Features

- **Three catalogs:** English-language manga volumes (7 series), video games, and discs — one entry per item, with release facts from publisher and distributor pages first.
- **Landing page:** pick a shelf — Manga, Video Games, or Discs — then browse flip cards with the product image and brief info; selecting a card opens full details and buy links.
- **Flexible buy links:** each item links to whichever stores carry it — Amazon, Indigo, VIZ, Crunchyroll, and more — verified daily; a store's link is hidden when the item is confirmed unavailable there (see `check_buy_links.py`). Crunchyroll Store links are labelled subscriber-only (Mega/Ultimate membership required to shop). Amazon links are title searches, never stock evidence.
- **Checker-gated visibility:** a card shows when the item is confirmed in stock; upcoming items show with an Upcoming badge; unknown/inconclusive items show unless confirmed unavailable; a card hides only after 2 consecutive daily checks find it unavailable everywhere (per-entry `availability` + `unavailableStreak` in the JSON).
- **Country-dynamic links:** Amazon links resolve to amazon.com, amazon.ca, or amazon.com.mx by visitor country; other stores keep their stored URLs.
- **Filters & search:** filter by series / platform / format or Released/Upcoming, and search by title, creator, publisher, or ISBN/UPC.
- **Cover art backgrounds:** manga cards and the detail view show the volume's official cover art, cropped to its art band and blurred behind the text. Games and discs show a styled placeholder until verified artwork URLs exist.
- **Daily automated updates:** new manga volumes are detected and all three catalogs' buy links re-verified every day (see `.github/workflows/update-solicitations.yml`).

## Data

Catalog data lives in `data/` — one JSON file per media type, one entry per item:

- `data/gundam_manga.json` — manga volumes (series, ISBN, publisher, cover art).
- `data/gundam_games.json` — video games (platforms, model, format).
- `data/gundam_discs.json` — discs (format, distributor, UPC, region).

Scope rules: no out-of-print, no imports (only natively North-American releases), no dead or delisted games, no tabletop. The DVD shelf launches empty — no DVD title is currently verified in stock — and fills automatically when the daily check confirms one. Facts flagged unverified in research stay flagged as `unverified` notes on the entry until a primary source confirms them.

Sister site: [DC Compact Comics dashboard](https://dc-compact-live-dashboard.vercel.app).
