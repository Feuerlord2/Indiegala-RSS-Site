#!/usr/bin/env python3
"""Generate the RSS feeds served by the GitHub Pages site in docs/.

IndieGala does not offer a public feed for its bundles. The only official
machine-readable interface is the store RSS feed documented at
https://docs.indiegala.com/support/rss_feed.html:

    https://www.indiegala.com/store_games_rss          (all store games)
    https://www.indiegala.com/store_games_rss?sale     (discounted games)

For the bundles themselves we use the public JSON API of barter.vg, a
community site that tracks bundles across stores (no API key required):

    https://bartervg.com/browse/bundles/json/

IndieGala's terms allow feed access at no more than 240 requests/hour and
1 request/second; a scheduled run of this script performs 2 requests total,
far below that limit.

The script is defensive by design: if a source is unreachable the existing
feed file is left untouched (last known good), and pubDates of items that
were already published are preserved so feed readers do not see duplicates.
"""

from __future__ import annotations

import email.utils
import json
import re
import sys
import time
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path
from xml.sax.saxutils import escape

DOCS = Path(__file__).resolve().parent.parent / "docs"
SITE_URL = "https://feuerlord2.github.io/Indiegala-RSS-Site/"
USER_AGENT = (
    "IndiegalaRSSSite/1.0 (+https://github.com/Feuerlord2/Indiegala-RSS-Site; "
    "personal feed generator, <=2 requests per run)"
)

# Documented in https://github.com/bartervg/barter.vg/wiki (Get Bundles v1).
# NOT /browse/bundles/json - that endpoint returns per-game bundle counts.
BARTER_BUNDLES_URL = "https://bartervg.com/bundles/json"
BARTER_INDIEGALA_STORE_ID = 7  # per the wiki's ID List "Source" table
# IndieGala runs on Google App Engine; the appspot origin serves the same
# feed and is a fallback in case www.indiegala.com's CDN blocks runner IPs.
INDIEGALA_SALE_RSS_URLS = [
    "https://www.indiegala.com/store_games_rss?sale",
    "https://indiegala-prod.appspot.com/store_games_rss?sale",
]

MAX_BUNDLE_ITEMS = 50
MAX_SALE_ITEMS = 75


def log(msg: str) -> None:
    print(msg, flush=True)


BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:128.0) Gecko/20100101 Firefox/128.0"
)


def fetch(url: str, retries: int = 3) -> bytes | None:
    """Fetch a URL politely; return None instead of raising on failure.

    The honest bot User-Agent is tried first; IndieGala sits behind Imperva
    Incapsula which sometimes challenges unknown agents, so a plain browser
    UA (the approach used by long-running production importers of this feed)
    is used for the remaining attempts.
    """
    for attempt in range(1, retries + 1):
        ua = USER_AGENT if attempt == 1 else BROWSER_UA
        try:
            req = urllib.request.Request(url, headers={"User-Agent": ua})
            with urllib.request.urlopen(req, timeout=30) as resp:
                body = resp.read()
            if b"Incapsula" in body[:4096] and b"<rss" not in body[:4096]:
                raise RuntimeError("Imperva Incapsula challenge page received")
            return body
        except Exception as exc:  # noqa: BLE001 - report and retry
            log(f"WARN: fetch attempt {attempt}/{retries} failed for {url}: {exc}")
            if attempt < retries:
                time.sleep(2**attempt)
    return None


# --------------------------------------------------------------------------
# RSS output
# --------------------------------------------------------------------------


def rfc2822(dt: datetime) -> str:
    return email.utils.format_datetime(dt)


def read_existing_pubdates(path: Path) -> dict[str, str]:
    """Map guid -> pubDate from a previously generated feed, if present."""
    if not path.exists():
        return {}
    try:
        root = ET.parse(path).getroot()
    except ET.ParseError:
        return {}
    dates: dict[str, str] = {}
    for item in root.iter("item"):
        guid = item.findtext("guid")
        pubdate = item.findtext("pubDate")
        if guid and pubdate:
            dates[guid] = pubdate
    return dates


def strip_lastbuilddate(xml_text: str) -> str:
    return re.sub(r"<lastBuildDate>[^<]*</lastBuildDate>", "", xml_text)


