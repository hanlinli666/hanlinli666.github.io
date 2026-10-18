#!/usr/bin/env python3
"""Fetch the day's tech / AI news from the RSS sources listed in
config/news_sources.txt and write data/news.json.

Pure standard library: a hand-rolled RSS 2.0 + Atom reader that tolerates the
usual real-world breakage (missing dates, CDATA, relative links, GBK feeds).

Rules (see 开发文档.md): newest first, de-duplicated by link, max 30 items per
Beijing calendar day, and arXiv links that already appear in data/papers.json
are dropped.
"""

from __future__ import annotations

import os
import re
import sys
import urllib.parse
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import timedelta
from pathlib import Path
import xml.etree.ElementTree as ET

sys.path.insert(0, str(Path(__file__).resolve().parent))

from common import (  # noqa: E402
    CONFIG_DIR,
    DATA_DIR,
    ROOT,
    cn_day,
    collapse,
    http_get,
    iso,
    load_local_env,
    log,
    now_utc,
    parse_dt,
    read_json,
    strip_html,
    truncate,
    write_json,
)

LOOKBACK_DAYS = int(os.environ.get("NEWS_LOOKBACK_DAYS", "7"))
PER_DAY_LIMIT = int(os.environ.get("NEWS_PER_DAY_LIMIT", "30"))
TOTAL_LIMIT = int(os.environ.get("NEWS_TOTAL_LIMIT", "160"))
SUMMARY_CHARS = 220

TRACKING_PARAMS = re.compile(
    r"^(utm_\w+|fbclid|gclid|mc_cid|mc_eid|ref|ref_src|source|guccounter|__twitter_impression)$",
    re.IGNORECASE,
)


# --------------------------------------------------------------- source config


def load_sources() -> list[tuple[str, str]]:
    path = CONFIG_DIR / "news_sources.txt"
    if not path.is_file():
        log("news", f"{path.relative_to(ROOT)} missing")
        return []
    sources: list[tuple[str, str]] = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, _, url = line.partition("=")
        name = name.strip()
        url = url.strip()
        if not url.startswith("http"):
            continue
        if name.startswith("!"):
            continue  # explicitly disabled
        sources.append((name, url))
    return sources


# -------------------------------------------------------------------- parsing


def local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1].lower()


def first_child(node: ET.Element, *names: str) -> ET.Element | None:
    wanted = {n.lower() for n in names}
    for child in node:
        if local_name(child.tag) in wanted:
            return child
    return None


def child_text(node: ET.Element, *names: str) -> str:
    child = first_child(node, *names)
    if child is None:
        return ""
    return collapse("".join(child.itertext()))


def child_html(node: ET.Element, *names: str) -> str:
    child = first_child(node, *names)
    if child is None:
        return ""
    return "".join(child.itertext())


def find_link(node: ET.Element, base: str) -> str:
    for child in node:
        if local_name(child.tag) != "link":
            continue
        href = child.get("href")
        rel = (child.get("rel") or "alternate").lower()
        if href and rel in ("alternate", ""):
            return urllib.parse.urljoin(base, href.strip())
        if not href:
            text = collapse("".join(child.itertext()))
            if text:
                return urllib.parse.urljoin(base, text)
    for child in node:
        if local_name(child.tag) == "guid":
            text = collapse("".join(child.itertext()))
            if text.startswith("http"):
                return text
    return ""


def normalize_link(link: str) -> str:
    try:
        parts = urllib.parse.urlsplit(link.strip())
    except ValueError:
        return link.strip()
    query = [
        (k, v)
        for k, v in urllib.parse.parse_qsl(parts.query, keep_blank_values=True)
        if not TRACKING_PARAMS.match(k)
    ]
    path = parts.path.rstrip("/") or "/"
    netloc = parts.netloc.lower()
    if netloc.startswith("www."):
        netloc = netloc[4:]
    return urllib.parse.urlunsplit((parts.scheme or "https", netloc, path, urllib.parse.urlencode(query), ""))


def feed_title(root: ET.Element) -> str:
    channel = first_child(root, "channel")
    container = channel if channel is not None else root
    return child_text(container, "title")


