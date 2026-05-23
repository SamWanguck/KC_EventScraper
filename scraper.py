"""Event scraper for JohnsonCountyLocal.com.

Sources (in order):
  1. JCPRD iCal — loops every CivicPlus category ID on jcprd.com
  2. OPK / opkansas.gov — best-effort (CivicPlus HCMS, not currently parseable
     without schema knowledge; logs and returns []).
  3. Patch.com Overland Park — RSS is empty in practice; kept as a placeholder.
  4. Eventbrite — public-search API is dead, so we scrape JSON-LD from the
     /d/ks--<city>/all-events/ discover pages (5 cities, paginated).
  5. Visit Overland Park — direct Algolia query (their site uses Algolia for
     all event listings, exposes a public search key).

All sources are normalized to a shared schema, deduplicated, sorted, written
to events.json.
"""

from __future__ import annotations

import json
import logging
import os
import re
import sys
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone

import feedparser  # noqa: F401  (retained for future RSS sources)
import requests
from bs4 import BeautifulSoup
from dateutil import parser as dateparser
from icalendar import Calendar
from rapidfuzz import fuzz

CT = timezone(timedelta(hours=-5))
WINDOW_DAYS = 14
OUTPUT_PATH = "events.json"
UA = "Mozilla/5.0 (compatible; JoCoLocalBot/1.0; +https://johnsoncountylocal.com)"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("scraper")


# --------------------------------------------------------------------------- #
# Schema                                                                      #
# --------------------------------------------------------------------------- #


@dataclass
class Event:
    title: str
    date: str
    end_date: str | None
    time: str
    venue: str
    city: str
    description: str
    url: str
    image_url: str | None
    is_free: bool
    source: str

    def detail_score(self) -> int:
        score = 0
        for v in (self.description, self.venue, self.time, self.image_url, self.url):
            if v:
                score += len(str(v))
        if self.end_date:
            score += 10
        return score


def _truncate(text: str | None, n: int = 300) -> str:
    if not text:
        return ""
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) <= n:
        return text
    return text[: n - 1].rstrip() + "…"


def _to_iso(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=CT)
    return dt.astimezone(timezone.utc).isoformat()


def _fmt_clock(dt: datetime) -> str:
    return dt.strftime("%#I:%M %p") if sys.platform == "win32" else dt.strftime("%-I:%M %p")


def _format_time(start: datetime | None, end: datetime | None) -> str:
    if start is None:
        return ""
    if isinstance(start, datetime) and start.hour == 0 and start.minute == 0 and end is None:
        return "All day"
    s = _fmt_clock(start)
    if end and isinstance(end, datetime):
        return f"{s} – {_fmt_clock(end)}"
    return s


def _in_window(dt: datetime, now: datetime, days: int = WINDOW_DAYS) -> bool:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=CT)
    return now <= dt <= now + timedelta(days=days)


CITIES = ("Overland Park", "Olathe", "Leawood", "Lenexa", "Shawnee",
          "Mission", "Merriam", "Prairie Village", "Roeland Park", "Fairway")


def _guess_city(text: str, default: str = "Overland Park") -> str:
    if not text:
        return default
    t = text.lower()
    for c in CITIES:
        if c.lower() in t:
            return c
    return default


# --------------------------------------------------------------------------- #
# 1. JCPRD iCal — loop every category                                         #
# --------------------------------------------------------------------------- #

JCPRD_CIDS = [14, 27, 34, 35, 64, 67, 68, 70, 71]
JCPRD_ICAL = "https://www.jcprd.com/common/modules/iCalendar/iCalendar.aspx?catID={cid}&feed=calendar"


