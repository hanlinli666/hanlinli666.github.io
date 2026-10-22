#!/usr/bin/env python3
"""Fetch every enabled source in config/sources.json, apply the deterministic
keyword filter and write data/news.json + data/stats.json.

Four adapters, chosen per source:
  rss    — direct RSS/Atom (reuses the tolerant parser from fetch_news.py)
  rsshub — public RSSHub mirrors, tried in order
  json   — official JSON APIs, field paths declared in sources.json
  html   — list/home page link extraction (ministries, GitHub Trending)

No LLM is involved here. Every source failure is isolated: one dead feed never
blocks the rest, and if the whole run yields nothing the previous news.json is
left untouched.

    python scripts/daily/fetch_sources.py
    python scripts/daily/fetch_sources.py --only zhihu-hot,qbitai --verbose
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import urllib.parse
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from common import (  # noqa: E402
    CACHE_DIR,
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
from fetch_news import normalize_link, normalize_title, parse_feed  # noqa: E402

SOURCES_FILE = CONFIG_DIR / "sources.json"
KEYWORDS_FILE = CONFIG_DIR / "news_keywords.txt"
POLICY_KEYWORDS_FILE = CONFIG_DIR / "news_keywords_policy.txt"
NEWS_FILE = DATA_DIR / "news.json"
STATS_FILE = DATA_DIR / "stats.json"

LOOKBACK_DAYS = int(os.environ.get("NEWS_LOOKBACK_DAYS", "3"))
HISTORY_DAYS = int(os.environ.get("NEWS_HISTORY_DAYS", "7"))
PER_DAY_LIMIT = int(os.environ.get("NEWS_PER_DAY_LIMIT", "150"))
TOTAL_LIMIT = int(os.environ.get("NEWS_TOTAL_LIMIT", "600"))
SUMMARY_CHARS = int(os.environ.get("NEWS_SUMMARY_CHARS", "260"))
# 源本身不给摘要时（部委列表页、热榜），抓一次正文，取前 N 字当摘要
BODY_CHARS = int(os.environ.get("NEWS_BODY_CHARS", "200"))
BODY_LIMIT = int(os.environ.get("NEWS_BODY_LIMIT", "12"))
BODY_CACHE = CACHE_DIR / "news_body.json"
BODY_MIN_SUMMARY = 20
LEVEL_ORDER = {"L1": 0, "L2": 1, "L3": 2}
# GitHub Trending 里混着产品入口，形如 /features/copilot，不是仓库
GITHUB_RESERVED = {
    "features", "enterprise", "team", "topics", "collections", "trending", "apps",
    "marketplace", "sponsors", "login", "signup", "settings", "orgs", "users",
    "site", "about", "pricing", "security", "events", "readme", "customer-stories",
    "solutions", "resources", "education", "github", "contact", "join", "explore",
    "git-guides", "mobile", "codespaces", "copilot",
}


# --------------------------------------------------------------------- config


def load_sources(only: set[str] | None = None) -> list[dict]:
    data = read_json(SOURCES_FILE, {})
    sources = [s for s in data.get("sources", []) if isinstance(s, dict)]
    sources = [s for s in sources if s.get("enabled") and s.get("id")]
    if only:
        sources = [s for s in sources if s["id"] in only]
    # L1 first so that, when the same story appears in several sources, the
    # official one wins the de-duplication.
    sources.sort(key=lambda s: (LEVEL_ORDER.get(s.get("level", "L2"), 9), s["id"]))
    return sources


# ---------------------------------------------------------------- keyword rules


class Rule:
    __slots__ = ("label", "terms", "patterns", "limit", "title_only")

    def __init__(self, label: str, terms: list[str], patterns: list[re.Pattern],
                 limit: int | None = None, title_only: bool = False):
        self.label = label
        self.terms = terms
        self.patterns = patterns
        self.limit = limit
        self.title_only = title_only

    def match(self, haystack: str) -> bool:
        for term in self.terms:
            if term not in haystack:
                return False
        for pattern in self.patterns:
            if not pattern.search(haystack):
                return False
        return True


def _compile_start(raw: str) -> tuple[list[str], list[re.Pattern]]:
    """One rule: `+` joins AND terms; `/.../` chunks become case-insensitive regexes."""
    terms: list[str] = []
    patterns: list[re.Pattern] = []
    for part in raw.split("+"):
        part = part.strip()
        if not part:
            continue
        if part.startswith("/") and part.endswith("/") and len(part) > 2:
            try:
                patterns.append(re.compile(part[1:-1], re.IGNORECASE))
            except re.error:
                continue
        else:
            terms.append(part.lower())
    return terms, patterns


def load_rules(path: Path | None = None) -> tuple[list[Rule], list[Rule]]:
    """Returns (include rules, global excludes) for one keyword file."""
    path = path or KEYWORDS_FILE
    if not path.is_file():
        log("keyword", f"{path.name} missing - keeping everything")
        return [], []
    rules: list[Rule] = []
    excludes: list[Rule] = []
    in_global = False
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.upper().startswith("[GLOBAL_FILTER]"):
            in_global = True
            continue
        line = line.split("=>", 1)[0].strip()
        limit_match = re.search(r"@(\d+)\s*$", line)
        limit = int(limit_match.group(1)) if limit_match else None
        line = re.sub(r"@\d+\s*$", "", line).strip()
        if not line:
            continue
        negated = line.startswith("!")
        if negated:
            line = line[1:].strip()
        title_only = line.startswith("~")
        if title_only:
            line = line[1:].strip()
        terms, patterns = _compile_start(line)
        if not terms and not patterns:
            continue
        rule = Rule(line, terms, patterns, limit, title_only)
        if negated or in_global:
            excludes.append(rule)
        else:
            rules.append(rule)
    return rules, excludes


class DailyQuota:
    """`@N` means "at most N of this group per calendar day", persisted across runs.

    Without persistence, a 2-hourly job would let `美股 @2` through 24 times a day.
    """

    def __init__(self) -> None:
        self.path = CACHE_DIR / "quota.json"
        data = read_json(self.path, {})
        today = cn_day(now_utc())
        if not isinstance(data, dict) or data.get("date") != today:
            data = {"date": today, "counts": {}}
        self.data = data
        self.dirty = False

    def allow(self, rule: Rule) -> bool:
        if not rule.limit:
            return True
        return self.data["counts"].get(rule.label, 0) < rule.limit

    def take(self, rule: Rule) -> None:
        if not rule.limit:
            return
        self.data["counts"][rule.label] = self.data["counts"].get(rule.label, 0) + 1
        self.dirty = True

    def save(self) -> None:
        if self.dirty:
            write_json(self.path, self.data)


def keyword_check(item: dict, rules: list[Rule], excludes: list[Rule],
                  quota: DailyQuota) -> tuple[bool, list[str], Rule | None]:
    title = str(item.get("title", "")).lower()
    haystack = f"{title} {item.get('summary', '')}".lower()
    for rule in excludes:
        if rule.match(title if rule.title_only else haystack):
            return False, [], None
    for rule in rules:
        if rule.match(title if rule.title_only else haystack) and quota.allow(rule):
            return True, [rule.label], rule
    return False, [], None


# ------------------------------------------------------------------- adapters


def dot_get(obj, path: str):
    if not path:
        return None
    node = obj
    for part in str(path).split("."):
        if isinstance(node, dict):
            node = node.get(part)
        elif isinstance(node, list) and part.isdigit():
            node = node[int(part)] if int(part) < len(node) else None
        else:
            return None
        if node is None:
            return None
    return node


def render_template(template: str, obj) -> str:
    def repl(match: re.Match) -> str:
        value = dot_get(obj, match.group(1))
        return "" if value is None else str(value)

    return re.sub(r"\{([^{}]+)\}", repl, template or "")


def to_datetime(value, unit: str = "") -> datetime | None:
    if value in (None, ""):
        return None
    if unit in ("s", "ms"):
        try:
            number = float(value)
        except (TypeError, ValueError):
            return parse_dt(str(value))
        if unit == "ms" or number > 1e12:
            number /= 1000.0
        try:
            return datetime.fromtimestamp(number, tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    return parse_dt(str(value))


def parse_href_date(href: str, pattern: str) -> datetime | None:
    if not pattern:
        return None
    match = re.search(pattern, href)
    if not match:
        return None
    raw = match.group(1) if match.groups() else match.group(0)
    raw = re.sub(r"\D", "", raw)
    if len(raw) != 8:
        return None
    try:
        return datetime.strptime(raw, "%Y%m%d").replace(tzinfo=timezone(timedelta(hours=8)))
    except ValueError:
        return None


def base_item(src: dict, title: str, link: str, published: datetime | None, summary: str = "") -> dict:
    return {
        "title": collapse(strip_html(title)),
        "link": link,
        "published": iso(published) if published else None,
        "source": src.get("name") or src["id"],
        "summary": truncate(summary or "", SUMMARY_CHARS),
        "categories": [],
        "author": None,
        "level": src.get("level", "L2"),
        "source_id": src["id"],
    }


def fetch_rss(src: dict, verbose: bool) -> list[dict]:
    text = http_get(
        src["url"],
        timeout=float(src.get("timeout", 30)),
        retries=1,
        accept="application/rss+xml, application/xml, */*",
    )
    items = parse_feed(text, src.get("name") or src["id"], src["url"])
    return [base_item(src, i["title"], i["link"], parse_dt(i.get("published")), i.get("summary", "")) for i in items]


def fetch_rsshub(src: dict, verbose: bool) -> tuple[list[dict], str]:
    last_error: Exception | None = None
    for mirror in src.get("mirrors", []):
        url = mirror.rstrip("/") + src["route"]
        try:
            text = http_get(
                url,
                timeout=float(src.get("timeout", 60)),
                retries=1,
                accept="application/rss+xml, application/xml, */*",
            )
            items = parse_feed(text, src.get("name") or src["id"], url)
            if items:
                return [
                    base_item(src, i["title"], i["link"], parse_dt(i.get("published")), i.get("summary", ""))
                    for i in items
                ], mirror
            last_error = RuntimeError("mirror returned 0 items")
        except Exception as exc:  # noqa: BLE001 - try the next mirror
            last_error = exc
            if verbose:
                log("news", f"{src['id']}: {mirror} failed :: {str(exc)[:120]}")
    raise RuntimeError(f"all mirrors failed :: {last_error}")


def fetch_json(src: dict, verbose: bool) -> list[dict]:
    text = http_get(
        src["url"],
        timeout=float(src.get("timeout", 30)),
        retries=1,
        accept="application/json, */*",
    )
    data = json.loads(text)
    mapping = src.get("mapping") or {}
    rows = dot_get(data, mapping.get("items", ""))
    if not isinstance(rows, list):
        return []
    items: list[dict] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        title = dot_get(row, mapping.get("title", ""))
        if not title and mapping.get("title_fallback"):
            title = dot_get(row, mapping["title_fallback"])
        link = dot_get(row, mapping.get("link", ""))
        if not link and mapping.get("link_template"):
            link = render_template(mapping["link_template"], row)
        if not title or not link:
            continue
        published = to_datetime(dot_get(row, mapping.get("date", "")), mapping.get("date_unit", ""))
        summary = dot_get(row, mapping.get("summary", "")) or ""
        items.append(base_item(src, str(title), str(link), published, str(summary)))
    return items


def fetch_html(src: dict, verbose: bool) -> list[dict]:
    text = http_get(
        src["url"],
        timeout=float(src.get("timeout", 30)),
        retries=1,
        accept="text/html, */*",
    )
    base = src.get("base") or src["url"]
    include = src.get("include_href") or []
    href_regex = src.get("href_regex") or ""
    min_len = int(src.get("min_title_len", 6))
    date_pattern = src.get("date_pattern") or ""
    items: list[dict] = []

    if src.get("mode") == "github_trending":
        blocks = re.findall(r"(?is)<article\b[^>]*>(.*?)</article>", text)
        for block in blocks:
            for href, inner in re.findall(r"(?is)<a\b[^>]*href=[\"']([^\"']+)[\"'][^>]*>(.*?)</a>", block):
                if not re.fullmatch(r"/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", href):
                    continue
                owner = href.strip("/").split("/")[0].lower()
                if owner in GITHUB_RESERVED:
                    continue
                title = collapse(re.sub(r"<[^>]+>", " ", inner))
                title = re.sub(r"\s*/\s*", "/", title)
                if "/" not in title or len(title) < 3:
                    continue  # banner / chrome links never carry an "owner/repo" label
                items.append(base_item(src, title, urllib.parse.urljoin(base, href), None))
                break
        return items

    text = re.sub(r"(?is)<(script|style)\b.*?</\1>", " ", text)
    for href, inner in re.findall(r"(?is)<a\b[^>]*href=[\"']([^\"']+)[\"'][^>]*>(.*?)</a>", text):
        if href_regex and not re.search(href_regex, href):
            continue
        if include and not any(pattern in href for pattern in include):
            continue
        title = collapse(re.sub(r"<[^>]+>", " ", inner))
        if len(title) < min_len:
            continue
        link = urllib.parse.urljoin(base, href)
        published = parse_href_date(href, date_pattern)
        items.append(base_item(src, title, link, published))
    return items


# -------------------------------------------------------------------- runner


# 部委页面正文前常有一排「来源 / 发文字号 / 发布日期」元信息，不能当作摘要
META_LINE = re.compile(
    r"(来源：|类型：|发文机关|发文字号|成文日期|发布日期|发布机构|索引号|主题分类|"
    r"公开日期|作者：|发布时间：|责任编辑|浏览次数|打印本页|关闭窗口|网站地图|版权所有)"
)


def extract_body(html: str) -> str:
    """正文前 N 字：优先取 <p> 段落，过滤元信息；不足时退回 meta description。"""
    text = re.sub(r"(?is)<(script|style|nav|header|footer|aside|form|svg|noscript)\b.*?</\1>", " ", html)
    parts = [collapse(strip_html(chunk)) for chunk in re.findall(r"(?is)<p\b[^>]*>(.*?)</p>", text)]
    body = collapse(" ".join(p for p in parts if len(p) >= BODY_MIN_SUMMARY and not META_LINE.search(p)))
    if len(body) < 40:
        meta = re.search(
            r'<meta[^>]+(?:name|property)="(?:description|og:description)"[^>]+content="([^"]*)"',
            text, re.I,
        )
        body = collapse(meta.group(1)) if meta else body
    return truncate(body, BODY_CHARS)


def fill_summaries(items: list[dict], src_by_id: dict[str, dict], verbose: bool) -> int:
    """给没有摘要的条目补「正文前 200 字」；失败记进缓存，下一轮不重复抓。"""
    cache = read_json(BODY_CACHE, {})
    if not isinstance(cache, dict):
        cache = {}
    todo: list[tuple[str, dict]] = []
    for item in items:
        if len((item.get("summary") or "").strip()) >= BODY_MIN_SUMMARY:
            continue
        link = item.get("link") or ""
        if not link.startswith("http"):
            continue
        src = src_by_id.get(item.get("source_id")) or {}
        if src.get("body") is False or src.get("mode") == "github_trending":
            continue  # 热榜/榜单页是索引，正文不在这个链接里
        key = item.get("id") or normalize_link(link)
        entry = cache.get(key)
        if entry and not entry.get("ok"):
            continue
        todo.append((key, item))
    if not todo:
        return 0
    # L1 优先，其次更新；每轮最多抓 BODY_LIMIT 篇，避免拖慢 2 小时一轮的采集
    # （两次稳定排序：先按发布时间倒序，再按层级）
    todo.sort(key=lambda pair: pair[1].get("published") or "", reverse=True)
    todo.sort(key=lambda pair: LEVEL_ORDER.get(pair[1].get("level", "L3"), 9))
    todo = todo[:BODY_LIMIT]

    def work(pair: tuple[str, dict]) -> tuple[str, str]:
        key, item = pair
        try:
            return key, extract_body(http_get(item["link"], timeout=20, retries=1,
                                              accept="text/html, */*"))
        except Exception as exc:  # noqa: BLE001
            if verbose:
                log("body", f"{item['link'][:90]} -> {str(exc)[:100]}")
            return key, ""

    by_key = dict(todo)
    filled = 0
    with ThreadPoolExecutor(max_workers=6) as pool:
        for key, body in pool.map(work, todo):
            if len(body) >= BODY_MIN_SUMMARY:
                by_key[key]["summary"] = body
                filled += 1
                cache.pop(key, None)
            else:
                cache[key] = {"ok": False, "at": iso(now_utc())}
    if len(cache) > 800:
        for key in list(cache)[: len(cache) - 800]:
            cache.pop(key, None)
    write_json(BODY_CACHE, cache)
    return filled



def fetch_source(src: dict, verbose: bool) -> dict:
    started = time.monotonic()
    result = {"id": src["id"], "name": src.get("name", src["id"]), "level": src.get("level", "L2"),
              "ok": False, "items": [], "error": "", "mirror": "", "ms": 0}
    try:
        kind = src.get("type", "rss")
        if kind == "rss":
            result["items"] = fetch_rss(src, verbose)
        elif kind == "rsshub":
            result["items"], result["mirror"] = fetch_rsshub(src, verbose)
        elif kind == "json":
            result["items"] = fetch_json(src, verbose)
        elif kind == "html":
            result["items"] = fetch_html(src, verbose)
        else:
            raise RuntimeError(f"unknown type {kind}")
        result["ok"] = True
        if not result["items"]:
            result["error"] = "0 items"
    except Exception as exc:  # noqa: BLE001 - one source must not kill the run
        result["error"] = str(exc)[:200]
    max_items = int(src.get("max_items", 40))
    result["items"] = result["items"][:max_items]
    result["ms"] = int((time.monotonic() - started) * 1000)
    return result


def merge_previous(fresh: list[dict], cutoff: datetime, allowed_names: set[str]) -> list[dict]:
    previous = read_json(NEWS_FILE, [])
    if isinstance(previous, dict):
        previous = previous.get("items", [])
    merged: list[dict] = []
    seen: set[str] = set()
    for item in fresh:
        key = item.get("id") or normalize_link(item.get("link", ""))
        if key in seen:
            continue
        seen.add(key)
        merged.append(item)
    for item in previous if isinstance(previous, list) else []:
        if not isinstance(item, dict) or not item.get("link"):
            continue
        source_name = item.get("source")
        if source_name and source_name not in allowed_names:
            continue  # source was removed / renamed in sources.json
        published = parse_dt(item.get("published"))
        if published and published < cutoff:
            continue
        key = item.get("id") or normalize_link(item["link"])
        if key in seen:
            continue
        seen.add(key)
        merged.append(item)
    return merged


def main() -> None:
    parser = argparse.ArgumentParser(description="Fetch configured news sources")
    parser.add_argument("--only", default="", help="comma separated source ids")
    parser.add_argument("--dry-run", action="store_true", help="print the funnel, write nothing")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    load_local_env()
    only = {s.strip() for s in args.only.split(",") if s.strip()}
    sources = load_sources(only)
    if not sources:
        log("news", "no enabled sources - aborting")
        return
    rules, excludes = load_rules(KEYWORDS_FILE)
    policy_rules, policy_excludes = load_rules(POLICY_KEYWORDS_FILE)
    quota = DailyQuota()
    log("news", f"{len(sources)} sources · media {len(rules)} rules / policy {len(policy_rules)} rules "
                f"· quota {quota.data['counts'] or '-'}")

    results = []
    with ThreadPoolExecutor(max_workers=6) as pool:
        futures = {pool.submit(fetch_source, src, args.verbose): src for src in sources}
        for future in as_completed(futures):
            result = future.result()
            results.append(result)
            flag = "ok " if result["items"] else "0  "
            extra = f" via {result['mirror']}" if result["mirror"] else ""
            log("news", f"{flag}{result['name']:<18} {len(result['items']):>4} items "
                        f"{result['ms']:>6}ms{extra} {result['error'][:80]}")

    order = {src["id"]: i for i, src in enumerate(sources)}
    results.sort(key=lambda r: order.get(r["id"], 999))

    raw = 0
    fresh: list[dict] = []
    cutoff = now_utc() - timedelta(days=LOOKBACK_DAYS)
    seen_links: set[str] = set()
    seen_titles: set[str] = set()
    previous_ids: set[str] = set()
    previous_raw = read_json(NEWS_FILE, [])
    if isinstance(previous_raw, dict):
        previous_raw = previous_raw.get("items", [])
    for old in previous_raw if isinstance(previous_raw, list) else []:
        if isinstance(old, dict) and old.get("id"):
            previous_ids.add(str(old["id"]))
    source_stats = []

    for result in results:
        src = next(s for s in sources if s["id"] == result["id"])
        kept = 0
        raw += len(result["items"])
        for item in result["items"]:
            published = parse_dt(item.get("published"))
            estimated = published is None
            if estimated:
                published = now_utc()
                item["published"] = iso(published)
                item["_estimated"] = True
            if published < cutoff:
                continue
            key = normalize_link(item["link"])
            title_key = normalize_title(item["title"])
            if key in seen_links or (title_key and title_key in seen_titles):
                continue
            if key in previous_ids:
                continue  # already stored by an earlier run: do not burn quota again

            allowed = True
            matched: list[str] = []
            rule: Rule | None = None
            mode = src.get("filter", "keyword")
            if mode == "policy":
                allowed, matched, rule = keyword_check(item, policy_rules, policy_excludes, quota)
            elif mode != "all":
                allowed, matched, rule = keyword_check(item, rules, excludes, quota)
            else:
                haystack = f"{item.get('title', '')} {item.get('summary', '')}".lower()
                allowed = not any(rule_ex.match(haystack) for rule_ex in excludes)
                matched = ["*"]
            if not allowed:
                continue
            if rule is not None:
                quota.take(rule)

            seen_links.add(key)
            if title_key:
                seen_titles.add(title_key)
            item["id"] = key
            item["day"] = cn_day(published)
            item["matched"] = matched
            item["first_seen"] = item.get("first_seen") or iso(now_utc())
            fresh.append(item)
            kept += 1

        source_stats.append({
            "id": result["id"], "name": result["name"], "level": result["level"],
            "ok": result["ok"], "raw": len(result["items"]), "kept": kept,
            "ms": result["ms"], "mirror": result["mirror"], "error": result["error"],
        })

    merged = merge_previous(fresh, now_utc() - timedelta(days=HISTORY_DAYS),
                            {s.get("name", s["id"]) for s in sources})

    # keep the earlier timestamp for undated (hot-list) items across runs
    previous_published: dict[str, str] = {}
    prev = read_json(NEWS_FILE, [])
    for item in prev if isinstance(prev, list) else []:
        if isinstance(item, dict) and item.get("id") and item.get("published"):
            previous_published[item["id"]] = item["published"]
    for item in merged:
        if item.get("_estimated") and item.get("id") in previous_published:
            item["published"] = previous_published[item["id"]]
            published = parse_dt(item["published"])
            if published:
                item["day"] = cn_day(published)

    merged.sort(key=lambda i: i.get("published") or "", reverse=True)

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

    for item in capped:
        item.pop("_estimated", None)

    body_filled = 0
    if not args.dry_run:
        body_filled = fill_summaries(capped, {s["id"]: s for s in sources}, args.verbose)
        if body_filled:
            log("news", f"filled {body_filled} empty summaries from article body (<= {BODY_CHARS} chars)")

    today = cn_day(now_utc())
    stats = {
        "run_at": iso(now_utc()),
        "stages": {
            "sources": len(sources),
            "sources_ok": sum(1 for r in source_stats if r["raw"]),
            "raw": raw,
            "after_keywords": len(fresh),
            "merged": len(merged),
            "written": len(capped),
            "body_filled": body_filled,
            "new_today": len([i for i in capped if i.get("day") == today]),
        },
        "sources": source_stats,
    }

    if args.dry_run:
        print(json.dumps(stats, ensure_ascii=False, indent=2))
        log("news", f"dry run - raw {stats['stages']['raw']} -> keyword {len(fresh)} "
                    f"-> would write {len(capped)}")
        return

    if not capped:
        log("news", "nothing survived the pipeline - keeping the previous news.json untouched")
        write_json(STATS_FILE, stats)
        return

    write_json(NEWS_FILE, capped)
    write_json(STATS_FILE, stats)
    quota.save()

    meta = read_json(DATA_DIR / "meta.json", {})
    meta["news"] = {
        "generated_at": stats["run_at"],
        "count": len(capped),
        "new_today": stats["stages"]["new_today"],
        "sources": [s["name"] for s in source_stats if s["raw"]],
        "raw": raw,
        "candidates": len(fresh),
        "keywords": [r.label for r in rules][:20],
    }
    write_json(DATA_DIR / "meta.json", meta)

    stages = stats["stages"]
    log("news", f"done - raw {stages['raw']} -> keyword {stages['after_keywords']} "
                f"-> kept {stages['written']} (today {stages['new_today']})")
    dead = [s["name"] for s in source_stats if not s["raw"]]
    if dead:
        log("news", f"{len(dead)} sources returned nothing: {', '.join(dead)}")


if __name__ == "__main__":
    main()
