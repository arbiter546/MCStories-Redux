#!/usr/bin/env python3
"""Keep stories.json in sync with the MCStories "What Was New" page.

How it works
------------
1. Reads stories.json and finds the newest date it knows about (the maximum of
   every story's date_added / date_updated).
2. Downloads https://mcstories.com/WhatWasNew.html and keeps only the weekly
   batches dated *after* that.
3. Every story linked from those batches (new stories, new chapters, revised
   versions) has its own index page read once. That page is the source of truth
   for title, author(s), tags, synopsis, dates, chapter list and word counts.
4. New stories are added, existing ones are replaced, tag counts are rebuilt,
   the list is re-sorted the way the file is already sorted, and the file is
   written back atomically (previous version kept as stories.json.bak).

Nothing is written unless every page fetched and parsed cleanly, so a failed
run can simply be repeated. Run it as often as you like; when the site has no
newer batch it exits immediately after a single request.

Requires: Python 3.9+, `pip install requests beautifulsoup4`
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import re
import shutil
import sys
import tempfile
import time
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, timedelta
from urllib import robotparser
from urllib.parse import urldefrag, urljoin, urlparse

import requests
from bs4 import BeautifulSoup, NavigableString, Tag

BASE = "https://mcstories.com/"
NEWS_URL = BASE + "WhatWasNew.html"
USER_AGENT = "stories-json-updater/1.0 (personal index of public story metadata)"

# Directories that contain an index.html but are not stories.
NON_STORY_DIRS = {"ReadersPicks", "Authors", "Titles", "Tags"}
# Tags that are left out of the JSON (every story on the site has "mc").
IGNORED_TAGS = {"mc"}
# Fields that are expected to change when a story gets new chapters.
EXPECTED_CHANGES = {"date_updated", "chapters", "words"}

log = logging.getLogger("update_stories")


# --------------------------------------------------------------------------- #
# Small helpers
# --------------------------------------------------------------------------- #
def clean(text: str | None) -> str:
    """Collapse all whitespace (used for comparing and for names/dates)."""
    return re.sub(r"\s+", " ", text or "").strip()


def tidy(text: str | None) -> str:
    """Trim and fold line breaks into a space, but keep spacing as written.

    The existing JSON keeps authors' double spaces ("end.  Next"), so only
    layout artefacts (newlines) are removed rather than collapsing everything.
    """
    return re.sub(r"\s*[\r\n]+\s*", " ", text or "").strip()


MONTHS = {m: i for i, m in enumerate(
    ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"), 1)}
DATE_RE = re.compile(r"^\s*(\d{1,2})\s+([A-Za-z]{3,9})\.?,?\s+(\d{4})\s*$")
WORDS_RE = re.compile(r"([\d,]+)\s+words?\b", re.I)
PAREN_WORDS_RE = re.compile(r"\(\s*([\d,]+)\s+words?\s*\)", re.I)


def parse_site_date(text: str | None) -> date | None:
    """'10 October 2026' / '10 Oct 2026' -> date (locale independent)."""
    m = DATE_RE.match(text or "")
    if not m:
        return None
    month = MONTHS.get(m.group(2).lower()[:3])
    if not month:
        return None
    try:
        return date(int(m.group(3)), month, int(m.group(1)))
    except ValueError:
        return None


def split_names(text: str) -> list[str]:
    parts = re.split(r"\s*(?:,|&|\band\b)\s*", clean(text))
    return list(dict.fromkeys(p for p in parts if p))


def sort_key(story: dict) -> str:
    """Matches the existing file order: lower-cased title, leading the/a/an ignored."""
    return re.sub(r"^(?:the|a|an)\s+", "", story["title"].lower())


# --------------------------------------------------------------------------- #
# Polite HTTP client
# --------------------------------------------------------------------------- #
class FetchError(RuntimeError):
    pass


class ParseError(ValueError):
    pass


class Fetcher:
    """requests wrapper: robots.txt check, delay between requests, retries."""

    def __init__(self, delay: float = 1.0, retries: int = 4, timeout: float = 30.0):
        self.session = requests.Session()
        self.session.headers["User-Agent"] = USER_AGENT
        self.delay, self.retries, self.timeout = delay, retries, timeout
        self._last = 0.0
        self.robots = self._load_robots()

    def _load_robots(self) -> robotparser.RobotFileParser:
        rp = robotparser.RobotFileParser()
        try:
            resp = self.session.get(BASE + "robots.txt", timeout=self.timeout)
            if resp.status_code == 200:
                rp.parse(resp.text.splitlines())
                rp.modified()
            else:
                rp.allow_all = True
        except requests.RequestException:
            rp.allow_all = True
        crawl_delay = rp.crawl_delay(USER_AGENT)
        if crawl_delay:
            self.delay = max(self.delay, float(crawl_delay))
        return rp

    def get(self, url: str) -> str | None:
        """Return page text, None on 404. Raises FetchError otherwise."""
        if not self.robots.can_fetch(USER_AGENT, url):
            raise FetchError(f"robots.txt disallows {url}")
        problem = "unknown error"
        for attempt in range(self.retries + 1):
            pause = self.delay - (time.monotonic() - self._last)
            if pause > 0:
                time.sleep(pause)
            retry_after = 0.0
            try:
                resp = self.session.get(url, timeout=self.timeout)
            except requests.RequestException as exc:
                problem = str(exc)
            else:
                if resp.status_code == 404:
                    self._last = time.monotonic()
                    return None
                if resp.status_code == 200:
                    if "charset" not in resp.headers.get("Content-Type", "").lower():
                        resp.encoding = "utf-8"
                    self._last = time.monotonic()
                    return resp.text
                problem = f"HTTP {resp.status_code}"
                if resp.status_code not in (429, 500, 502, 503, 504):
                    raise FetchError(f"{problem} for {url}")
                if resp.headers.get("Retry-After", "").isdigit():
                    retry_after = float(resp.headers["Retry-After"])
            self._last = time.monotonic()
            if attempt < self.retries:
                wait = max(retry_after, 2.0 ** (attempt + 1))
                log.warning("%s for %s - retrying in %.0fs", problem, url, wait)
                time.sleep(wait)
        raise FetchError(f"giving up on {url}: {problem}")


# --------------------------------------------------------------------------- #
# Parsing: the "What Was New" page
# --------------------------------------------------------------------------- #
@dataclass
class Batch:
    day: date
    hrefs: list[str] = field(default_factory=list)


def story_href(link: str, base_url: str) -> str | None:
    """Turn any link into 'Slug/index.html' if it points at a story page."""
    parsed = urlparse(urljoin(base_url, link))
    if parsed.netloc.lower().removeprefix("www.") != "mcstories.com":
        return None
    m = re.fullmatch(r"/([^/]+)/index\.html", parsed.path)
    if not m or m.group(1) in NON_STORY_DIRS:
        return None
    return f"{m.group(1)}/index.html"


def parse_news_page(html: str, base_url: str = NEWS_URL) -> list[Batch]:
    """Return the weekly batches (in page order, newest first) with the story
    links listed under each date heading ('Added ...' and 'Also added ...')."""
    soup = BeautifulSoup(html, "html.parser")
    batches: list[Batch] = []
    current: Batch | None = None
    for el in soup.descendants:
        if not isinstance(el, Tag):
            continue
        if el.name in {"h1", "h2", "h3", "h4", "h5", "h6", "p", "strong", "b"}:
            text = clean(el.get_text())
            if len(text) <= 25:
                day = parse_site_date(text)
                if day:
                    if current is None or current.day != day:
                        current = Batch(day)
                        batches.append(current)
                    continue
        if el.name == "a" and current is not None and el.get("href"):
            href = story_href(el["href"], base_url)
            if href and href not in current.hrefs:
                current.hrefs.append(href)
    return batches


# --------------------------------------------------------------------------- #
# Parsing: a story's index page
# --------------------------------------------------------------------------- #
def _site_relative(url: str) -> str:
    url = urldefrag(url)[0]
    return url[len(BASE):] if url.startswith(BASE) else url


def _parse_chapters(soup: BeautifulSoup, page_url: str) -> list[dict]:
    chapters: list[dict] = []

    # Layout A: a table (Chapter | Length | Added [| Updated]).
    for table in soup.find_all("table"):
        headers = [clean(th.get_text()).lower() for th in table.find_all("th")]
        author_col = next((i for i, h in enumerate(headers) if "author" in h), None)
        for row in table.find_all("tr"):
            link = next((a for a in row.find_all("a", href=True)
                         if "Authors/" not in a["href"]), None)
            if link is None:
                continue
            cells = row.find_all(["td", "th"])
            link_cell = link.find_parent(["td", "th"])
            words = None
            for cell in cells:
                if cell is link_cell:
                    continue
                m = WORDS_RE.search(cell.get_text())
                if m:
                    words = int(m.group(1).replace(",", ""))
                    break
            if words is None:
                raise ParseError(f"no word count in chapter row {clean(row.get_text())!r}")
            chapter = {"name": tidy(link.get_text()),
                       "href": _site_relative(urljoin(page_url, link["href"])),
                       "words": words}
            authors = [clean(a.get_text()) for a in row.find_all("a", href=re.compile(r"Authors/"))]
            if not authors and author_col is not None and author_col < len(cells):
                authors = split_names(cells[author_col].get_text())
            if authors:
                chapter["authors"] = list(dict.fromkeys(authors))
            chapters.append(chapter)
    if chapters:
        return chapters

    # Layout B (one-shot stories): <a>Title</a> (23416 words)
    for node in soup.find_all(string=PAREN_WORDS_RE):
        prev = node.previous_sibling
        while isinstance(prev, NavigableString) and not prev.strip():
            prev = prev.previous_sibling
        link = prev if isinstance(prev, Tag) and prev.name == "a" else node.parent.find("a", href=True)
        if link is None or not link.get("href"):
            continue
        words = int(PAREN_WORDS_RE.search(node).group(1).replace(",", ""))
        chapters.append({"name": tidy(link.get_text()),
                         "href": _site_relative(urljoin(page_url, link["href"])),
                         "words": words})
    return chapters


def parse_story_page(html: str, href: str, previous: dict | None = None) -> dict:
    """Build a stories.json record from a story's index.html."""
    soup = BeautifulSoup(html, "html.parser")
    page_url = urljoin(BASE, href)

    meta: dict[str, str] = {}
    for tag in soup.find_all("meta"):
        name = (tag.get("name") or tag.get("property") or "").lower()
        if name and tag.get("content") is not None:
            meta.setdefault(name, tag["content"])

    # Header block: <h3>Title</h3> <h3>by ...</h3> <h3>Added ...</h3> <h3>Updated ...</h3>
    title = None
    authors: list[str] = []
    added = updated = None
    for h in soup.find_all("h3"):
        text = tidy(h.get_text())
        linked = [clean(a.get_text()) for a in h.find_all("a", href=re.compile(r"Authors/"))]
        if linked and not authors:
            authors = list(dict.fromkeys(a for a in linked if a))
            continue
        m = re.match(r"^(Added|Updated)\s+(.+)$", text)
        if m and parse_site_date(m.group(2)):
            if m.group(1) == "Added":
                added = parse_site_date(m.group(2))
            else:
                updated = parse_site_date(m.group(2))
            continue
        if re.match(r"^Reader.s Picks?\b", text, re.I):
            continue
        if title is None:
            title = text                      # first remaining h3 is the title
        elif not authors and re.match(r"^by\s+", text, re.I):
            authors = split_names(text[3:])   # byline without author links
    title = title or tidy(soup.title.get_text() if soup.title else "")
    if not title:
        raise ParseError("title not found")
    if not authors and clean(meta.get("dcterms.creator")):
        authors = [clean(meta["dcterms.creator"])]
    if not authors:
        raise ParseError("author not found")

    if added is None and clean(meta.get("dcterms.date")):
        try:
            added = date.fromisoformat(clean(meta["dcterms.date"])[:10])
        except ValueError:
            pass
    if added is None and previous:
        added = date.fromisoformat(previous["date_added"])
    if added is None:
        raise ParseError("'Added' date not found")

    tags = set()
    for a in soup.find_all("a", href=True):
        m = re.search(r"(?:^|/)Tags/([A-Za-z0-9_-]+)\.html$", a["href"])
        if m and m.group(1).lower() != "index":
            tags.add(m.group(1).lower())
    if not tags and meta.get("dcterms.subject"):
        tags = set(meta["dcterms.subject"].lower().split())
    tags -= IGNORED_TAGS

    synopsis = tidy(meta.get("dcterms.description") or meta.get("description"))
    if not synopsis:
        for p in soup.find_all("p"):
            if clean(p.get_text()) and not p.find("a"):
                synopsis = tidy(p.get_text())
                break

    chapters = _parse_chapters(soup, page_url)
    if not chapters:
        raise ParseError("no chapters found (page layout changed?)")
    # Keep per-chapter authors (anthologies) if this parse could not see them.
    if previous:
        old_by_href = {c["href"]: c for c in previous.get("chapters", [])}
        for ch in chapters:
            old = old_by_href.get(ch["href"])
            if "authors" not in ch and old and "authors" in old:
                ch["authors"] = old["authors"]

    return {
        "href": href,
        "title": title,
        "tags": sorted(tags),
        "author": authors[0],
        "authors": authors,
        "synopsis": synopsis,
        "date_added": added.isoformat(),
        "date_updated": updated.isoformat() if updated else None,
        "chapters": chapters,
        "words": sum(c["words"] for c in chapters),
    }