def scrape_jcprd(now: datetime) -> list[Event]:
    events: list[Event] = []
    seen_uids: set[str] = set()
    for cid in JCPRD_CIDS:
        url = JCPRD_ICAL.format(cid=cid)
        try:
            r = requests.get(url, timeout=30, headers={"User-Agent": UA})
            if r.status_code != 200 or b"BEGIN:VCALENDAR" not in r.content:
                log.warning("JCPRD CID %s: HTTP %d", cid, r.status_code)
                continue
            cal = Calendar.from_ical(r.content)
        except Exception as e:
            log.warning("JCPRD CID %s failed: %s", cid, e)
            continue

        for comp in cal.walk("VEVENT"):
            uid = str(comp.get("UID") or "")
            if uid and uid in seen_uids:
                continue
            try:
                start = comp.decoded("DTSTART")
                end = comp.decoded("DTEND", default=None)
                start_dt = start if isinstance(start, datetime) else datetime.combine(start, datetime.min.time(), tzinfo=CT)
                end_dt = end if isinstance(end, datetime) else (datetime.combine(end, datetime.min.time(), tzinfo=CT) if end else None)
                if not _in_window(start_dt, now):
                    continue

                location = str(comp.get("LOCATION") or "")
                events.append(Event(
                    title=str(comp.get("SUMMARY") or "Untitled"),
                    date=_to_iso(start_dt) or "",
                    end_date=_to_iso(end_dt),
                    time=_format_time(start_dt, end_dt),
                    venue=location.split(",")[0].strip(),
                    city=_guess_city(location),
                    description=_truncate(str(comp.get("DESCRIPTION") or "")),
                    url=str(comp.get("URL") or "https://www.jcprd.com/calendar.aspx"),
                    image_url=None,
                    is_free=True,
                    source="JCPRD",
                ))
                if uid:
                    seen_uids.add(uid)
            except Exception as e:
                log.warning("JCPRD event skipped (CID %s): %s", cid, e)
    log.info("JCPRD: %d events", len(events))
    return events


# --------------------------------------------------------------------------- #
# 2. City of Overland Park (opkansas.gov)                                     #
# --------------------------------------------------------------------------- #


def scrape_opk(now: datetime) -> list[Event]:
    # opkansas.gov runs on CivicPlus HCMS and loads events via an
    # authenticated content API whose schema isn't public. Until we have
    # that schema, this source is a no-op. Visit OP's Algolia feed picks up
    # most major OPK civic events anyway.
    log.info("OPK: skipped (HCMS API requires schema; tracked separately)")
    return []


# --------------------------------------------------------------------------- #
# 3. Patch — RSS empty in practice                                            #
# --------------------------------------------------------------------------- #


def scrape_patch(now: datetime) -> list[Event]:
    # Patch's RSS feed at /feeds/kansas/overland-park returns a valid but
    # empty channel — they no longer publish news that way.
    log.info("Patch: skipped (RSS feed empty)")
    return []


# --------------------------------------------------------------------------- #
# 4. Eventbrite — scrape JSON-LD from discover pages                          #
# --------------------------------------------------------------------------- #

EVENTBRITE_CITIES = ["overland-park", "olathe", "leawood", "lenexa", "shawnee"]
EVENTBRITE_PAGES_PER_CITY = 3  # ~60 events per city, plenty for 14-day window


def _extract_eventbrite_jsonld(html: str) -> list[dict]:
    blocks = re.findall(
        r'<script type="application/ld\+json">(.+?)</script>',
        html, flags=re.S,
    )
    items: list[dict] = []
    for raw in blocks:
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            continue
        candidates = data if isinstance(data, list) else [data]
        for d in candidates:
            if not isinstance(d, dict):
                continue
            if d.get("@type") == "ItemList" or "itemListElement" in d:
                for el in d.get("itemListElement", []):
                    if isinstance(el, dict) and isinstance(el.get("item"), dict):
                        items.append(el["item"])
            elif d.get("@type") == "Event":
                items.append(d)
    return items


