#!/usr/bin/env python3
"""Fetch AI papers from the arXiv API, score them against keywords.txt and
enrich them with Chinese titles / summaries.

LLM backend (first key found wins):
  BIGMODEL_API_KEY / ZHIPUAI_API_KEY  → 智谱 GLM (default glm-4.7, thinking off)
  DASHSCOPE_API_KEY / MODELSCOPE_API_KEY → 阿里云百炼 qwen-plus

Output: daily/data/papers.json  (a plain JSON array, consumed by the PWA)
        daily/data/meta.json    (generation stamps + counters for the UI)

Only the standard library is used, so GitHub Actions needs no `pip install`.
"""

from __future__ import annotations

import json
import os
import re
import sys
import threading
import time
import urllib.parse
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from common import (  # noqa: E402
    CN_TZ,
    CACHE_DIR,
    DATA_DIR,
    KEYWORDS_FILE,
    ROOT,
    collapse,
    cn_day,
    http_get,
    http_post_json,
    iso,
    load_local_env,
    log,
    now_utc,
    parse_dt,
    read_json,
    truncate,
    write_json,
)

ARXIV_API = "https://export.arxiv.org/api/query"
NS = {
    "atom": "http://www.w3.org/2005/Atom",
    "arxiv": "http://arxiv.org/schemas/atom",
    "opensearch": "http://a9.com/-/spec/opensearch/1.1/",
}

LOOKBACK_DAYS = int(os.environ.get("PAPERS_LOOKBACK_DAYS", "4"))
HISTORY_DAYS = int(os.environ.get("PAPERS_HISTORY_DAYS", "21"))
MAX_RESULTS = int(os.environ.get("PAPERS_MAX_RESULTS", "220"))
PAGE_SIZE = 100
SUMMARIZE_LIMIT = int(os.environ.get("SUMMARIZE_LIMIT", "40"))

# 免费额度有速率限制（429 code=1302），所以请求之间要留间隔并允许退避重试。
LLM_MIN_INTERVAL = float(os.environ.get("LLM_MIN_INTERVAL", "4.0"))  # 两次请求之间的最小间隔（秒）
LLM_WORKERS = int(os.environ.get("LLM_WORKERS", "3"))
LLM_ATTEMPTS = int(os.environ.get("LLM_ATTEMPTS", "5"))

# 是否让 GLM-4.7 开「思考」：off 省钱省时（默认）| on 全开 | auto 只给最相关的 N 篇开
THINKING_MODE = (os.environ.get("LLM_THINKING") or os.environ.get("BIGMODEL_THINKING") or "off").lower()
THINK_TOP_N = int(os.environ.get("LLM_THINK_TOP_N", "8"))

CATEGORY_BONUS = {
    "cs.AI": 6,
    "cs.CL": 6,
    "cs.LG": 4,
    "cs.MA": 5,
    "cs.SE": 3,
    "cs.HC": 2,
    "cs.IR": 3,
    "cs.CV": 2,
}


# ---------------------------------------------------------------- keyword file


def load_keyword_branches() -> list[str]:
    """Each non-comment line is one OR branch, e.g. `LLM agent` or `agent + tool`."""
    path = KEYWORDS_FILE
    if not path.is_file():
        return ["LLM agent", "AI agent"]
    branches: list[str] = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        branches.append(line)
    return branches or ["LLM agent"]


def branch_terms(branch: str) -> list[str]:
    """`a + "b c"` -> ['a', 'b c']; a bare multi-word line is treated as a phrase."""
    parts = [p.strip() for p in branch.split("+") if p.strip()]
    terms: list[str] = []
    for part in parts:
        part = part.strip().strip('"').strip()
        if part:
            terms.append(part)
    return terms


def build_query(branches: list[str]) -> str:
    or_parts: list[str] = []
    for branch in branches:
        terms = branch_terms(branch)
        if not terms:
            continue
        and_parts = []
        for term in terms:
            escaped = term.replace('"', "")
            and_parts.append(f'(ti:"{escaped}" OR abs:"{escaped}")')
        or_parts.append("(" + " AND ".join(and_parts) + ")")
    return " OR ".join(or_parts)


# ---------------------------------------------------------------- arXiv access