# --------------------------------------------------------------------------- #
# JSON handling
# --------------------------------------------------------------------------- #
def load_json(path: str) -> dict:
    with open(path, encoding="utf-8") as fh:
        data = json.load(fh)
    if not isinstance(data, dict) or "stories" not in data or "tags" not in data:
        raise SystemExit(f"{path}: expected an object with 'tags' and 'stories'")
    return data


def dumps(data: dict) -> str:
    # Same formatting as the existing file: compact, real UTF-8, no trailing newline.
    return json.dumps(data, ensure_ascii=False, separators=(",", ":"))


def save_json(path: str, data: dict, backup: bool = True) -> None:
    if backup and os.path.exists(path):
        shutil.copy2(path, path + ".bak")
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(os.path.abspath(path)), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as fh:
            fh.write(dumps(data))
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.remove(tmp)
        raise


def latest_known_date(stories: list[dict]) -> date | None:
    days = []
    for s in stories:
        for key in ("date_added", "date_updated"):
            if s.get(key):
                try:
                    days.append(date.fromisoformat(s[key]))
                except ValueError:
                    log.warning("Ignoring bad %s %r in %r", key, s[key], s.get("title"))
    return max(days) if days else None


def keep_old_form(old: dict, new: dict) -> None:
    """Avoid churn: if a re-read value is equivalent to the stored one (same text
    apart from whitespace, same tags apart from duplicates), keep the stored form."""
    if set(old.get("tags", [])) == set(new["tags"]):
        new["tags"] = old["tags"]
    for key in ("title", "synopsis"):
        if clean(old.get(key)) == clean(new[key]):
            new[key] = old[key]
    old_chapters = {c["href"]: c for c in old.get("chapters", [])}
    for ch in new["chapters"]:
        prev = old_chapters.get(ch["href"])
        if prev and clean(prev["name"]) == clean(ch["name"]):
            ch["name"] = prev["name"]


