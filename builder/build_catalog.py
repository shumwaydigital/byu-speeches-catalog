#!/usr/bin/env python3
"""Builds catalog.json for the BYU Speeches app from speeches.byu.edu.

Standard library only (Python 3.9+). Run nightly by .github/workflows/nightly.yml:

    python3 builder/build_catalog.py            # full nightly run
    python3 builder/build_catalog.py --limit 3  # small trial: 3 listing pages

What it does, in order:
  1. Reads every talk from the WordPress REST API (id, link, modified date, speaker/topic slugs).
  2. Reads the site's own "Load More" listing, which carries view counts, MP3, PDF, excerpt.
  3. Fetches each talk's page once (again only when the site says the talk changed) for the
     duration, YouTube ids and keywords.
  4. If YOUTUBE_API_KEY is set, asks YouTube for views, likes and comments.
  5. Saves today's view counts as a snapshot and works out 7/30/90-day gains.
  6. Writes docs/catalog.json (published by GitHub Pages).

It is polite: one request a second, an identifying user agent, and it never downloads audio.
"""
import argparse
import datetime as dt
import html
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

SITE = "https://speeches.byu.edu"
USER_AGENT = ("BYUSpeechesCatalog/1.0 (personal catalog builder, 1 request/second; "
              "https://github.com/shumwaydigital/byu-speeches-catalog)")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DETAILS_PATH = os.path.join(ROOT, "data", "details.json")
SNAPSHOT_DIR = os.path.join(ROOT, "data", "snapshots")
OUT_DIR = os.path.join(ROOT, "docs")
SNAPSHOT_KEEP_DAYS = 120
CATALOG_VERSION = 1


# ---------------------------------------------------------------- network

class Fetcher:
    """HTTP with a minimum gap between requests and a few retries."""

    def __init__(self, delay=1.0):
        self.delay = delay
        self.last = 0.0
        self.count = 0

    def get(self, url, data=None, headers=None):
        body = urllib.parse.urlencode(data).encode() if data is not None else None
        last_error = None
        for attempt in range(4):
            wait = self.delay - (time.monotonic() - self.last)
            if wait > 0:
                time.sleep(wait)
            self.last = time.monotonic()
            self.count += 1
            request = urllib.request.Request(url, data=body, headers={"User-Agent": USER_AGENT, **(headers or {})})
            try:
                with urllib.request.urlopen(request, timeout=60) as response:
                    return response.read().decode("utf-8", "replace"), response.headers
            except urllib.error.HTTPError as error:
                if error.code in (400, 401, 403, 404):
                    raise
                last_error = error
            except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as error:
                last_error = error
            time.sleep(5 * (attempt + 1))
        raise RuntimeError(f"gave up on {url}: {last_error}")


# ---------------------------------------------------------------- parsing (pure functions, tested)

def parse_views(text):
    """'953k views' -> 953000, '1.2M views' -> 1200000, '812 views' -> 812. None if absent."""
    match = re.search(r"([\d.,]+)\s*([kKmM]?)\s*views?", text or "")
    if not match:
        return None
    number = float(match.group(1).replace(",", ""))
    scale = {"": 1, "k": 1_000, "m": 1_000_000}[match.group(2).lower()]
    return int(round(number * scale))


def parse_duration(iso):
    """'PT1H2M3S' -> 3723 seconds. None if it isn't an ISO-8601 duration."""
    match = re.fullmatch(r"PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?", iso or "")
    if not match or not any(match.groups()):
        return None
    hours, minutes, seconds = (int(g) if g else 0 for g in match.groups())
    return hours * 3600 + minutes * 60 + seconds


def _attr(name, text):
    """Value of name=... in a tag, quoted or not."""
    match = re.search(name + r'=(?:"([^"]*)"|\'([^\']*)\'|([^\s>]+))', text)
    if not match:
        return None
    return html.unescape(next(g for g in match.groups() if g is not None))


def _metas(prop, text):
    return [html.unescape(m.group(1) if m.group(1) is not None else m.group(2))
            for m in re.finditer(r'<meta\s+itemprop="?' + prop + r'"?\s+content=(?:"([^"]*)"|([^\s>]+))', text)]


def _clean(text):
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", "", text or ""))).strip()


