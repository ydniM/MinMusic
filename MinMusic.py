#!/usr/bin/env python3
"""
Builds an RSS feed of recent releases by the artists in Artists.txt.

Main source: Deezer's public API. Lines with a MusicBrainz link are also checked on
MusicBrainz. If Deezer isn't working, MusicBrainz is used for everything and a notice
is added to the top of the feed.

Writes site/feed.xml, site/index.html, and a report to the Actions run summary.
"""

import datetime as dt
import hashlib
import http.client
import json
import os
import re
import sys
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from email.utils import format_datetime
from xml.sax.saxutils import escape

# ---- Settings --------------------------------------------------------------
DAYS_BACK = 90                     # how far back to include releases
FEED_TITLE = "MinMusic"
ARTISTS_FILE = "Artists.txt"
OUT_DIR = "site"
# ---------------------------------------------------------------------------

REPO = os.environ.get("GITHUB_REPOSITORY", "")
REPO_URL = f"https://github.com/{REPO}" if REPO else "https://github.com"
OWNER, _, REPO_NAME = REPO.partition("/")
FEED_URL = f"https://{OWNER.lower()}.github.io/{REPO_NAME}/feed.xml" if REPO else ""
USER_AGENT = f"MinMusic/1.2 ( {REPO_URL} )"   # MusicBrainz asks for contact info here

DEEZER = "https://api.deezer.com"
MUSICBRAINZ = "https://musicbrainz.org/ws/2/"
DEEZER_LINK_RE = re.compile(r"deezer\.com/(?:[a-z]{2}(?:-[a-z]{2})?/)?artist/(\d+)", re.I)
MB_LINK_RE = re.compile(r"musicbrainz\.org/artist/([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})", re.I)
DEEZER_TYPES = {"album": "Album", "ep": "EP", "single": "Single", "compile": "Compilation"}
XML_INVALID_RE = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f￾￿]")


class NotFound(Exception):
    pass


# ---- Shared helpers -------------------------------------------------------

def http_json(url, delay=0.0, tries=4):
    """Download JSON. Retries network errors, 429 and 5xx; raises NotFound on 404."""
    host = urllib.parse.urlsplit(url).netloc
    for attempt in range(tries):
        wait = 2 * (attempt + 1)
        try:
            req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
            with urllib.request.urlopen(req, timeout=30) as resp:
                data = json.load(resp)
            time.sleep(delay)
            return data
        except urllib.error.HTTPError as e:
            if e.code == 404:
                raise NotFound(url)
            if e.code != 429 and e.code < 500:
                raise RuntimeError(f"{host} answered HTTP {e.code}")
            error = f"HTTP {e.code}"
            retry_after = e.headers.get("Retry-After", "")
            if retry_after.isdigit():
                wait = min(int(retry_after), 60)
        except (OSError, ValueError, http.client.HTTPException) as e:   # network, timeout, bad JSON
            error = e
        if attempt < tries - 1:
            time.sleep(wait)
    raise RuntimeError(f"{host} did not respond after {tries} tries ({error})")


def clean(text):
    """API text with characters that would break XML removed."""
    return XML_INVALID_RE.sub("", str(text or "")).strip()


def safe_url(url):
    """Only https links from the APIs go into the feed."""
    url = clean(url)
    return url if url.lower().startswith("https://") else None


def norm(text):
    """Text normalized for the feed item ID (case, spacing, Unicode forms). Don't change:
    changing it gives every item a new ID, so readers would show them all again."""
    text = unicodedata.normalize("NFKC", text or "").casefold()
    return " ".join(text.split())


def match_key(text):
    """Looser form for spotting duplicates: letters and digits only, so
    "Don't" (Deezer) and "Don’t" (MusicBrainz) match."""
    return "".join(ch for ch in norm(text) if ch.isalnum())


def parse_date(value):
    try:
        return dt.date.fromisoformat(value)
    except (TypeError, ValueError):
        return None  # unknown or partial date (like "0000-00-00" or "2026")


def md(text):
    """Text made safe for a Markdown table cell or link label."""
    text = clean(text).replace("\n", " ").replace("\\", "\\\\")
    for ch in "|[]*_`":
        text = text.replace(ch, "\\" + ch)
    return text.replace("<", "&lt;").replace(">", "&gt;")


def md_link(label, url):
    url = safe_url(url)
    return f"[{md(label)}](<{url}>)" if url else md(label)