def rebuild_tag_counts(data: dict) -> None:
    counts = Counter(t for s in data["stories"] for t in s["tags"])
    for code, n in counts.items():
        if code not in data["tags"]:
            log.warning("New tag %r found - added with the code as its label; "
                        "edit the label in the JSON.", code)
            data["tags"][code] = {"label": code, "count": n}
        else:
            data["tags"][code]["count"] = n
    for code, info in data["tags"].items():
        if code not in counts:
            info["count"] = 0


# --------------------------------------------------------------------------- #
# Main workflow
# --------------------------------------------------------------------------- #
def resolve_story_arg(arg: str) -> str:
    if arg.startswith("http"):
        href = story_href(arg, BASE)
        if href:
            return href
    slug = arg.strip("/ ").split("/")[0]
    return f"{slug}/index.html"


def check_one(fetcher: Fetcher, data: dict, arg: str) -> int:
    """Debug helper: parse one live story page and diff it against the JSON."""
    href = resolve_story_arg(arg)
    html = fetcher.get(BASE + href)
    if html is None:
        print(f"404: {BASE + href}")
        return 1
    existing = next((s for s in data["stories"] if s["href"] == href), None)
    new = parse_story_page(html, href, existing)
    if existing:
        keep_old_form(existing, new)
    print(json.dumps({k: v for k, v in new.items() if k != "chapters"}, indent=2, ensure_ascii=False))
    print(f"chapters parsed: {len(new['chapters'])}  (first: {new['chapters'][0]}, last: {new['chapters'][-1]})")
    if existing:
        print("\nDifferences vs. your JSON:")
        diffs = [k for k in new if existing.get(k) != new[k]]
        for k in diffs:
            old_v, new_v = existing.get(k), new[k]
            if k == "chapters":
                old_v, new_v = f"{len(old_v)} chapters", f"{len(new_v)} chapters"
            print(f"  {k}: {old_v!r} -> {new_v!r}")
        if not diffs:
            print("  none")
    return 0


