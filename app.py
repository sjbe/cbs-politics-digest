import json
import re
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from html import unescape
from zoneinfo import ZoneInfo

import feedparser
import requests
from bs4 import BeautifulSoup
from flask import Flask, render_template

ET = ZoneInfo("America/New_York")

RSS_URL = "https://www.cbsnews.com/latest/rss/politics"
POLITICS_INDEX_URLS = [
    "https://www.cbsnews.com/politics/",
    "https://www.cbsnews.com/politics/2/",
]
MAX_ENTRIES = 20
RSS_POOL_SIZE = 150
MAX_AGE_SECONDS = 48 * 3600
CACHE_TTL_SECONDS = 300
REQUEST_TIMEOUT = 10
USER_AGENT = "Mozilla/5.0 (compatible; CBSPoliticsDigest/1.0)"

app = Flask(__name__)
_cache = {"ts": 0.0, "entries": []}


NAME_PARTICLES = {
    "van", "von", "de", "del", "della", "di", "da", "la", "le", "der",
    "den", "ten", "ter", "du", "dos", "das", "do", "st", "st.", "saint",
    "mc", "mac", "el", "al", "bin", "ibn",
}


def last_name(full_name: str) -> str:
    name = re.sub(r"\s+", " ", full_name).strip()
    name = re.sub(r",.*$", "", name)  # drop ", CBS News" etc.
    parts = name.split(" ")
    if not parts:
        return name
    i = len(parts) - 1
    while i > 0 and parts[i - 1].lower().rstrip(".") in NAME_PARTICLES:
        i -= 1
    return " ".join(parts[i:])


def parse_iso(dt_str: str) -> datetime | None:
    if not dt_str:
        return None
    s = dt_str.strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def format_time(dt: datetime, with_zone: bool = False) -> str:
    local = dt.astimezone(ET)
    h = local.strftime("%I").lstrip("0") or "12"
    m = local.strftime("%M")
    ampm = local.strftime("%p").lower()
    ampm = f"{ampm[0]}.{ampm[1]}."
    s = f"{h}:{m} {ampm}"
    if with_zone:
        s += " ET"
    return s


def format_date(dt: datetime) -> str:
    return dt.astimezone(ET).strftime("%b %-d")


def extract_dates(soup: BeautifulSoup) -> tuple[datetime | None, datetime | None]:
    published = None
    modified = None
    for tag in soup.find_all("script", {"type": "application/ld+json"}):
        try:
            data = json.loads(tag.string or "")
        except (json.JSONDecodeError, TypeError):
            continue
        items = data if isinstance(data, list) else [data]
        for item in items:
            if not isinstance(item, dict):
                continue
            p = parse_iso(item.get("datePublished", ""))
            m = parse_iso(item.get("dateModified", ""))
            if p and not published:
                published = p
            if m and not modified:
                modified = m
    return published, modified


def extract_authors(soup: BeautifulSoup) -> list[str]:
    names: list[str] = []
    seen: set[str] = set()

    def add(n: str) -> None:
        n = re.sub(r"\s+", " ", n).strip()
        if n and n.lower() not in seen:
            seen.add(n.lower())
            names.append(n)

    for tag in soup.find_all("script", {"type": "application/ld+json"}):
        try:
            data = json.loads(tag.string or "")
        except (json.JSONDecodeError, TypeError):
            continue
        items = data if isinstance(data, list) else [data]
        for item in items:
            if not isinstance(item, dict):
                continue
            author = item.get("author")
            if not author:
                continue
            if isinstance(author, dict):
                author = [author]
            if isinstance(author, list):
                for a in author:
                    if isinstance(a, dict) and a.get("name"):
                        add(a["name"])
                    elif isinstance(a, str):
                        add(a)
            elif isinstance(author, str):
                add(author)

    body = soup.select_one(".content__body") or soup
    text_blocks = body.find_all(["p", "em", "i"])
    contrib_re = re.compile(
        r"(?:contribut(?:ed|ing)\s+to\s+this\s+report[:\.\s]*)([A-Z][^\.<]+)",
        re.IGNORECASE,
    )
    contrib_re2 = re.compile(
        r"^([A-Z][A-Za-z\.\-' ]+(?:,\s*[A-Z][A-Za-z\.\-' ]+)*)\s+contributed\s+to\s+this\s+report",
    )
    for el in text_blocks:
        txt = el.get_text(" ", strip=True)
        if "contribut" not in txt.lower():
            continue
        m = contrib_re2.match(txt) or contrib_re.search(txt)
        if not m:
            continue
        chunk = m.group(1)
        chunk = re.sub(r"\s+and\s+", ", ", chunk)
        for piece in chunk.split(","):
            piece = piece.strip().strip(".")
            if 2 <= len(piece.split()) <= 5 and piece[:1].isupper():
                add(piece)

    return names


DATELINE_RE = re.compile(
    r"^[A-Z][A-Za-z\.\-']*(?:[ \t]+[A-Z][A-Za-z\.\-']*){0,3}(?:,\s*[A-Z][A-Za-z\.\-' ]+)?\s+[—–-]+\s+"
)


def strip_dateline(text: str) -> str:
    return DATELINE_RE.sub("", text, count=1)


