"""Permission-first monitoring of publicly accessible listing pages.

No target is configured by default. One run makes at most one listing-page
request after checking robots.txt; no continuous polling or bypass features.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
from hashlib import sha256
from html import unescape
import ipaddress
import json
import os
from pathlib import Path
import sys
from typing import Any
from urllib.parse import urljoin, urlsplit, urldefrag
from urllib.robotparser import RobotFileParser

import requests
from bs4 import BeautifulSoup

USER_AGENT = "HousingyBot/2.0 (+https://github.com/BadrELJAOUHARI/housingybot)"
MAX_PAGE_BYTES = 2_000_000
MAX_ROBOTS_BYTES = 250_000
MAX_ITEMS = 100
MINIMUM_DELAY_SECONDS = 3600
TIMEOUT = (5, 15)
PERMISSION_BASES = {"written-permission", "published-policy-explicitly-permits-automation"}


class MonitorError(Exception):
    """A request or configuration was blocked safely."""


def public_https_url(url: str) -> str:
    """Require an HTTPS URL, a public-looking hostname, and no URL credentials."""
    if not isinstance(url, str):
        raise MonitorError("Website URLs must be strings")
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    if parts.scheme != "https" or not host or parts.username or parts.password:
        raise MonitorError("Only public HTTPS URLs without credentials are supported")
    if host in {"localhost"} or host.endswith((".local", ".internal", ".example")):
        raise MonitorError("Replace the example URL with an approved public site")
    try:
        ip = ipaddress.ip_address(host.strip("[]"))
    except ValueError:
        ip = None
    if ip and not ip.is_global:
        raise MonitorError("Private or non-public IP destinations are not allowed")
    if not host or "." not in host:
        raise MonitorError("Use a public fully qualified hostname")
    return url


def load_config(path: Path) -> dict[str, Any]:
    try:
        config = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise MonitorError(f"Could not load configuration ({exc})") from exc
    website = config.get("website")
    selectors = config.get("selectors")
    if not isinstance(website, dict) or not isinstance(selectors, dict):
        raise MonitorError("Configuration needs website and selectors objects")
    url = public_https_url(website.get("listings_url"))
    if website.get("permission_confirmed") is not True:
        raise MonitorError("Permission not confirmed: see the authorization checklist in README.md")
    if website.get("permission_basis") not in PERMISSION_BASES:
        raise MonitorError("Permission basis must be written permission or a published policy explicitly allowing automation")
    evidence = website.get("permission_reference", "")
    if not isinstance(evidence, str) or len(evidence.strip()) < 8:
        raise MonitorError("Provide a reference to the permission document or published policy")
    if website["permission_basis"] == "published-policy-explicitly-permits-automation":
        public_https_url(evidence)
    for field in ("item", "title", "link"):
        if not isinstance(selectors.get(field), str) or not selectors[field].strip():
            raise MonitorError(f"Missing CSS selector: {field}")
    for field in ("price", "location"):
        if selectors.get(field) is not None and not isinstance(selectors[field], str):
            raise MonitorError(f"Invalid optional CSS selector: {field}")
    filters = config.get("filter_keywords", [])
    if not isinstance(filters, list) or not all(isinstance(x, str) for x in filters):
        raise MonitorError("filter_keywords must be a list of strings")
    delay = config.get("minimum_interval_seconds", MINIMUM_DELAY_SECONDS)
    if isinstance(delay, bool) or not isinstance(delay, int) or delay < MINIMUM_DELAY_SECONDS:
        raise MonitorError(f"minimum_interval_seconds must be at least {MINIMUM_DELAY_SECONDS}")
    config["website"]["listings_url"] = url
    return config


def _limited_get(session: requests.Session, url: str, max_bytes: int) -> tuple[int, str, str]:
    try:
        with session.get(
            url,
            headers={"User-Agent": USER_AGENT, "Accept": "text/html,text/plain;q=0.9"},
            timeout=TIMEOUT,
            allow_redirects=False,
            stream=True,
        ) as response:
            if 300 <= response.status_code < 400:
                raise MonitorError("Redirect encountered; manually review the destination and its permission")
            chunks, used = [], 0
            for chunk in response.iter_content(chunk_size=32768):
                used += len(chunk)
                if used > max_bytes:
                    raise MonitorError("Response exceeded configured size limit")
                chunks.append(chunk)
            data = b"".join(chunks)
            return response.status_code, data.decode(response.encoding or "utf-8", errors="replace"), response.headers.get("Content-Type", "")
    except requests.RequestException as exc:
        raise MonitorError(f"Network request failed: {exc.__class__.__name__}") from exc


def check_robots(session: requests.Session, url: str) -> None:
    """Use robots instructions as a *technical* rule, never legal permission."""
    parts = urlsplit(url)
    robots_url = f"{parts.scheme}://{parts.netloc}/robots.txt"
    status, text, _ = _limited_get(session, robots_url, MAX_ROBOTS_BYTES)
    if status == 404:
        return  # no robots rules published; independent permission is still required
    if status != 200:
        raise MonitorError(f"Cannot establish robots.txt permission (HTTP {status}); stopping")
    parser = RobotFileParser()
    parser.parse(text.splitlines())
    if not parser.can_fetch(USER_AGENT, url):
        raise MonitorError("robots.txt disallows the requested path for this user-agent")


def fetch_listings_html(session: requests.Session, url: str) -> str:
    status, text, content_type = _limited_get(session, url, MAX_PAGE_BYTES)
    if status in (401, 403, 429):
        raise MonitorError(f"Source declined access (HTTP {status}); do not bypass the restriction")
    if status != 200:
        raise MonitorError(f"Listings fetch failed (HTTP {status})")
    if "html" not in content_type.lower():
        raise MonitorError("Expected HTML, got a different response type")
    return text


def _extract(node: Any, selector: str | None) -> str:
    if not selector:
        return ""
    match = node.select_one(selector)
    return unescape(match.get_text(" ", strip=True)) if match else ""


def parse_listings(html: str, config: dict[str, Any]) -> list[dict[str, str]]:
    """Extract minimal listing metadata with site-provided CSS selectors."""
    source_url = config["website"]["listings_url"]
    source_host = urlsplit(source_url).netloc.lower()
    selectors = config["selectors"]
    soup = BeautifulSoup(html, "html.parser")
    cards = soup.select(selectors["item"])
    filters = [w.casefold().strip() for w in config.get("filter_keywords", []) if w.strip()]
    listings, seen = [], set()
    for card in cards[:MAX_ITEMS]:
        anchor = card.select_one(selectors["link"])
        href = anchor.get("href", "") if anchor else ""
        if not isinstance(href, str) or not href:
            continue
        link = urldefrag(urljoin(source_url, href))[0]
        parsed = urlsplit(link)
        if parsed.scheme != "https" or parsed.netloc.lower() != source_host:
            continue
        title = _extract(card, selectors["title"])
        if not title:
            continue
        location = _extract(card, selectors.get("location"))
        if filters and not any(word in (title + " " + location).casefold() for word in filters):
            continue
        identifier = sha256(link.encode("utf-8")).hexdigest()[:20]
        if identifier in seen:
            continue
        seen.add(identifier)
        listings.append({
            "id": identifier,
            "title": title[:200],
            "url": link,
            "price": _extract(card, selectors.get("price"))[:80],
            "location": location[:120],
        })
    return listings


def load_state(path: Path, fingerprint: str) -> dict[str, Any]:
    if not path.exists():
        return {"source_fingerprint": fingerprint, "last_checked_at": None, "seen_ids": []}
    try:
        saved = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise MonitorError(f"Could not parse state file; refusing to reset alert history: {exc}") from exc
    if isinstance(saved, list):
        return {"source_fingerprint": fingerprint, "last_checked_at": None, "seen_ids": []}
    if not isinstance(saved, dict):
        raise MonitorError("Unrecognized state format")
    if saved.get("source_fingerprint") != fingerprint:
        return {"source_fingerprint": fingerprint, "last_checked_at": None, "seen_ids": []}
    if not isinstance(saved.get("seen_ids"), list):
        raise MonitorError("Invalid seen_ids format")
    return saved


def save_state(path: Path, state: dict[str, Any]) -> None:
    path.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def telegram_alert(session: requests.Session, listing: dict[str, str], token: str, chat_id: str) -> None:
    parts = ["New listing", listing["title"]]
    if listing.get("location"):
        parts.append(listing["location"])
    if listing.get("price"):
        parts.append(listing["price"])
    parts.append(listing["url"])
    try:
        response = session.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            data={"chat_id": chat_id, "text": "\n".join(parts), "disable_web_page_preview": "true"},
            timeout=TIMEOUT,
        )
        response.raise_for_status()
        if not response.json().get("ok", False):
            raise MonitorError("Telegram declined the notification")
    except (requests.RequestException, ValueError) as exc:
        raise MonitorError("Telegram notification failed; the listing remains unseen") from exc


def run(config_path: Path, state_path: Path, dry_run: bool = False, session: requests.Session | None = None) -> int:
    config = load_config(config_path)
    website_url = config["website"]["listings_url"]
    fingerprint = sha256(website_url.encode("utf-8")).hexdigest()[:24]
    state = load_state(state_path, fingerprint)
    now = datetime.now(timezone.utc)
    if state.get("last_checked_at"):
        try:
            previous = datetime.fromisoformat(state["last_checked_at"])
            if previous.tzinfo is None:
                raise ValueError("Missing timezone")
        except (TypeError, ValueError) as exc:
            raise MonitorError("Invalid timestamp in monitoring state") from exc
        elapsed = (now - previous).total_seconds()
        if elapsed < config.get("minimum_interval_seconds", MINIMUM_DELAY_SECONDS):
            print("Rate limit: next check is not yet due")
            return 0
    token = os.getenv("TELEGRAM_BOT_TOKEN", "")
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "")
    if not dry_run and (not token or not chat_id):
        raise MonitorError("Set Telegram credentials or use --dry-run")
    client = session or requests.Session()
    check_robots(client, website_url)
    html = fetch_listings_html(client, website_url)
    listings = parse_listings(html, config)
    unseen = [l for l in listings if l["id"] not in set(state.get("seen_ids", []))]
    print(f"Parsed {len(listings)} listings, {len(unseen)} new")
    state["last_checked_at"] = now.isoformat()
    save_state(state_path, state)
    if dry_run:
        print("Preview: no alerts or seen IDs saved; last check time recorded")
        for listing in unseen[:5]:
            print(f"- {listing['title']} — {listing['url']}")
        return 0
    for listing in unseen:
        telegram_alert(client, listing, token, chat_id)
        state["seen_ids"].append(listing["id"])
        save_state(state_path, state)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=Path("config.json"))
    parser.add_argument("--state", type=Path, default=Path("seen.json"))
    parser.add_argument("--dry-run", action="store_true", help="Do not send notifications or change state")
    args = parser.parse_args()
    try:
        return run(args.config, args.state, dry_run=args.dry_run)
    except MonitorError as exc:
        print(f"Stopped: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