def run(args: argparse.Namespace, fetcher: Fetcher | None = None) -> int:
    data = load_json(args.json_path)
    stories = data["stories"]
    index = {s["href"]: s for s in stories}
    fetcher = fetcher or Fetcher(delay=args.delay)

    if args.check:
        return check_one(fetcher, data, args.check)

    cutoff = args.since or latest_known_date(stories)
    if cutoff is None:
        raise SystemExit("Could not work out a starting date; pass --since YYYY-MM-DD")
    log.info("%s: %d stories, newest date %s", args.json_path, len(stories), cutoff)

    news = fetcher.get(NEWS_URL)
    if news is None:
        raise FetchError(f"{NEWS_URL} returned 404")
    batches = parse_news_page(news)
    if not batches:
        raise ParseError("no dated batches found on the news page (layout changed?)")
    new_batches = sorted((b for b in batches if b.day > cutoff), key=lambda b: b.day)
    if not new_batches:
        log.info("Up to date - newest batch on the site is %s.", max(b.day for b in batches))
        return 0

    oldest = min(b.day for b in batches)
    if oldest > cutoff + timedelta(days=8):
        log.warning("The news page only goes back to %s but your newest date is %s: "
                    "anything posted in between cannot be recovered by this script.", oldest, cutoff)
    log.info("%d new batch(es): %s", len(new_batches), ", ".join(str(b.day) for b in new_batches))

    # Each story page already reflects its latest state, so read each one once.
    links: dict[str, date] = {}
    for b in new_batches:
        for href in b.hrefs:
            links.setdefault(href, b.day)
    log.info("Reading %d story page(s)...", len(links))

    parsed: dict[str, dict] = {}
    failures: list[str] = []
    for n, href in enumerate(links, 1):
        log.debug("[%d/%d] %s", n, len(links), href)
        try:
            html = fetcher.get(BASE + href)
            if html is None:
                log.warning("404 for %s - skipped (story removed?)", href)
                continue
            parsed[href] = parse_story_page(html, href, index.get(href))
        except (FetchError, ParseError) as exc:
            log.error("%s: %s", href, exc)
            failures.append(href)

    if failures and not args.skip_failures:
        log.error("%d page(s) failed, nothing was written. Fix the problem and re-run "
                  "(or use --skip-failures to save the rest).", len(failures))
        return 1

    added, updated = 0, 0
    for href, new in parsed.items():
        old = index.get(href)
        if old is None:
            stories.append(new)
            index[href] = new
            added += 1
            log.info("+ %s - %s (%d chapter(s), %d words)", new["title"], new["author"],
                     len(new["chapters"]), new["words"])
            continue
        keep_old_form(old, new)
        if old == new:
            continue
        surprising = [k for k in new if k not in EXPECTED_CHANGES and old.get(k) != new[k]]
        if surprising:
            log.warning("  unexpected change in %s for %r: %s", ", ".join(surprising),
                        new["title"], "; ".join(f"{old.get(k)!r} -> {new[k]!r}" for k in surprising))
        delta = len(new["chapters"]) - len(old["chapters"])
        log.info("~ %s (%+d chapter(s), updated %s)", new["title"], delta, new["date_updated"])
        old.clear()
        old.update(new)
        updated += 1

    if not (added or updated):
        log.info("Pages read, but nothing differs from the JSON.")
        return 0

    stories.sort(key=sort_key)
    rebuild_tag_counts(data)
    log.info("%d new stor%s, %d updated.", added, "y" if added == 1 else "ies", updated)
    if args.dry_run:
        log.info("Dry run - %s was not modified.", args.json_path)
        return 1 if failures else 0
    save_json(args.json_path, data, backup=not args.no_backup)
    log.info("Saved %s%s", args.json_path, "" if args.no_backup else f" (previous version: {args.json_path}.bak)")
    return 1 if failures else 0


