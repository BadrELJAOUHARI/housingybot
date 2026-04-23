"""Student Experience studio watcher.

Scrapes studentexperience.com for available long-stay studios at the 5 Dutch
locations (Amsterdam Amstel, NDSM, Zuidas, Minervahaven, Leiden) and sends
a Telegram alert the moment a new one appears.

State is kept in seen.json and committed back to the repo by GitHub Actions.
No Google Sheets, no database.
"""
from __future__ import annotations
import json
import os
import re
import sys
from pathlib import Path
import requests
from bs4 import BeautifulSoup

# ---------- config ----------
# The main long-stay listings page shows all Dutch locations in one place.
URL = "https://studentexperience.com/studios?los=longstay"

# Only alert for studios at these locations (match on listing text, case-insensitive).
# Add/remove as needed. Matching is a substring check.
WANTED_LOCATIONS = [
    "amsterdam amstel",
    "amsterdam ndsm",
    "amsterdam zuidas",
    "amsterdam minervahaven",
    "leiden",
]

SEEN_FILE = Path("seen.json")

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9,nl;q=0.8",
}


# ---------- scraping ----------
def fetch_html(url: str) -> str | None:
    try:
        r = requests.get(url, headers=HEADERS, timeout=20)
        if r.status_code == 200:
            return r.text
        print(f"HTTP {r.status_code} on {url}")
    except requests.RequestException as e:
        print(f"Request failed: {e}")
    return None


def parse_studios(html: str) -> list[dict]:
    """Return list of studio dicts: {id, url, title, location, price, size}.

    Strategy: the site is Next.js — all page data sits inside
    <script id="__NEXT_DATA__">{...JSON...}</script>. We parse that first
    because it's far more stable than CSS selectors.
    Fallback: scrape HTML cards if the JSON shape changes.
    """
    soup = BeautifulSoup(html, "html.parser")

    # --- Preferred: parse __NEXT_DATA__ JSON ---
    next_data_tag = soup.find("script", id="__NEXT_DATA__")
    if next_data_tag and next_data_tag.string:
        try:
            data = json.loads(next_data_tag.string)
            studios = _extract_from_next_data(data)
            if studios:
                return studios
        except (json.JSONDecodeError, TypeError) as e:
            print(f"__NEXT_DATA__ parse error: {e}")

    # --- Fallback: CSS/HTML parsing ---
    return _extract_from_html(soup)


def _extract_from_next_data(data: dict) -> list[dict]:
    """Walk the Next.js page props JSON looking for studio listing arrays."""
    studios: list[dict] = []

    def walk(node, path=""):
        if isinstance(node, dict):
            # Heuristic: a studio listing usually has price + location + id/url
            keys = set(node.keys())
            looks_like_studio = (
                ("price" in keys or "rent" in keys or "totalPrice" in keys)
                and ("location" in keys or "locationName" in keys or "complex" in keys)
            )
            if looks_like_studio:
                studios.append(_normalize(node))
                return
            for k, v in node.items():
                walk(v, f"{path}.{k}")
        elif isinstance(node, list):
            for i, item in enumerate(node):
                walk(item, f"{path}[{i}]")

    walk(data)
    # De-dup by id
    seen_ids = set()
    unique = []
    for s in studios:
        if s["id"] in seen_ids:
            continue
        seen_ids.add(s["id"])
        unique.append(s)
    return unique


def _normalize(node: dict) -> dict:
    """Map a raw JSON node to our studio dict."""
    sid = str(node.get("id") or node.get("studioId") or node.get("slug") or "")
    slug = node.get("slug") or node.get("url") or ""
    url = slug if str(slug).startswith("http") else (
        f"https://studentexperience.com/studios/{slug or sid}" if sid or slug else URL
    )

    # Location can be nested or flat
    loc = node.get("location") or node.get("locationName") or node.get("complex") or ""
    if isinstance(loc, dict):
        loc = loc.get("name") or loc.get("title") or ""

    price = node.get("totalPrice") or node.get("price") or node.get("rent") or None
    if isinstance(price, dict):
        price = price.get("amount") or price.get("value")

    size = node.get("size") or node.get("floorSize") or node.get("surface") or None
    if isinstance(size, dict):
        size = size.get("value")

    title = (
        node.get("title")
        or node.get("name")
        or node.get("studioType")
        or f"Studio at {loc}"
    )

    return {
        "id": sid or str(hash(str(node)))[:12],
        "url": url,
        "title": str(title),
        "location": str(loc),
        "price": price,
        "size": size,
    }