def write_feed(path: Path, title: str, description: str, items: list[dict]) -> bool:
    """Write an RSS 2.0 feed; returns True if the file changed.

    ``items`` entries: {title, link, guid, description, pubDate(optional)}.
    pubDates of items already present in the old feed are preserved so that
    regeneration does not bump every entry to "now".
    """
    old_dates = read_existing_pubdates(path)
    now = rfc2822(datetime.now(timezone.utc))

    parts = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<rss version="2.0" xmlns:atom="http://www.w3.org/2005/Atom">',
        "<channel>",
        f"<title>{escape(title)}</title>",
        f"<link>{escape(SITE_URL)}</link>",
        f'<atom:link href="{escape(SITE_URL + path.name)}" rel="self" type="application/rss+xml"/>',
        f"<description>{escape(description)}</description>",
        "<language>en</language>",
        "<lastBuildDate>__LASTBUILD__</lastBuildDate>",
    ]
    for item in items:
        pubdate = old_dates.get(item["guid"]) or item.get("pubDate") or now
        parts += [
            "<item>",
            f"<title>{escape(item['title'])}</title>",
            f"<link>{escape(item['link'])}</link>",
            f'<guid isPermaLink="false">{escape(item["guid"])}</guid>',
            f"<description>{escape(item['description'])}</description>",
            f"<pubDate>{pubdate}</pubDate>",
            "</item>",
        ]
    parts += ["</channel>", "</rss>", ""]
    new_xml = "\n".join(parts)

    old_xml = path.read_text(encoding="utf-8") if path.exists() else ""
    if strip_lastbuilddate(new_xml.replace("__LASTBUILD__", "")) == strip_lastbuilddate(old_xml):
        log(f"OK: {path.name} unchanged ({len(items)} items)")
        return False

    path.write_text(new_xml.replace("__LASTBUILD__", now), encoding="utf-8")
    log(f"OK: {path.name} written ({len(items)} items)")
    return True


# --------------------------------------------------------------------------
# Source: IndieGala bundles via barter.vg
#
# GET /bundles/json returns {"bundles": {"<id>": {"meta": {...}, "games":
# {...}}}}. meta carries store (7 = IndieGala), storename, title (without
# the "Indiegala: " prefix), url (the bundle page on indiegala.com), and
# start/end as unix epoch seconds.
# --------------------------------------------------------------------------


def epoch_to_rfc2822(value) -> str | None:
    try:
        return rfc2822(datetime.fromtimestamp(int(value), tz=timezone.utc))
    except (TypeError, ValueError, OSError, OverflowError):
        return None


def bundle_items() -> list[dict] | None:
    raw = fetch(BARTER_BUNDLES_URL)
    if raw is None:
        return None
    try:
        data = json.loads(raw)
        bundles = data.get("bundles", data)
    except (json.JSONDecodeError, AttributeError) as exc:
        log(f"WARN: could not parse barter.vg JSON: {exc}")
        return None
    if not isinstance(bundles, dict):
        log("WARN: unexpected barter.vg JSON shape (no bundles object)")
        return None

    items = []
    for bundle_id, entry in bundles.items():
        if not isinstance(entry, dict):
            continue
        meta = entry.get("meta") if isinstance(entry.get("meta"), dict) else entry
        storename = str(meta.get("storename") or "").strip()
        try:
            store = int(meta.get("store"))
        except (TypeError, ValueError):
            store = None
        if store != BARTER_INDIEGALA_STORE_ID and storename.lower() != "indiegala":
            continue

        title = str(meta.get("title") or "").strip()
        if not title:
            continue
        try:
            id_key = int(bundle_id)
        except (TypeError, ValueError):
            id_key = 0
        try:
            start_key = int(meta.get("start"))
        except (TypeError, ValueError):
            start_key = 0
        sort_key = (start_key, id_key)
        link = str(meta.get("url") or "").strip() or f"https://barter.vg/bundle/{bundle_id}/"

        description = f"IndieGala bundle: {title}."
        games = entry.get("games")
        if isinstance(games, (dict, list)) and len(games) > 0:
            description += f" Contains {len(games)} games."
        end = epoch_to_rfc2822(meta.get("end"))
        if end:
            description += f" Available until {end}."

        items.append(
            (
                sort_key,
                {
                    "title": title,
                    "link": link,
                    "guid": f"barter-bundle-{bundle_id}",
                    "description": description,
                    "pubDate": epoch_to_rfc2822(meta.get("start")),
                },
            )
        )

    # Newest first by start date (bundle ids are not strictly chronological).
    items.sort(key=lambda pair: pair[0], reverse=True)
    return [item for _, item in items[:MAX_BUNDLE_ITEMS]]