def sanitize_xml(text: str) -> str:
    """Fix the two breakages seen in the wild: stray '&' and raw control chars."""
    text = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", text)
    return re.sub(r"&(?!(?:#\d+|#x[0-9a-fA-F]+|[A-Za-z][A-Za-z0-9]*);)", "&amp;", text)


def parse_feed(xml_text: str, source_name: str, feed_url: str) -> list[dict]:
    # The text is already decoded, so drop any <?xml ... encoding=...?> prologue:
    # ElementTree would otherwise trust a stale declaration (e.g. gb2312).
    cleaned = re.sub(r"^\s*<\?xml[^>]*\?>", "", xml_text, count=1)
    root = None
    for candidate in (cleaned, sanitize_xml(cleaned)):
        try:
            root = ET.fromstring(candidate.strip())
            break
        except ET.ParseError:
            start = candidate.find("<")
            try:
                root = ET.fromstring(candidate[start:].strip())
                break
            except ET.ParseError:
                continue
    if root is None:
        log("news", f"{source_name}: XML parse error, source skipped")
        return []

    channel = first_child(root, "channel")
    container = channel if channel is not None else root
    base = feed_base(container, feed_url)

    nodes = [n for n in container if local_name(n.tag) in ("item", "entry")]
    if not nodes:
        nodes = [n for n in root.iter() if local_name(n.tag) in ("item", "entry")]

    items: list[dict] = []
    for node in nodes:
        title = collapse(strip_html(child_text(node, "title")))
        link = find_link(node, base)
        if not title or not link:
            continue
        published = (
            parse_dt(child_text(node, "pubdate"))
            or parse_dt(child_text(node, "published"))
            or parse_dt(child_text(node, "updated"))
            or parse_dt(child_text(node, "date"))
            or parse_dt(child_text(node, "created"))
        )
        summary = clean_summary(
            strip_html(child_html(node, "description", "summary", "content", "encoded") or "")
        )
        category_nodes = [c for c in node if local_name(c.tag) == "category"]
        categories = []
        for cat in category_nodes[:4]:
            term = cat.get("term") or collapse("".join(cat.itertext()))
            if term:
                categories.append(collapse(term))
        author = child_text(node, "author", "creator")
        if not author:
            author_node = first_child(node, "author")
            if author_node is not None:
                author = child_text(author_node, "name") or ""
        items.append(
            {
                "title": title,
                "link": link,
                "published": iso(published) if published else None,
                "source": source_name,
                "summary": truncate(summary, SUMMARY_CHARS),
                "categories": categories,
                "author": collapse(author) or None,
                "_has_date": published is not None,
            }
        )
    return items


def clean_summary(text: str) -> str:
    """Drop the boilerplate that HNRSS and friends inject into <description>."""
    if not text:
        return ""
    value = re.sub(r"(?i)article url:\s*\S+", " ", text)
    value = re.sub(r"(?i)comments url:\s*\S+", " ", value)
    value = re.sub(r"(?i)\bpoints:\s*\d+", " ", value)
    value = re.sub(r"(?i)#\s*comments:\s*\d+", " ", value)
    value = re.sub(r"(?i)\bdiscussion\b\s*$", " ", value)
    return collapse(value)


def feed_base(container: ET.Element, feed_url: str) -> str:
    for name in ("link", "id"):
        child = first_child(container, name)
        if child is None:
            continue
        text = child.get("href") or collapse("".join(child.itertext()))
        if text.startswith("http"):
            return text
    return feed_url


def fetch_source(name: str, url: str) -> list[dict]:
    try:
        xml_text = http_get(url, timeout=30, retries=2, accept="application/rss+xml, application/xml, */*")
    except Exception as exc:  # noqa: BLE001
        log("news", f"{name}: fetch failed :: {exc}")
        return []
    items = parse_feed(xml_text, name, url)
    dated = sum(1 for i in items if i["_has_date"])
    log("news", f"{name}: {len(items)} items ({dated} with date)")
    return items


# ----------------------------------------------------------------------- main