def fetch_arxiv(query: str) -> list[dict]:
    entries: list[dict] = []
    cutoff = now_utc() - timedelta(days=LOOKBACK_DAYS)
    start = 0
    while start < MAX_RESULTS:
        params = {
            "search_query": query,
            "start": str(start),
            "max_results": str(min(PAGE_SIZE, MAX_RESULTS - start)),
            "sortBy": "submittedDate",
            "sortOrder": "descending",
        }
        url = ARXIV_API + "?" + urllib.parse.urlencode(params)
        log("papers", f"arXiv page start={start} ({min(PAGE_SIZE, MAX_RESULTS - start)} entries)")
        try:
            xml_text = http_get(url, timeout=45, retries=3, accept="application/atom+xml")
        except Exception as exc:  # noqa: BLE001
            log("papers", f"arXiv request failed, keeping what we have: {exc}")
            break

        root = ET.fromstring(xml_text)
        page = [parse_entry(node) for node in root.findall("atom:entry", NS)]
        page = [item for item in page if item]
        entries.extend(page)

        oldest = min((parse_dt(item["published"]) for item in page if item.get("published")), default=None)
        if len(page) < PAGE_SIZE or oldest is None or oldest < cutoff:
            break
        start += PAGE_SIZE
        time.sleep(3.0)  # arXiv asks for >= 3s between API calls

    # keep only recent submissions
    fresh = []
    for item in entries:
        published = parse_dt(item.get("published"))
        if published is None or published >= cutoff:
            fresh.append(item)
    log("papers", f"arXiv returned {len(entries)} entries, {len(fresh)} within last {LOOKBACK_DAYS}d")
    return fresh


def parse_entry(node: ET.Element) -> dict | None:
    raw_id = (node.findtext("atom:id", default="", namespaces=NS) or "").strip()
    if not raw_id:
        return None
    match = re.search(r"abs/([^v]+?)(v\d+)?$", raw_id)
    short_id = match.group(1) if match else raw_id.rsplit("/", 1)[-1]
    short_id = short_id.split("v")[0] if re.fullmatch(r"[\d.]+v\d+", short_id) else short_id

    title = collapse(node.findtext("atom:title", default="", namespaces=NS))
    abstract = collapse(node.findtext("atom:summary", default="", namespaces=NS))
    if not title:
        return None

    published = (node.findtext("atom:published", default="", namespaces=NS) or "").strip()
    updated = (node.findtext("atom:updated", default="", namespaces=NS) or "").strip()
    published_dt = parse_dt(published) or parse_dt(updated) or now_utc()

    authors = [
        collapse(a.findtext("atom:name", default="", namespaces=NS))
        for a in node.findall("atom:author", NS)
    ]
    authors = [a for a in authors if a]

    categories = [c.get("term", "") for c in node.findall("atom:category", NS) if c.get("term")]
    primary = node.find("arxiv:primary_category", NS)
    primary_term = primary.get("term") if primary is not None else (categories[0] if categories else "")

    comment = collapse(node.findtext("arxiv:comment", default="", namespaces=NS))
    journal = collapse(node.findtext("arxiv:journal_ref", default="", namespaces=NS))

    return {
        "id": short_id,
        "title": title,
        "title_zh": None,
        "abstract": abstract,
        "summary_zh": None,
        "authors": authors[:12],
        "author_count": len(authors),
        "published": iso(published_dt),
        "updated": iso(parse_dt(updated) or published_dt),
        "day": cn_day(published_dt),
        "categories": categories[:6],
        "primary_category": primary_term,
        "url": f"https://arxiv.org/abs/{short_id}",
        "pdf": f"https://arxiv.org/pdf/{short_id}",
        "comment": comment,
        "journal_ref": journal,
    }


# --------------------------------------------------------------------- scoring


def score_item(item: dict, branches: list[str]) -> dict:
    title_l = item["title"].lower()
    abstract_l = item["abstract"].lower()
    matched: list[str] = []
    score = 0
    for branch in branches:
        terms = branch_terms(branch)
        if not terms:
            continue
        hit_title = 0
        hit_abstract = 0
        for term in terms:
            needle = term.lower()
            if needle in title_l:
                hit_title += 1
            elif needle in abstract_l:
                hit_abstract += 1
        if hit_title or hit_abstract:
            matched.append(branch)
            score += 10 * hit_title + 3 * hit_abstract
    score += CATEGORY_BONUS.get(item.get("primary_category", ""), 0)
    if item.get("comment"):
        score += 1
    if len(item.get("authors") or []) > 6:
        score += 1
    item["matched"] = matched
    item["score"] = score
    return item


# ------------------------------------------------------------ AI enrichment


def _extract_json(text: str) -> dict | None:
    if not text:
        return None
    cleaned = text.strip()
    cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", cleaned, flags=re.MULTILINE).strip()
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        pass
    match = re.search(r"\{.*\}", cleaned, flags=re.DOTALL)
    if match:
        try:
            return json.loads(match.group(0))
        except json.JSONDecodeError:
            return None
    return None