def iso_date(text: str) -> date:
    try:
        return date.fromisoformat(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"{text!r} is not a valid YYYY-MM-DD date") from None


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Update stories.json from mcstories.com/WhatWasNew.html")
    p.add_argument("json_path", nargs="?", default="stories.json", help="path to stories.json (default: ./stories.json)")
    p.add_argument("--dry-run", action="store_true", help="show what would change, write nothing")
    p.add_argument("--since", type=iso_date, metavar="YYYY-MM-DD", help="process batches dated after this day instead of the newest date in the JSON")
    p.add_argument("--delay", type=float, default=1.0, help="seconds between requests (default 1.0)")
    p.add_argument("--no-backup", action="store_true", help="don't write stories.json.bak")
    p.add_argument("--skip-failures", action="store_true", help="save what worked even if some pages failed (failed stories are NOT retried automatically)")
    p.add_argument("--check", metavar="STORY", help="debug: parse one live story (slug or URL) and diff it against the JSON")
    p.add_argument("-v", "--verbose", action="store_true")
    return p


def main(argv: list[str] | None = None, fetcher: Fetcher | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)-7s %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
    try:
        return run(args, fetcher)
    except (FetchError, ParseError) as exc:
        log.error("%s", exc)
        return 1


if __name__ == "__main__":
    sys.exit(main())