def load_paper_index() -> tuple[set[str], set[str]]:
    papers = read_json(DATA_DIR / "papers.json", [])
    if isinstance(papers, dict):
        papers = papers.get("items", [])
    ids: set[str] = set()
    titles: set[str] = set()
    for paper in papers if isinstance(papers, list) else []:
        if not isinstance(paper, dict):
            continue
        if paper.get("id"):
            ids.add(str(paper["id"]).lower())
        if paper.get("title"):
            titles.add(normalize_title(paper["title"]))
    return ids, titles


def normalize_title(title: str) -> str:
    return re.sub(r"[^a-z0-9\u4e00-\u9fff]+", "", (title or "").lower())


def is_arxiv_link(link: str) -> bool:
    return "arxiv.org" in link.lower()


def main() -> None:
    load_local_env()
    sources = load_sources()
    if not sources:
        log("news", "no enabled sources - aborting")
        sys.exit(0)
    log("news", f"fetching {len(sources)} sources in parallel")

    collected: list[dict] = []
    with ThreadPoolExecutor(max_workers=6) as pool:
        futures = {pool.submit(fetch_source, name, url): name for name, url in sources}
        for future in as_completed(futures):
            name = futures[future]
            try:
                collected.extend(future.result())
            except Exception as exc:  # noqa: BLE001
                log("news", f"{name}: unexpected error {exc}")

    # ---- filter: recency, dedupe, arXiv overlap
    cutoff = now_utc() - timedelta(days=LOOKBACK_DAYS)
    paper_ids, paper_titles = load_paper_index()

    seen_links: set[str] = set()
    seen_titles: set[str] = set()
    kept: list[dict] = []
    for item in collected:
        published = parse_dt(item.get("published"))
        if published is None:
            # undated item: keep, but date it now so it still shows up
            item["published"] = iso(now_utc())
            published = parse_dt(item["published"])
        if published < cutoff:
            continue
        key = normalize_link(item["link"])
        title_key = normalize_title(item["title"])
        if key in seen_links or (title_key and title_key in seen_titles):
            continue
        if is_arxiv_link(item["link"]):
            arxiv_id = re.search(r"abs/([^v?#]+)", item["link"])
            if (arxiv_id and arxiv_id.group(1).lower() in paper_ids) or title_key in paper_titles:
                continue
        seen_links.add(key)
        if title_key:
            seen_titles.add(title_key)
        item["id"] = key
        item["day"] = cn_day(published)
        item.pop("_has_date", None)
        kept.append(item)

    # ---- merge with the previous file so the PWA keeps a few days of history
    allowed_sources = {name for name, _ in sources}
    previous = read_json(DATA_DIR / "news.json", [])
    if isinstance(previous, dict):
        previous = previous.get("items", [])
    merged: list[dict] = []
    seen = set()
    for item in list(kept) + list(previous if isinstance(previous, list) else []):
        if not isinstance(item, dict) or not item.get("link"):
            continue
        # a source that was removed / disabled disappears from the archive too
        if item.get("source") and item["source"] not in allowed_sources:
            continue
        published = parse_dt(item.get("published"))
        if published is None or published < cutoff:
            continue
        key = item.get("id") or normalize_link(item["link"])
        if key in seen:
            continue
        seen.add(key)
        item["id"] = key
        item["day"] = cn_day(published)
        merged.append(item)

    merged.sort(key=lambda i: i.get("published") or "", reverse=True)

    # ---- cap: 30 per day, 160 overall
    per_day: dict[str, int] = {}
    capped: list[dict] = []
    for item in merged:
        day = item.get("day") or cn_day(now_utc())
        if per_day.get(day, 0) >= PER_DAY_LIMIT:
            continue
        per_day[day] = per_day.get(day, 0) + 1
        capped.append(item)
        if len(capped) >= TOTAL_LIMIT:
            break

    write_json(DATA_DIR / "news.json", capped)

    meta = read_json(DATA_DIR / "meta.json", {})
    meta["news"] = {
        "generated_at": iso(now_utc()),
        "count": len(capped),
        "new_today": len([i for i in capped if i.get("day") == cn_day(now_utc())]),
        "sources": [name for name, _ in sources],
    }
    write_json(DATA_DIR / "meta.json", meta)
    log("news", f"done - {len(capped)} news items from {len(sources)} sources")


if __name__ == "__main__":
    main()