def build_prompt(item: dict) -> str:
    return (
        "你是一名严谨的中文科技编辑。请阅读下面的 arXiv 论文信息，输出严格 JSON（不要 markdown 代码块）：\n"
        '{"title_zh": "准确、简洁的中文标题（不超过 40 字）",'
        ' "summary_zh": ["要点一", "要点二", "要点三"],'
        ' "tags": ["中文关键词1", "中文关键词2", "中文关键词3"]}\n'
        "要求：\n"
        "1. summary_zh 写 3 条，每条不超过 60 字，讲清楚【问题 → 方法 → 结论/贡献】，不要复述标题；\n"
        "2. 使用平实的中文，保留必要的英文术语（如 LLM、agent、RAG）；\n"
        "3. 只基于给定摘要，不要编造数据或结论；信息不足时宁可概括表述；\n"
        "4. tags 为 2-4 个中文主题词。\n\n"
        f"英文标题：{item['title']}\n"
        f"作者数：{item.get('author_count', 0)}\n"
        f"arXiv 分类：{item.get('primary_category', '')}\n"
        f"英文摘要：{truncate(item['abstract'], 1800)}"
    )


def resolve_llm() -> dict | None:
    """Pick the summarisation backend from the environment: 智谱 GLM first, then DashScope."""
    bigmodel = os.environ.get("BIGMODEL_API_KEY") or os.environ.get("ZHIPUAI_API_KEY") or ""
    if bigmodel:
        model = os.environ.get("BIGMODEL_MODEL") or "glm-4.7"
        base = (os.environ.get("BIGMODEL_BASE_URL") or "https://open.bigmodel.cn/api/paas/v4").rstrip("/")
        return {
            "provider": "bigmodel",
            "label": f"智谱 {model}",
            "model": model,
            "key": bigmodel,
            "url": base + "/chat/completions",
        }
    dashscope = os.environ.get("DASHSCOPE_API_KEY") or os.environ.get("MODELSCOPE_API_KEY") or ""
    if dashscope:
        model = os.environ.get("DASHSCOPE_MODEL") or "qwen-plus"
        base = (
            os.environ.get("DASHSCOPE_BASE_URL")
            or "https://dashscope.aliyuncs.com/compatible-mode/v1"
        ).rstrip("/")
        return {
            "provider": "dashscope",
            "label": f"DashScope {model}",
            "model": model,
            "key": dashscope,
            "url": base + "/chat/completions",
        }
    return None


_rate_lock = threading.Lock()
_last_call = 0.0


def _throttle() -> None:
    """Space out LLM requests so a free-tier QPS cap is not tripped."""
    global _last_call
    with _rate_lock:
        gap = LLM_MIN_INTERVAL - (time.monotonic() - _last_call)
        if gap > 0:
            time.sleep(gap)
        _last_call = time.monotonic()


def summarize(item: dict, llm: dict, thinking: bool = False) -> dict | None:
    """thinking=True 让模型先想再写：质量略好，但 token 约 3.3x、耗时约 5-6x。"""
    payload = {
        "model": llm["model"],
        "messages": [
            {"role": "system", "content": "你输出严格合法的 JSON，不含任何多余字符。"},
            {"role": "user", "content": build_prompt(item)},
        ],
        "temperature": 0.25,
        # 开思考时思维链会占掉大部分预算，必须给足 max_tokens，否则正文会被截断。
        "max_tokens": 3000 if thinking else 900,
    }
    if llm["provider"] == "bigmodel" and not thinking:
        payload["thinking"] = {"type": "disabled"}
    resp = None
    for attempt in range(1, LLM_ATTEMPTS + 1):
        _throttle()
        try:
            resp = http_post_json(
                llm["url"],
                payload,
                timeout=120,
                retries=1,
                headers={"Authorization": f"Bearer {llm['key']}"},
            )
            break
        except Exception as exc:  # noqa: BLE001
            text = str(exc)
            throttled = "429" in text or "1302" in text
            delay = (10.0 if throttled else 4.0) * attempt
            if attempt >= LLM_ATTEMPTS:
                log("papers", f"summary failed for {item['id']}: {text[:160]}")
                return None
            if throttled:
                log("papers", f"rate limited by {llm['label']}, backing off {delay:.0f}s")
            time.sleep(delay)

    if resp is None:
        return None
    try:
        content = resp["choices"][0]["message"].get("content") or ""
    except (KeyError, IndexError, TypeError, AttributeError):
        log("papers", f"unexpected LLM response for {item['id']}: {str(resp)[:200]}")
        return None
    data = _extract_json(content)
    if not data:
        log("papers", f"unparseable JSON for {item['id']}: {content[:120]}")
        return None
    title_zh = collapse(data.get("title_zh") or "") or None
    bullets = data.get("summary_zh") or []
    if isinstance(bullets, str):
        bullets = [bullets]
    bullets = [collapse(b) for b in bullets if collapse(b)][:4]
    tags = data.get("tags") or []
    if isinstance(tags, str):
        tags = [tags]
    tags = [collapse(t) for t in tags if collapse(t)][:5]
    if not title_zh and not bullets:
        return None
    return {"title_zh": title_zh, "summary_zh": bullets or None, "tags": tags}