def parse_line(line):
    """Split a line into name, Deezer ids, MusicBrainz ids, and unreadable parts."""
    parts = [p.strip() for p in line.split("|")]
    name, deezer_ids, mb_ids, unreadable = "", [], [], []
    for i, part in enumerate(parts):
        if not part:
            continue
        dz, mb = DEEZER_LINK_RE.search(part), MB_LINK_RE.search(part)
        if dz:
            deezer_ids.append(dz.group(1))
        elif mb:
            mb_ids.append(mb.group(1).lower())
        elif i > 0 and part.isdigit():
            deezer_ids.append(part)
        elif i == 0:
            name = part
        else:
            unreadable.append(part)
    return {"name": name, "deezer": list(dict.fromkeys(deezer_ids)),
            "mb": list(dict.fromkeys(mb_ids)), "unreadable": unreadable}


# ---- Deezer (main source) -------------------------------------------------

def deezer_get(path, params=None):
    url = DEEZER + path + ("?" + urllib.parse.urlencode(params) if params else "")
    for attempt in range(3):
        data = http_json(url, delay=0.15)
        if isinstance(data, dict) and "error" in data:
            err = data["error"]
            if err.get("code") in (4, 700) and attempt < 2:   # too many requests / busy
                time.sleep(5)
                continue
            if err.get("code") == 800:                         # no data
                raise NotFound(err.get("message", "no data"))
            raise RuntimeError(f"Deezer error: {err.get('message', err)}")
        return data
    raise RuntimeError("Deezer kept saying it was too busy")


class DeezerSource:
    label = "Deezer"

    def check(self):
        data = deezer_get("/search/artist", {"q": "radiohead", "limit": 1})
        if not isinstance(data, dict) or not data.get("data"):
            raise RuntimeError("Deezer gave an unexpected answer to a test search")

    def lookup(self, line, notes):
        """Returns [(artist id, name, link)]."""
        name, artists = line["name"], []
        if line["deezer"]:
            for artist_id in line["deezer"]:
                try:
                    info = deezer_get(f"/artist/{artist_id}")
                    artists.append((artist_id, clean(info.get("name")) or name or artist_id,
                                    f"https://www.deezer.com/artist/{artist_id}"))
                except NotFound:
                    notes.append(f"❌ No Deezer artist with number {artist_id}.")
            return artists
        if not name:
            notes.append("❌ No name or Deezer link.")
            return []
        results = deezer_get("/search/artist", {"q": name, "limit": 25}).get("data", [])
        if not results:
            notes.append("❌ No match on Deezer. Add the artist's Deezer link.")
            return []
        top = results[0]
        if norm(top.get("name")) != norm(name):
            notes.append(f"⚠️ Closest match has a different name: **{md(top.get('name'))}**. "
                         "If that's wrong, add the right Deezer link.")
        others = [r for r in results[1:] if norm(r.get("name")) == norm(name)]
        if others:
            links = ", ".join(f"https://www.deezer.com/artist/{int(r['id'])}" for r in others)
            notes.append(f"⚠️ Other Deezer artists with this name: {links}. "
                         "If any are the same artist, add their links.")
        return [(str(int(top["id"])), clean(top.get("name")) or name,
                 f"https://www.deezer.com/artist/{int(top['id'])}")]

    def releases(self, artist_id, artist_name):
        """Every release Deezer lists for this artist. The artist's release list can lag
        a week or more behind, so top tracks and an album search are also checked, and
        any release missing from the list is looked up directly."""
        listed = set()
        index = 0
        while True:
            page = deezer_get(f"/artist/{artist_id}/albums", {"limit": 100, "index": index})
            batch = page.get("data", [])
            for album in batch:
                listed.add(album.get("id"))
                yield self._release(album)
            index += len(batch)
            if not batch or index >= page.get("total", 0):
                break

        extra = []
        try:
            for track in deezer_get(f"/artist/{artist_id}/top", {"limit": 100}).get("data", []):
                extra.append((track.get("album") or {}).get("id"))
            query = 'artist:"{}"'.format(artist_name.replace('"', ""))
            for album in deezer_get("/search/album", {"q": query, "limit": 100}).get("data", []):
                if str((album.get("artist") or {}).get("id")) == str(artist_id):
                    extra.append(album.get("id"))
        except Exception as e:
            print(f"::warning::Extra Deezer check for artist {artist_id} failed: {e}")
            return
        for album_id in dict.fromkeys(extra):
            if not album_id or album_id in listed:
                continue
            listed.add(album_id)
            try:
                album = deezer_get(f"/album/{album_id}")
            except NotFound:
                continue
            if str((album.get("artist") or {}).get("id")) == str(artist_id):
                yield self._release(album)

    @staticmethod
    def _release(album):
        album_id = album.get("id")
        return {
            "source_id": f"deezer-{album_id}",
            "title": clean(album.get("title")) or "Untitled",
            "type": DEEZER_TYPES.get(album.get("record_type"), "Release"),
            "date": parse_date(album.get("release_date")),
            "link": safe_url(album.get("link")) or f"https://www.deezer.com/album/{album_id}",
            "cover": safe_url(album.get("cover_xl") or album.get("cover_big") or album.get("cover")),
        }