def extract_paragraphs(soup: BeautifulSoup, n: int = 3) -> list[str]:
    body = soup.select_one(".content__body")
    if not body:
        return []
    paragraphs = []
    for p in body.find_all("p", recursive=True):
        if p.find_parent(class_=re.compile(r"content__body--footer")):
            continue
        text = p.get_text(" ", strip=True)
        if not text:
            continue
        if re.match(r"^[A-Z][a-zA-Z\-' ]+ contributed to this report", text):
            continue
        paragraphs.append(text)
        if len(paragraphs) >= n:
            break
    if paragraphs:
        paragraphs[0] = strip_dateline(paragraphs[0])
    return paragraphs


def fetch_article(url: str, headline: str, summary: str) -> dict:
    try:
        resp = requests.get(
            url,
            timeout=REQUEST_TIMEOUT,
            headers={"User-Agent": USER_AGENT},
        )
        resp.raise_for_status()
    except requests.RequestException:
        return {
            "headline": headline,
            "url": url,
            "paragraphs": [summary] if summary else [],
            "authors_tag": "",
            "published_str": "",
            "updated_str": "",
            "published": None,
        }

    soup = BeautifulSoup(resp.text, "html.parser")
    if not headline:
        og = soup.find("meta", attrs={"property": "og:title"})
        if og and og.get("content"):
            headline = unescape(og["content"]).strip()
        else:
            h1 = soup.find("h1")
            if h1:
                headline = h1.get_text(" ", strip=True)
    paragraphs = extract_paragraphs(soup, 3)
    if not paragraphs and summary:
        paragraphs = [summary]
    authors = extract_authors(soup)
    last_names = [last_name(a) for a in authors if last_name(a)]
    authors_tag = f" ({', '.join(last_names)})" if last_names else ""

    published, modified = extract_dates(soup)
    today = datetime.now(ET).date()
    published_str = ""
    if published:
        pub_same = published.astimezone(ET).date() == today
        t = format_time(published, with_zone=True)
        published_str = f"Published {t}" if pub_same else f"Published {format_date(published)}, {t}"
    updated_str = ""
    if published and modified and (modified - published).total_seconds() > 120:
        mod_same = modified.astimezone(ET).date() == today
        t = format_time(modified)
        updated_str = f"Updated {t}" if mod_same else f"Updated {format_date(modified)}, {t}"

    return {
        "headline": headline,
        "url": url,
        "paragraphs": paragraphs,
        "authors_tag": authors_tag,
        "published_str": published_str,
        "updated_str": updated_str,
        "published": published,
    }


def is_article_url(url: str) -> bool:
    return "/news/" in url


def fetch_index_links(url: str) -> list[str]:
    try:
        resp = requests.get(url, timeout=REQUEST_TIMEOUT, headers={"User-Agent": USER_AGENT})
        resp.raise_for_status()
    except requests.RequestException:
        return []
    html = resp.text
    m = re.search(r'<section[^>]*class="[^"]*list-river[^"]*"[^>]*>', html)
    if not m:
        return []
    chunk = html[m.start():]
    next_section = re.search(r'<section[^>]*class="[^"]*component[^"]*"', chunk[10:])
    end = next_section.start() + 10 if next_section else len(chunk)
    section = chunk[:end]
    found = re.findall(
        r'href="https://www\.cbsnews\.com/(?:[a-z]+/)?news/([a-z0-9\-]+/)"',
        section,
    )
    return [f"https://www.cbsnews.com/news/{slug}" for slug in found]


def load_entries() -> list[dict]:
    feed = feedparser.parse(RSS_URL)
    items: list[tuple[str, str, str]] = []
    seen_urls: set[str] = set()
    for e in feed.entries:
        link = e.get("link", "")
        if not is_article_url(link) or link in seen_urls:
            continue
        title = unescape(e.get("title", "")).strip()
        if "transcript" in title.lower():
            continue
        summary = unescape(re.sub(r"<[^>]+>", "", e.get("summary", ""))).strip()
        items.append((link, title, summary))
        seen_urls.add(link)
        if len(items) >= RSS_POOL_SIZE:
            break

    for index_url in POLITICS_INDEX_URLS:
        for link in fetch_index_links(index_url):
            if link in seen_urls:
                continue
            items.append((link, "", ""))
            seen_urls.add(link)

    with ThreadPoolExecutor(max_workers=8) as ex:
        results = list(
            ex.map(lambda t: fetch_article(t[0], t[1], t[2]), items)
        )

    now = datetime.now(timezone.utc)
    fresh = []
    for r in results:
        pub = r.get("published")
        if pub and (now - pub).total_seconds() > MAX_AGE_SECONDS:
            continue
        if "transcript" in r.get("headline", "").lower():
            continue
        if not r.get("headline"):
            continue
        fresh.append(r)

    fresh.sort(
        key=lambda r: r["published"] or datetime.min.replace(tzinfo=timezone.utc),
        reverse=True,
    )
    return fresh[:MAX_ENTRIES]


def get_entries(force: bool = False) -> list[dict]:
    now = time.time()
    if not force and _cache["entries"] and (now - _cache["ts"]) < CACHE_TTL_SECONDS:
        return _cache["entries"]
    entries = load_entries()
    _cache["entries"] = entries
    _cache["ts"] = now
    return entries


@app.route("/")
def index():
    entries = get_entries()
    age = int(time.time() - _cache["ts"]) if _cache["ts"] else 0
    return render_template("index.html", entries=entries, age_seconds=age)


@app.route("/refresh", methods=["POST", "GET"])
def refresh():
    get_entries(force=True)
    from flask import redirect, url_for
    return redirect(url_for("index"))


if __name__ == "__main__":
    import os
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