def parse_cards(listing_html):
    """Every talk card in a listing page or a Load More response."""
    cards = []
    for chunk in re.split(r"<article\b", listing_html)[1:]:
        chunk = chunk.split("</article>")[0]
        urls = _metas("url", chunk)
        names = _metas("name", chunk)
        if not urls or not names:
            continue
        url = urls[0]
        path = urllib.parse.urlparse(url).path.strip("/").split("/")  # talks/<speaker>/<slug>
        speakers = []
        for m in re.finditer(r'<a\s[^>]*archive__card-name[^>]*>(.*?)</a>', chunk, re.S):
            slug = re.search(r"/speakers/([^/\"'\s>]+)", m.group(0))
            speakers.append({"name": _clean(m.group(1)), "slug": slug.group(1) if slug else ""})
        time_tag = re.search(r"<time\b[^>]*>", chunk)
        views = re.search(r"archive__card-views[^>]*>([^<]*)<", chunk)
        portrait = re.search(r"<img\b[^>]*archive__card-portrait[^>]*>", chunk, re.S)
        post_id = re.search(r"archive-dd-(\d+)", chunk)
        files = re.findall(r'href=["\']?([^"\'\s>]+\.(?:mp3|pdf))["\'\s>]', chunk, re.I)
        unavailable = [_clean(m) for m in re.findall(r"archive__media-link--unavailable[^>]*>(.*?)</span>", chunk, re.S)]
        descriptions = _metas("description", chunk)
        genres = _metas("genre", chunk)
        cards.append({
            "id": int(post_id.group(1)) if post_id else None,
            "url": url,
            "slug": path[-1] if path else "",
            "title": names[0],
            "speakers": speakers,
            "date": _attr("datetime", time_tag.group(0)) if time_tag else None,
            "views": parse_views(views.group(1)) if views else None,
            "excerpt": descriptions[0] if descriptions else "",
            "type": genres[0] if genres else "",
            "topics": _metas("about", chunk),
            "mp3": next((f for f in files if f.lower().endswith(".mp3")), None),
            "pdf": next((f for f in files if f.lower().endswith(".pdf")), None),
            "img": _attr("src", portrait.group(0)) if portrait else None,
            "hasVideo": "Video" not in unavailable and "?M=V" in chunk,
            "hasText": "Text" not in unavailable,
        })
    return cards


def youtube_id(url):
    match = re.search(r"(?:v=|/embed/|youtu\.be/|/vi/)([A-Za-z0-9_-]{11})", url or "")
    return match.group(1) if match else None


def video_kind(name):
    """'full' for the whole address; 'short' for highlights, clips and Shorts."""
    lowered = (name or "").lower()
    if "full" in lowered or "complete" in lowered:
        return "full"
    if any(word in lowered for word in ("highlight", "short", "clip", "excerpt")):
        return "short"
    return "other"


def parse_talk_page(page_html):
    """Duration, YouTube videos and keywords from a talk page's structured data."""
    result = {"dur": None, "mp3": None, "videos": [], "keywords": [], "id": None}
    for match in re.finditer(r'<script\b[^>]*application/ld\+json[^>]*>(.*?)</script>', page_html, re.S):
        try:
            graph = json.loads(match.group(1))
        except ValueError:
            continue
        nodes = graph.get("@graph", [graph]) if isinstance(graph, dict) else graph
        for node in nodes:
            if not isinstance(node, dict) or node.get("@type") != "Article":
                continue
            result["id"] = node.get("identifier")
            audio = node.get("audio") or {}
            if isinstance(audio, list):
                audio = audio[0] if audio else {}
            result["dur"] = parse_duration(audio.get("duration"))
            result["mp3"] = audio.get("contentUrl")
            videos = node.get("video") or []
            if isinstance(videos, dict):
                videos = [videos]
            seen = set()
            for video in videos:
                vid = youtube_id(video.get("contentUrl")) or youtube_id(video.get("embedUrl"))
                if vid and vid not in seen:
                    seen.add(vid)
                    result["videos"].append({"id": vid, "kind": video_kind(video.get("name"))})
            if result["videos"] and not any(v["kind"] == "full" for v in result["videos"]):
                # A single unlabelled video is the address itself.
                if len(result["videos"]) == 1:
                    result["videos"][0]["kind"] = "full"
            words = []
            for key in ("about", "keywords", "teaches"):
                value = node.get(key) or []
                if isinstance(value, str):
                    value = [v.strip() for v in value.split(",")]
                words += [v for v in value if isinstance(v, str)]
            result["keywords"] = list(dict.fromkeys(words))[:14]
    return result