# ---- MusicBrainz ----------------------------------------------------------

def mb_get(path, params):
    url = MUSICBRAINZ + path + "?" + urllib.parse.urlencode({**params, "fmt": "json"})
    return http_json(url, delay=1.1)   # MusicBrainz allows about one request per second


class MusicBrainzSource:
    label = "MusicBrainz"

    def lookup(self, line, notes):
        """Returns [(artist id, name, link)]."""
        name, artists = line["name"], []
        if line["mb"]:
            for mbid in line["mb"]:
                try:
                    info = mb_get(f"artist/{mbid}", {})
                    artists.append((mbid, clean(info.get("name")) or name or mbid,
                                    f"https://musicbrainz.org/artist/{mbid}"))
                except NotFound:
                    notes.append(f"❌ No MusicBrainz artist with ID {mbid}.")
            return artists
        if not name:
            notes.append("❌ No name or MusicBrainz link, so the backup can't check this line.")
            return []
        q = name.replace("\\", "\\\\").replace('"', '\\"')
        results = mb_get("artist", {"query": f'artist:"{q}" OR alias:"{q}"', "limit": 25}).get("artists", [])
        if not results:
            notes.append("❌ No match on MusicBrainz. Add the artist's MusicBrainz link.")
            return []
        top = results[0]
        known_names = {norm(top.get("name"))} | {norm(a.get("name")) for a in top.get("aliases", [])}
        if norm(name) not in known_names:
            notes.append(f"⚠️ Closest MusicBrainz match has a different name: **{md(top.get('name'))}**. "
                         "If that's wrong, add the right MusicBrainz link.")
        others = [r for r in results[1:] if norm(r.get("name")) == norm(name)]
        if others:
            links = ", ".join(f"https://musicbrainz.org/artist/{md(r['id'])}"
                              + (f" ({md(r['disambiguation'])})" if r.get("disambiguation") else "")
                              for r in others)
            notes.append(f"⚠️ Other MusicBrainz artists with this name: {links}. "
                         "If the match is wrong, add the right MusicBrainz link.")
        return [(top["id"], clean(top.get("name")) or name, f"https://musicbrainz.org/artist/{top['id']}")]

    def releases(self, mbid, artist_name=None):
        offset = 0
        while True:
            page = mb_get("release-group", {"artist": mbid, "limit": 100, "offset": offset})
            batch = page.get("release-groups", [])
            for rg in batch:
                kind = rg.get("primary-type") or "Release"
                if "Compilation" in (rg.get("secondary-types") or []):
                    kind = "Compilation"
                yield {
                    "source_id": f"mb-{rg.get('id')}",
                    "mbid": rg.get("id"),
                    "title": clean(rg.get("title")) or "Untitled",
                    "type": clean(kind),
                    "date": parse_date(rg.get("first-release-date")),
                    "link": f"https://musicbrainz.org/release-group/{rg.get('id')}",
                }
            offset += len(batch)
            if not batch or offset >= page.get("release-group-count", 0):
                return


def cover_art_archive(mbid):
    try:
        data = http_json(f"https://coverartarchive.org/release-group/{mbid}", tries=2)
    except Exception:
        return None   # no cover on file, or the service is down
    for image in data.get("images", []):
        if image.get("front"):
            thumbs = image.get("thumbnails", {})
            url = clean(thumbs.get("500") or thumbs.get("large") or image.get("image"))
            return safe_url(url.replace("http://", "https://", 1))
    return None