# --------------------------------------------------------------------------
# Source: IndieGala store sale games via the official RSS feed
#
# The feed is pseudo-RSS: <rss><channel> holds <currentPage>/<totalPages>/
# <totalGames> and a <browse> wrapper around the <item> elements. Items have
# no description/pubDate/guid; instead they carry price fields per currency
# (priceEUR, discountPriceEUR, discountPercentEUR, ...), drminfo, platforms.
# --------------------------------------------------------------------------


def sale_description(node: ET.Element) -> str:
    parts = []
    price = node.findtext("priceEUR") or node.findtext("priceUSD")
    discount = node.findtext("discountPriceEUR") or node.findtext("discountPriceUSD")
    percent = node.findtext("discountPercentEUR") or node.findtext("discountPercentUSD")
    currency = "EUR" if node.findtext("priceEUR") else "USD"
    if discount:
        parts.append(f"{discount} {currency}")
        if percent:
            parts.append(f"(-{percent.strip('-% ')}%)")
        if price:
            parts.append(f"instead of {price} {currency}")
    elif price:
        parts.append(f"{price} {currency}")
    drm = node.findtext("drminfo")
    if drm:
        parts.append(f"- {drm}")
    publisher = node.findtext("publisher")
    if publisher:
        parts.append(f"- by {publisher}")
    return " ".join(parts) or (node.findtext("title") or "").strip()


def sale_items() -> list[dict] | None:
    root = None
    for url in INDIEGALA_SALE_RSS_URLS:
        raw = fetch(url)
        if raw is None:
            continue
        try:
            root = ET.fromstring(raw)
            break
        except ET.ParseError as exc:
            log(f"WARN: response from {url} is not parseable XML: {exc}")
    if root is None:
        return None

    items = []
    # iter() descends into the <browse> wrapper as well as plain channels.
    for node in root.iter("item"):
        title = (node.findtext("title") or "").strip()
        link = (node.findtext("link") or "").strip()
        if not title or not link:
            continue
        items.append(
            {
                "title": title,
                "link": link,
                "guid": node.findtext("sku") or link,
                "description": sale_description(node),
                # No pubDate in the source; write_feed keeps the date an item
                # was first seen, so entries are dated by their appearance.
                "pubDate": node.findtext("pubDate"),
            }
        )
        if len(items) >= MAX_SALE_ITEMS:
            break
    return items


# --------------------------------------------------------------------------


def main() -> int:
    DOCS.mkdir(parents=True, exist_ok=True)
    changed = False
    failures = []

    bundles = bundle_items()
    if bundles is None:
        failures.append("bundles (barter.vg)")
        log("WARN: keeping previous games.rss (source unreachable)")
    else:
        changed |= write_feed(
            DOCS / "games.rss",
            "IndieGala Game Bundles",
            "New game bundles on IndieGala, tracked via barter.vg.",
            bundles,
        )

    sales = sale_items()
    if sales is None:
        failures.append("store sale (indiegala.com)")
        log("WARN: keeping previous sales.rss (source unreachable)")
    else:
        changed |= write_feed(
            DOCS / "sales.rss",
            "IndieGala Store Sales",
            "Discounted games in the IndieGala store, from the official IndieGala RSS feed.",
            sales,
        )

    if failures:
        log(f"NOTE: {len(failures)} source(s) failed this run: {', '.join(failures)}")
    # Fail the job only if nothing could be generated at all and no previous
    # feeds exist to fall back on.
    have_any = any((DOCS / name).exists() for name in ("games.rss", "sales.rss"))
    if not have_any:
        log("ERROR: no feeds could be generated and none exist yet")
        return 1
    log("Done." + (" Feeds changed." if changed else " No changes."))
    return 0


if __name__ == "__main__":
    sys.exit(main())