def slugs_from_class_list(class_list):
    """REST class_list -> (speaker slugs, topic slugs, event type slug)."""
    speakers = [c[len("speaker-"):] for c in class_list if c.startswith("speaker-")]
    topics = [c[len("topic-"):] for c in class_list if c.startswith("topic-")]
    kinds = [c[len("event_type-"):] for c in class_list if c.startswith("event_type-")]
    return speakers, topics, (kinds[0] if kinds else "")


def gains(today_views, snapshots, today, days):
    """Views gained over `days`, per talk id, using the newest snapshot at least that old.

    snapshots: {date string: {id string: [site, youtube]}}. A talk missing from the old
    snapshot (or no snapshot old enough) has no gain yet -> absent from the result.
    """
    cutoff = (today - dt.timedelta(days=days)).isoformat()
    older = sorted(d for d in snapshots if d <= cutoff)
    if not older:
        return {}
    # Don't stretch a window over a much older snapshot (e.g. after a long outage).
    base_date = older[-1]
    if (today - dt.date.fromisoformat(base_date)).days > days * 2 + 3:
        return {}
    base = snapshots[base_date]
    result = {}
    for talk_id, now in today_views.items():
        then = base.get(str(talk_id))
        if then is None:
            continue
        gained = 0
        for current, previous in zip(now, then):
            if current is not None and previous is not None:
                gained += max(0, current - previous)
        result[talk_id] = gained
    return result


def needs_detail_fetch(cached, modified, today, talk_date):
    """Fetch a talk's page when it's new, when the site changed it, or weekly while it is recent
    (new talks gain their video, audio and text over the first weeks)."""
    if cached is None:
        return True
    if cached.get("modified") != modified:
        return True
    try:
        age = (today - dt.date.fromisoformat((talk_date or "")[:10])).days
        since = (today - dt.date.fromisoformat(cached.get("fetched", "1970-01-01"))).days
    except ValueError:
        return False
    return age <= 60 and since >= 7 and not (cached.get("dur") and cached.get("videos"))


# ---------------------------------------------------------------- site readers

def read_rest_index(fetcher, limit=None):
    """{link: {id, modified, speakerSlugs, topicSlugs, typeSlug}} for every talk."""
    index, page = {}, 1
    while True:
        url = (f"{SITE}/wp-json/wp/v2/speech?per_page=100&page={page}"
               "&_fields=id,link,slug,modified,date,class_list&orderby=date&order=desc")
        try:
            text, headers = fetcher.get(url)
        except urllib.error.HTTPError as error:
            if error.code == 400:  # past the last page
                break
            raise
        rows = json.loads(text)
        if not rows:
            break
        for row in rows:
            speakers, topics, kind = slugs_from_class_list(row.get("class_list", []))
            index[row["link"]] = {"id": row["id"], "modified": row.get("modified"), "date": row.get("date"),
                                  "speakerSlugs": speakers, "topicSlugs": topics, "typeSlug": kind}
        total_pages = int(headers.get("X-WP-TotalPages", page))
        if page >= total_pages or (limit and page >= limit):
            break
        page += 1
    return index


def read_listing(fetcher, limit=None):
    """Every card from the site's listing, through the same call its Load More button makes."""
    page_html, _ = fetcher.get(f"{SITE}/talks/?sort=most_viewed")
    nonce = re.search(r'"nonce":"([^"]+)","action":"pgsp_get_archive_speeches"', page_html)
    if not nonce:
        raise RuntimeError("the listing page no longer exposes its Load More settings")
    cards, page = {}, 1
    while True:
        text, _ = fetcher.get(f"{SITE}/wp-admin/admin-ajax.php", data={
            "action": "pgsp_get_archive_speeches", "security": nonce.group(1), "page": page,
            "sort": "most_viewed", "year": "", "event_type": "", "topic": ""})
        payload = json.loads(text)
        if not isinstance(payload, dict) or not payload.get("success"):
            raise RuntimeError(f"Load More refused page {page}: {text[:120]}")
        found = parse_cards(payload["data"].get("html", ""))
        for card in found:
            cards[card["url"]] = card
        if page % 50 == 0:
            print(f"  listing page {page}: {len(cards)} talks", flush=True)
        if not payload["data"].get("has_more") or not found or (limit and page >= limit):
            break
        page += 1
    return cards