# ---- Building the feed ----------------------------------------------------

def collect(source, line, notes, matched, cutoff, today, seen, line_keys, found, skip_titles=frozenset()):
    """Add one line's in-range releases from one source to `found` (so a failure partway
    keeps what was already found). Returns the title keys of every release seen, any date."""
    all_titles = set()
    for artist_id, artist_name, artist_link in source.lookup(line, notes):
        matched.append(md_link(artist_name, artist_link))
        for rel in source.releases(artist_id, artist_name):
            title_key = match_key(rel["title"])
            all_titles.add(title_key)
            if rel["date"] is None or not (cutoff <= rel["date"] <= today):
                continue
            key = (title_key, norm(rel["type"]), rel["date"])
            if rel["source_id"] in seen or key in line_keys or title_key in skip_titles:
                continue
            seen.add(rel["source_id"])
            line_keys.add(key)
            rel["artist"] = line["name"] or artist_name
            found.append(rel)
    return all_titles


def build(source, lines, cutoff, today, extra=None):
    """Check every line with `source`. If `extra` is given, lines with a MusicBrainz link
    are also checked there, adding releases the main source doesn't have.
    Returns (items, report, lines checked, lines failed)."""
    items, report, seen = [], [], set()
    checked = failed = 0
    for raw in lines:
        text = raw.strip()
        if not text or text.startswith("#"):
            continue
        checked += 1
        line = parse_line(text)
        notes, matched, line_keys = [], [], set()
        for bad in line["unreadable"]:
            notes.append(f"⚠️ Couldn't read `{md(bad)}` as a Deezer or MusicBrainz artist link. "
                         "Copy the link from the artist's page.")
        main_titles = set()
        try:
            main_titles = collect(source, line, notes, matched, cutoff, today, seen, line_keys, items)
        except Exception as e:
            failed += 1
            notes.append(f"❌ {source.label} error: {md(e)}")
        if extra and line["mb"]:
            try:
                collect(extra, line, notes, matched, cutoff, today, seen, line_keys, items, main_titles)
            except Exception as e:
                notes.append(f"⚠️ MusicBrainz check failed this time: {md(e)}")
        report.append((text, ", ".join(matched) or "—", " ".join(notes) or "✅"))
    return items, report, checked, failed


def main():
    now = dt.datetime.now(dt.timezone.utc)
    today = now.date()
    cutoff = today - dt.timedelta(days=DAYS_BACK)

    try:
        with open(ARTISTS_FILE, encoding="utf-8-sig") as f:
            lines = f.read().splitlines()
    except FileNotFoundError:
        sys.exit(f"Could not find {ARTISTS_FILE}. It must be in the same folder as this script.")

    source, backup_reason = DeezerSource(), None
    try:
        source.check()
        items, report, checked, failed = build(source, lines, cutoff, today, extra=MusicBrainzSource())
        if checked and failed == checked:
            raise RuntimeError("every artist failed on Deezer")
    except Exception as e:
        backup_reason = clean(e)
        print(f"::warning::Deezer isn't working ({backup_reason}). Using the MusicBrainz backup.")
        source = MusicBrainzSource()
        items, report, checked, failed = build(source, lines, cutoff, today)
        if checked and failed == checked:
            print_report(report, source, backup_reason, [])
            sys.exit("Deezer and the MusicBrainz backup both failed. The previous feed was left unchanged.")

    for rel in items:
        if "mbid" in rel:
            rel["cover"] = cover_art_archive(rel["mbid"])
    items.sort(key=lambda x: (x["date"], x["artist"].casefold()), reverse=True)

    os.makedirs(OUT_DIR, exist_ok=True)
    write_feed(items, now, backup_reason)
    write_index()
    print_report(report, source, backup_reason, items)


def release_guid(rel):
    """Same release = same ID, whichever source found it, so switching sources doesn't repeat items."""
    key = f'{norm(rel["artist"])}|{norm(rel["title"])}|{rel["date"].isoformat()}'
    return "release-" + hashlib.sha1(key.encode("utf-8")).hexdigest()[:16]


def attr(text):
    return escape(text, {'"': "&quot;"})


def nice_date(day):
    return f"{day:%b} {day.day}, {day.year}"