def _extract_from_html(soup: BeautifulSoup) -> list[dict]:
    """Fallback CSS parsing. Looks for anchor tags that link to studio pages."""
    studios = []
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if "/studios/" not in href or href.endswith("/studios"):
            continue
        if href.startswith("/"):
            href = "https://studentexperience.com" + href

        card_text = a.get_text(" ", strip=True)
        # Try to extract price (€ number)
        price_m = re.search(r"€\s*([\d.]+)", card_text)
        price = int(price_m.group(1).replace(".", "")) if price_m else None
        # Try to extract size (number m²)
        size_m = re.search(r"(\d{1,3})\s*m[²2]", card_text)
        size = int(size_m.group(1)) if size_m else None

        # Guess location from card text
        loc = ""
        lower = card_text.lower()
        for cand in WANTED_LOCATIONS:
            if cand in lower:
                loc = cand.title()
                break

        sid = href.rstrip("/").split("/")[-1] or href
        studios.append({
            "id": sid,
            "url": href,
            "title": card_text[:120] or "Studio",
            "location": loc,
            "price": price,
            "size": size,
        })

    # De-dup by id
    seen_ids, unique = set(), []
    for s in studios:
        if s["id"] in seen_ids:
            continue
        seen_ids.add(s["id"])
        unique.append(s)
    return unique


def wanted(studio: dict) -> bool:
    """Keep only studios at the Dutch locations we care about."""
    text = f"{studio.get('location','')} {studio.get('title','')}".lower()
    return any(loc in text for loc in WANTED_LOCATIONS)


# ---------- state ----------
def load_seen() -> set[str]:
    if not SEEN_FILE.exists():
        return set()
    try:
        return set(json.loads(SEEN_FILE.read_text()))
    except (json.JSONDecodeError, OSError):
        return set()


def save_seen(seen: set[str]) -> None:
    SEEN_FILE.write_text(json.dumps(sorted(seen), indent=2))


# ---------- notifier ----------
def send_telegram(text: str) -> None:
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not (token and chat_id):
        print("Telegram not configured; skipping send.")
        return
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            data={
                "chat_id": chat_id,
                "text": text[:4000],
                "parse_mode": "HTML",
                "disable_web_page_preview": "false",
            },
            timeout=10,
        )
        if r.status_code != 200:
            print(f"Telegram send failed: {r.status_code} {r.text[:200]}")
    except requests.RequestException as e:
        print(f"Telegram error: {e}")


def format_alert(s: dict) -> str:
    price = f"€{s['price']}" if s.get("price") else "€?"
    size = f" · {s['size']}m²" if s.get("size") else ""
    loc = s.get("location") or "Student Experience"
    return (
        f"🏠 <b>NEW STUDIO</b>\n"
        f"{price}{size} · {loc}\n"
        f"{s.get('title','')[:120]}\n"
        f"{s['url']}"
    )


# ---------- main ----------
def main() -> int:
    html = fetch_html(URL)
    if html is None:
        print("Could not fetch page; exiting without changes.")
        return 0  # do NOT error out — we want the workflow to succeed and retry

    all_studios = parse_studios(html)
    dutch = [s for s in all_studios if wanted(s)]
    print(f"Parsed {len(all_studios)} studios total, {len(dutch)} match wanted locations.")

    seen = load_seen()
    new = [s for s in dutch if s["id"] not in seen]
    print(f"New since last run: {len(new)}")

    if new:
        # Send ONE message per new studio so Telegram previews each URL nicely
        for s in new:
            send_telegram(format_alert(s))
        # Update state
        for s in new:
            seen.add(s["id"])
        save_seen(seen)
    else:
        # Still write the file so it exists on first run
        if not SEEN_FILE.exists():
            save_seen(seen)

    return 0


if __name__ == "__main__":
    sys.exit(main())