def enrich(items: list[dict]) -> str | None:
    """Attach cached summaries, then generate the missing ones. Returns the model label."""
    cache_path = CACHE_DIR / "summaries.json"
    cache: dict = read_json(cache_path, {})

    # apply whatever we already have cached
    for item in items:
        cached = cache.get(item["id"])
        if cached:
            item["title_zh"] = cached.get("title_zh") or item.get("title_zh")
            item["summary_zh"] = cached.get("summary_zh") or item.get("summary_zh")
            item["tags"] = cached.get("tags") or item.get("tags") or []

    llm = resolve_llm()
    if not llm:
        missing = [i for i in items if not i.get("summary_zh")]
        log("papers", f"no LLM key (BIGMODEL_API_KEY / DASHSCOPE_API_KEY) - {len(missing)} papers stay English-only")
        return None

    # items are already sorted newest-first, so the freshest papers get done first
    todo = [i for i in items if not i.get("summary_zh")][:SUMMARIZE_LIMIT]
    if not todo:
        log("papers", f"all {len(items)} papers already have cached summaries ({llm['label']})")
        return llm["model"]
    log("papers", f"generating AI summaries for {len(todo)} papers with {llm['label']}")

    # 默认关思考；LLM_THINKING=on 全开，=auto 只给最相关的 THINK_TOP_N 篇开。
    think_ids: set[str] = set()
    if llm["provider"] == "bigmodel" and THINKING_MODE in ("on", "auto"):
        if THINKING_MODE == "on":
            think_ids = {i["id"] for i in todo}
        else:
            ranked = sorted(todo, key=lambda i: (i.get("score") or 0, i.get("published") or ""), reverse=True)
            think_ids = {i["id"] for i in ranked[:THINK_TOP_N]}
        log("papers", f"thinking enabled for {len(think_ids)}/{len(todo)} papers (mode={THINKING_MODE})")

    done = 0
    with ThreadPoolExecutor(max_workers=max(1, LLM_WORKERS)) as pool:
        futures = {
            pool.submit(summarize, item, llm, item["id"] in think_ids): item for item in todo
        }
        for future in as_completed(futures):
            item = futures[future]
            try:
                result = future.result()
            except Exception as exc:  # noqa: BLE001
                log("papers", f"summary error {item['id']}: {exc}")
                continue
            if not result:
                continue
            item.update(result)
            cache[item["id"]] = {**result, "model": llm["model"], "at": iso(now_utc())}
            done += 1
    log("papers", f"summarised {done}/{len(todo)} papers")

    # prune cache to the ids we still track
    live_ids = {item["id"] for item in items}
    pruned = {k: v for k, v in cache.items() if k in live_ids}
    write_json(cache_path, pruned)
    return llm["model"]


# ----------------------------------------------------------------------- main


def main() -> None:
    load_local_env()
    branches = load_keyword_branches()
    query = build_query(branches)
    log("papers", f"{len(branches)} keyword branches: {', '.join(branches[:8])}")

    fresh = fetch_arxiv(query)
    for item in fresh:
        score_item(item, branches)
    fresh.sort(key=lambda i: (i["score"], i["published"]), reverse=True)

    previous = read_json(DATA_DIR / "papers.json", [])
    if not isinstance(previous, list):
        previous = []
    cutoff = now_utc() - timedelta(days=HISTORY_DAYS)
    merged: dict[str, dict] = {}
    for item in previous:
        if not isinstance(item, dict) or not item.get("id"):
            continue
        published = parse_dt(item.get("published"))
        if published and published >= cutoff:
            merged[item["id"]] = item
    for item in fresh:
        merged[item["id"]] = item

    items = list(merged.values())
    items.sort(key=lambda i: i.get("published", ""), reverse=True)
    items = items[:600]

    ai_model = enrich(items)

    write_json(DATA_DIR / "papers.json", items)

    meta = read_json(DATA_DIR / "meta.json", {})
    meta["papers"] = {
        "generated_at": iso(now_utc()),
        "count": len(items),
        "new_today": len([i for i in items if i.get("day") == cn_day(now_utc())]),
        "keywords": branches,
        "ai_summary": bool(ai_model),
        "ai_model": ai_model,
        "ai_count": len([i for i in items if i.get("summary_zh")]),
    }
    write_json(DATA_DIR / "meta.json", meta)
    log("papers", f"done - {len(items)} papers in data/papers.json")


if __name__ == "__main__":
    main()