def scrape_eventbrite(now: datetime) -> list[Event]:
    # Public token is fine here — we're scraping public HTML, not hitting the
    # API. Keeping the env-var path documented for parity with the original spec.
    if os.environ.get("EVENTBRITE_API_KEY"):
        log.info("Eventbrite: API key present but unused (public search API is deprecated; using HTML JSON-LD)")

    events: list[Event] = []
    seen_urls: set[str] = set()

    for city_slug in EVENTBRITE_CITIES:
        for page in range(1, EVENTBRITE_PAGES_PER_CITY + 1):
            url = f"https://www.eventbrite.com/d/ks--{city_slug}/all-events/?page={page}"
            try:
                r = requests.get(url, timeout=30, headers={"User-Agent": UA})
                if r.status_code != 200:
                    log.warning("Eventbrite %s p%d: HTTP %d", city_slug, page, r.status_code)
                    break
            except requests.RequestException as e:
                log.warning("Eventbrite %s p%d failed: %s", city_slug, page, e)
                break

            items = _extract_eventbrite_jsonld(r.text)
            if not items:
                break
            new_on_page = 0

            for it in items:
                evurl = it.get("url", "")
                if not evurl or evurl in seen_urls:
                    continue
                seen_urls.add(evurl)
                new_on_page += 1

                try:
                    start_dt = dateparser.parse(it.get("startDate", ""))
                    end_dt = dateparser.parse(it["endDate"]) if it.get("endDate") else None
                except (ValueError, TypeError):
                    continue
                if not _in_window(start_dt, now):
                    continue

                loc = it.get("location") or {}
                addr = loc.get("address") if isinstance(loc, dict) else {}
                addr = addr if isinstance(addr, dict) else {}
                offers = it.get("offers")
                if isinstance(offers, list):
                    offers = offers[0] if offers else {}
                offers = offers if isinstance(offers, dict) else {}
                price = offers.get("price")
                is_free = (price in (None, "0", "0.00", 0, 0.0)) or (
                    str(offers.get("priceCurrency", "")).upper() == "USD" and str(price) in ("0", "0.00")
                )

                events.append(Event(
                    title=it.get("name", "") or "Untitled",
                    date=_to_iso(start_dt) or "",
                    end_date=_to_iso(end_dt),
                    time=_format_time(start_dt, end_dt),
                    venue=loc.get("name", "") if isinstance(loc, dict) else "",
                    city=addr.get("addressLocality") or city_slug.replace("-", " ").title(),
                    description=_truncate(it.get("description", "")),
                    url=evurl,
                    image_url=it.get("image"),
                    is_free=bool(is_free),
                    source="Eventbrite",
                ))

            if new_on_page == 0:
                break

    log.info("Eventbrite: %d events", len(events))
    return events


# --------------------------------------------------------------------------- #
# 5. Visit Overland Park — Algolia                                            #
# --------------------------------------------------------------------------- #

ALGOLIA_APP = "EYQHJ2IY2M"
ALGOLIA_KEY = "c6d5977cb5cd80c09abfd2a7e5d9e88b"
ALGOLIA_INDEX = "prod-visit-overland-park-listings"
ALGOLIA_URL = f"https://{ALGOLIA_APP}-dsn.algolia.net/1/indexes/{ALGOLIA_INDEX}/query"