def backup_notice(now, reason):
    year, week, _ = now.date().isocalendar()
    actions = f"{REPO_URL}/actions"
    body = ("<p>Deezer isn't working, so the feed is using MusicBrainz. "
            "Releases may show up later than usual, and a few may appear twice.</p>"
            f"<p>Reason: {escape(reason)}</p>"
            f'<p>This notice repeats weekly until Deezer works again. <a href="{attr(actions)}">Runs</a></p>')
    return f"""    <item>
      <title>⚠️ MinMusic is using the MusicBrainz backup</title>
      <link>{escape(actions)}</link>
      <guid isPermaLink="false">backup-notice-{year}-W{week:02d}</guid>
      <pubDate>{format_datetime(now)}</pubDate>
      <description>{escape(body)}</description>
    </item>"""


def write_feed(items, now, backup_reason):
    entries = [backup_notice(now, backup_reason)] if backup_reason else []
    for it in items:
        # Noon UTC on the release date, but never in the future.
        published = min(dt.datetime.combine(it["date"], dt.time(12, 0), dt.timezone.utc), now)
        title = f'{it["artist"]} – {it["title"]} ({it["type"]})'
        site = "MusicBrainz" if it["source_id"].startswith("mb-") else "Deezer"
        cover = it.get("cover")
        body = ""
        if cover:
            body += f'<p><img src="{attr(cover)}" alt="{attr(title)} cover" width="500"/></p>'
        body += (f'<p>{escape(it["type"])} · {nice_date(it["date"])} · '
                 f'<a href="{attr(it["link"])}">Open on {site}</a></p>')
        cover_tags = ""
        if cover:
            cover_tags = (f'\n      <enclosure url="{attr(cover)}" type="image/jpeg" length="0"/>'
                          f'\n      <media:thumbnail url="{attr(cover)}"/>')
        entries.append(f"""    <item>
      <title>{escape(title)}</title>
      <link>{escape(it["link"])}</link>
      <guid isPermaLink="false">{release_guid(it)}</guid>
      <pubDate>{format_datetime(published)}</pubDate>
      <description>{escape(body)}</description>{cover_tags}
    </item>""")

    self_link = f'\n    <atom:link href="{attr(FEED_URL)}" rel="self" type="application/rss+xml"/>' if FEED_URL else ""
    feed = f"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:media="http://search.yahoo.com/mrss/" xmlns:atom="http://www.w3.org/2005/Atom">
  <channel>
    <title>{escape(FEED_TITLE)}</title>
    <link>{escape(REPO_URL)}</link>
    <description>New releases from the artists I follow (last {DAYS_BACK} days)</description>{self_link}
    <lastBuildDate>{format_datetime(now)}</lastBuildDate>
{chr(10).join(entries)}
  </channel>
</rss>
"""
    with open(os.path.join(OUT_DIR, "feed.xml"), "w", encoding="utf-8") as f:
        f.write(feed)


def write_index():
    page = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>{escape(FEED_TITLE)}</title></head>
<body><p>Feed: <a href="feed.xml">feed.xml</a></p></body>
</html>
"""
    with open(os.path.join(OUT_DIR, "index.html"), "w", encoding="utf-8") as f:
        f.write(page)


def print_report(report, source, backup_reason, items):
    text = [f"## {FEED_TITLE} report\n"]
    if backup_reason:
        text.append(f"⚠️ **Deezer isn't working ({md(backup_reason)}), so this run used MusicBrainz.**\n")
    problems = sum(1 for _, _, n in report if n != "✅")
    text.append(f"**{len(items)}** releases from the last {DAYS_BACK} days · {len(report)} artists · "
                f"{problems} with notes · source: {source.label}\n")
    if items:
        text += ["### Releases", "", "| Released | Artist | Title | Type | From |", "|---|---|---|---|---|"]
        text += [f"| {it['date'].isoformat()} | {md(it['artist'])} | {md_link(it['title'], it['link'])} | "
                 f"{md(it['type'])} | {'MusicBrainz' if it['source_id'].startswith('mb-') else 'Deezer'} |"
                 for it in items]
        text.append("")
    text += ["### Artists", "", f"| Line in {ARTISTS_FILE} | Matched | Notes |", "|---|---|---|"]
    text += [f"| {md(line)} | {m} | {n} |" for line, m, n in report]
    output = "\n".join(text) + "\n"
    print(output)
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as f:
            f.write(output)


if __name__ == "__main__":
    main()