def read_youtube(fetcher, ids, key):
    """{video id: {views, likes, comments}} — 1 quota unit per 50 videos."""
    stats = {}
    ids = sorted(set(ids))
    for start in range(0, len(ids), 50):
        batch = ids[start:start + 50]
        url = ("https://www.googleapis.com/youtube/v3/videos?part=statistics&maxResults=50&id="
               + ",".join(batch) + "&key=" + urllib.parse.quote(key))
        try:
            text, _ = fetcher.get(url)
        except Exception as error:  # a YouTube outage must not sink the nightly run
            print(f"  YouTube lookup failed: {type(error).__name__}", flush=True)
            return stats
        for item in json.loads(text).get("items", []):
            numbers = item.get("statistics", {})
            stats[item["id"]] = {k: int(numbers[src]) if src in numbers else None
                                 for k, src in (("views", "viewCount"), ("likes", "likeCount"),
                                                ("comments", "commentCount"))}
    return stats


# ---------------------------------------------------------------- assembling

def load_json(path, default):
    try:
        with open(path, encoding="utf-8") as handle:
            return json.load(handle)
    except (FileNotFoundError, ValueError):
        return default


def save_json(path, value, pretty=False):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    temporary = path + ".tmp"
    with open(temporary, "w", encoding="utf-8") as handle:
        if pretty:
            json.dump(value, handle, ensure_ascii=False, indent=1, sort_keys=True)
        else:
            json.dump(value, handle, ensure_ascii=False, separators=(",", ":"))
    os.replace(temporary, path)


def load_snapshots():
    snapshots = {}
    if os.path.isdir(SNAPSHOT_DIR):
        for name in os.listdir(SNAPSHOT_DIR):
            if re.fullmatch(r"\d{4}-\d\d-\d\d\.json", name):
                snapshots[name[:-5]] = load_json(os.path.join(SNAPSHOT_DIR, name), {})
    return snapshots


