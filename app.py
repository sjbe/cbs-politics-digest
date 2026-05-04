import json
import re
import time
from concurrent.futures import ThreadPoolExecutor
from html import unescape

import feedparser
import requests
from bs4 import BeautifulSoup
from flask import Flask, render_template

RSS_URL = "https://www.cbsnews.com/latest/rss/politics"
MAX_ENTRIES = 20
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
        }

    soup = BeautifulSoup(resp.text, "html.parser")
    paragraphs = extract_paragraphs(soup, 3)
    if not paragraphs and summary:
        paragraphs = [summary]
    authors = extract_authors(soup)
    last_names = [last_name(a) for a in authors if last_name(a)]
    authors_tag = f" ({', '.join(last_names)})" if last_names else ""
    return {
        "headline": headline,
        "url": url,
        "paragraphs": paragraphs,
        "authors_tag": authors_tag,
    }


def is_article_url(url: str) -> bool:
    return "/news/" in url


def load_entries() -> list[dict]:
    feed = feedparser.parse(RSS_URL)
    items = []
    for e in feed.entries:
        link = e.get("link", "")
        if not is_article_url(link):
            continue
        title = unescape(e.get("title", "")).strip()
        if "transcript" in title.lower():
            continue
        summary = unescape(re.sub(r"<[^>]+>", "", e.get("summary", ""))).strip()
        items.append((link, title, summary))
        if len(items) >= MAX_ENTRIES:
            break

    with ThreadPoolExecutor(max_workers=8) as ex:
        results = list(
            ex.map(lambda t: fetch_article(t[0], t[1], t[2]), items)
        )
    return results


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