def scrape_visit_op(now: datetime) -> list[Event]:
    events: list[Event] = []
    end = now + timedelta(days=WINDOW_DAYS)
    # Algolia stores dates as Unix epoch seconds. Filter to events whose
    # startDate falls in our window — also include long-running events whose
    # endDate is still in the future.
    start_epoch = int(now.timestamp())
    end_epoch = int(end.timestamp())
    filters = (
        f'calendarName:"Default Calendar" AND startDate <= {end_epoch} '
        f'AND endDate >= {start_epoch}'
    )

    page = 0
    while True:
        body = {"params": f"hitsPerPage=50&page={page}&filters={requests.utils.quote(filters, safe='')}"}
        try:
            r = requests.post(
                ALGOLIA_URL,
                headers={
                    "X-Algolia-API-Key": ALGOLIA_KEY,
                    "X-Algolia-Application-Id": ALGOLIA_APP,
                    "Content-Type": "application/json",
                },
                data=json.dumps(body),
                timeout=20,
            )
            if r.status_code != 200:
                log.warning("Visit OP Algolia p%d: HTTP %d", page, r.status_code)
                break
            data = r.json()
        except requests.RequestException as e:
            log.error("Visit OP Algolia failed: %s", e)
            break

        for hit in data.get("hits", []):
            start_epoch_ev = hit.get("startDate")
            end_epoch_ev = hit.get("endDate")
            if not start_epoch_ev:
                continue
            try:
                # Algolia stores dates at UTC midnight for multi-day items; treat
                # as a calendar date (no time component) to avoid TZ display drift.
                all_day = int(start_epoch_ev) % 86400 == 0
                if all_day:
                    start_dt = datetime.fromtimestamp(int(start_epoch_ev), tz=timezone.utc).replace(tzinfo=CT)
                    end_dt = datetime.fromtimestamp(int(end_epoch_ev), tz=timezone.utc).replace(tzinfo=CT) if end_epoch_ev else None
                else:
                    start_dt = datetime.fromtimestamp(int(start_epoch_ev), tz=CT)
                    end_dt = datetime.fromtimestamp(int(end_epoch_ev), tz=CT) if end_epoch_ev else None
            except (ValueError, TypeError):
                continue
            # The "in window" check here is gentler than _in_window — long-
            # running exhibits that started before today should still appear
            # if they're ongoing.
            if start_dt > now + timedelta(days=WINDOW_DAYS):
                continue
            if end_dt and end_dt < now:
                continue

            uri = hit.get("uri") or ""
            full_url = f"https://www.visitoverlandpark.com{uri}" if uri.startswith("/") else uri
            venue_obj = hit.get("location") or {}
            venue = (venue_obj.get("name") if isinstance(venue_obj, dict) else "") or hit.get("venueName") or ""

            time_str = "All day" if all_day else _format_time(start_dt, end_dt)
            events.append(Event(
                title=hit.get("title", "Untitled"),
                date=_to_iso(start_dt) or "",
                end_date=_to_iso(end_dt),
                time=time_str,
                venue=venue,
                city="Overland Park",
                description=_truncate(hit.get("snippet") or hit.get("content", "")),
                url=full_url,
                image_url=hit.get("primaryImageUrl"),
                is_free=bool(hit.get("isFree", False)),
                source="Visit Overland Park",
            ))

        if page + 1 >= data.get("nbPages", 1):
            break
        page += 1
        if page > 10:
            break  # safety

    log.info("Visit OP: %d events", len(events))
    return events


# --------------------------------------------------------------------------- #
# Deduplication                                                               #
# --------------------------------------------------------------------------- #


def _date_key(iso: str) -> str:
    try:
        return dateparser.parse(iso).date().isoformat()
    except (ValueError, TypeError):
        return iso[:10]


def deduplicate(events: list[Event], threshold: int = 80) -> list[Event]:
    by_date: dict[str, list[Event]] = {}
    for ev in events:
        by_date.setdefault(_date_key(ev.date), []).append(ev)

    deduped: list[Event] = []
    for group in by_date.values():
        clusters: list[list[Event]] = []
        for ev in group:
            placed = False
            for cluster in clusters:
                if fuzz.token_set_ratio(ev.title, cluster[0].title) >= threshold:
                    cluster.append(ev)
                    placed = True
                    break
            if not placed:
                clusters.append([ev])
        for cluster in clusters:
            deduped.append(max(cluster, key=lambda e: e.detail_score()))
    return deduped


# --------------------------------------------------------------------------- #
# Main                                                                        #
# --------------------------------------------------------------------------- #


def gather(now: datetime) -> list[Event]:
    sources = [scrape_jcprd, scrape_opk, scrape_patch, scrape_eventbrite, scrape_visit_op]
    all_events: list[Event] = []
    for fn in sources:
        try:
            all_events.extend(fn(now))
        except Exception as e:
            log.exception("Source %s failed: %s", fn.__name__, e)
    return all_events


def main() -> int:
    now = datetime.now(tz=CT)
    log.info("Scrape window: %s → %s", now.date(), (now + timedelta(days=WINDOW_DAYS)).date())

    events = gather(now)
    log.info("Collected %d raw events", len(events))
    events = deduplicate(events)
    log.info("After dedup: %d events", len(events))

    events.sort(key=lambda e: e.date)
    with open(OUTPUT_PATH, "w", encoding="utf-8") as f:
        json.dump([asdict(e) for e in events], f, indent=2, ensure_ascii=False)
    log.info("Wrote %s (%d events)", OUTPUT_PATH, len(events))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