def build_record(card, rest, detail, youtube, gain):
    """One talk as the app reads it. Empty values are left out to keep the file small."""
    videos = (detail or {}).get("videos", [])
    full = next((v["id"] for v in videos if v["kind"] == "full"), None)
    clips = [v["id"] for v in videos if v["kind"] != "full"]
    stats = youtube.get(full) if full else None
    speakers = card["speakers"] or [{"name": "", "slug": (rest or {}).get("speakerSlugs", [""])[0] if rest else ""}]
    record = {
        "id": (rest or {}).get("id") or card["id"],
        "slug": card["slug"],
        "url": card["url"],
        "title": card["title"],
        "speaker": " and ".join(s["name"] for s in speakers if s["name"]),
        "speakerSlug": speakers[0]["slug"],
        "speakers": speakers if len(speakers) > 1 else None,
        "date": card["date"] or ((rest or {}).get("date") or "")[:10],
        "type": card["type"],
        "topics": card["topics"],
        "excerpt": card["excerpt"],
        "mp3": card["mp3"] or (detail or {}).get("mp3"),
        "pdf": card["pdf"],
        "img": card["img"],
        "dur": (detail or {}).get("dur"),
        "yt": full,
        "clips": clips,
        "views": card["views"],
        "ytViews": stats["views"] if stats else None,
        "ytLikes": stats["likes"] if stats else None,
        "ytComments": stats["comments"] if stats else None,
        "clipViews": sum((youtube.get(c) or {}).get("views") or 0 for c in clips) or None,
        "g7": gain[7], "g30": gain[30], "g90": gain[90],
        "hasText": card["hasText"] or None,
        "keywords": (detail or {}).get("keywords"),
    }
    return {k: v for k, v in record.items() if v not in (None, "", [], False)}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--limit", type=int, help="trial run: only this many listing pages")
    parser.add_argument("--max-details", type=int, default=4000, help="most talk pages to fetch in one run")
    parser.add_argument("--delay", type=float, default=1.0, help="seconds between requests")
    args = parser.parse_args(argv)

    today = dt.datetime.now(dt.timezone.utc).date()
    fetcher = Fetcher(args.delay)

    print("1. REST index…", flush=True)
    rest_index = read_rest_index(fetcher, limit=1 if args.limit else None)
    print(f"   {len(rest_index)} talks", flush=True)

    print("2. Listing with view counts…", flush=True)
    cards = read_listing(fetcher, limit=args.limit)
    print(f"   {len(cards)} talks", flush=True)
    if not args.limit and len(cards) < 0.9 * max(1, len(rest_index)):
        raise RuntimeError(f"listing returned {len(cards)} talks but the API has {len(rest_index)}; not publishing")

    print("3. Talk pages (new or changed only)…", flush=True)
    details = load_json(DETAILS_PATH, {})
    fetched = 0
    for url, card in cards.items():
        rest = rest_index.get(url)
        talk_id = str((rest or {}).get("id") or card["id"] or url)
        modified = (rest or {}).get("modified")
        if not needs_detail_fetch(details.get(talk_id), modified, today, card["date"]):
            continue
        if fetched >= args.max_details:
            break
        try:
            text, _ = fetcher.get(url)
        except Exception as error:
            print(f"   skipped {url}: {error}", flush=True)
            continue
        parsed = parse_talk_page(text)
        details[talk_id] = {"modified": modified, "fetched": today.isoformat(), "dur": parsed["dur"],
                            "mp3": parsed["mp3"], "videos": parsed["videos"], "keywords": parsed["keywords"]}
        fetched += 1
        if fetched % 100 == 0:
            print(f"   {fetched} pages", flush=True)
            save_json(DETAILS_PATH, details, pretty=True)
    save_json(DETAILS_PATH, details, pretty=True)
    print(f"   fetched {fetched} pages", flush=True)

    print("4. YouTube…", flush=True)
    key = os.environ.get("YOUTUBE_API_KEY", "").strip()
    video_ids = [v["id"] for d in details.values() for v in d.get("videos", [])]
    youtube = read_youtube(Fetcher(0.2), video_ids, key) if key else {}
    print(f"   {len(youtube)} of {len(set(video_ids))} videos" + ("" if key else " (no YOUTUBE_API_KEY set)"), flush=True)

    print("5. Snapshot and gains…", flush=True)
    today_views = {}
    for url, card in cards.items():
        rest = rest_index.get(url)
        talk_id = str((rest or {}).get("id") or card["id"] or url)
        full = next((v["id"] for v in details.get(talk_id, {}).get("videos", []) if v["kind"] == "full"), None)
        today_views[talk_id] = [card["views"], (youtube.get(full) or {}).get("views") if full else None]
    snapshots = load_snapshots()
    windows = {days: gains(today_views, snapshots, today, days) for days in (7, 30, 90)}
    if not args.limit:
        save_json(os.path.join(SNAPSHOT_DIR, today.isoformat() + ".json"), today_views)
        oldest = (today - dt.timedelta(days=SNAPSHOT_KEEP_DAYS)).isoformat()
        for date in snapshots:
            if date < oldest:
                os.remove(os.path.join(SNAPSHOT_DIR, date + ".json"))

    print("6. Writing catalog…", flush=True)
    talks = []
    for url, card in cards.items():
        rest = rest_index.get(url)
        talk_id = str((rest or {}).get("id") or card["id"] or url)
        gain = {days: windows[days].get(talk_id) for days in (7, 30, 90)}
        record = build_record(card, rest, details.get(talk_id), youtube, gain)
        if record.get("id") is not None:
            talks.append(record)
    talks.sort(key=lambda t: (t.get("date", ""), t["id"]), reverse=True)
    catalog = {
        "version": CATALOG_VERSION,
        "generated": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "latestDate": talks[0].get("date") if talks else None,
        "hasYouTube": bool(youtube),
        "snapshotDays": len(snapshots) + (0 if args.limit else 1),
        "count": len(talks),
        "talks": talks,
    }
    name = "catalog-trial.json" if args.limit else "catalog.json"
    save_json(os.path.join(OUT_DIR, name), catalog)
    size = os.path.getsize(os.path.join(OUT_DIR, name))
    print(f"Done: {len(talks)} talks, {size / 1e6:.2f} MB, {fetcher.count} requests to the site.", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
