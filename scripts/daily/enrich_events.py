#!/usr/bin/env python3
"""Turn the filtered news pool into structured events.

Only one LLM job is left, and it is deliberately small:

  1. 标题翻译：把没有中文的标题批量翻成中文（25 条/次）。摘要、政策阶段、
     主题词、实体全部由确定性规则产生，LLM 不再写摘要和判断。
  2. 事件聚类：把同一件事的多家报道并成一个事件，算出 first_seen_at
     （本系统最早看到的时刻）和 hot_seen_at（首次进入热榜的时刻）。

Why: 精炼（50 字摘要 + 实体 + 影响）在免费模型上调用量太大，而它的产出
大部分可以用原文摘要和关键词命中替代；260 字以内的原文摘要已经够读。

All model output is cached by item id in data/cache/news_llm.json, so a link is
only ever translated once. Without an API key the script still runs: events are
built from raw titles and marked `needs_translation`.

    python scripts/daily/enrich_events.py
    python scripts/daily/enrich_events.py --limit 5 --dry-run
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from common import (  # noqa: E402
    CACHE_DIR,
    DATA_DIR,
    cn_day,
    collapse,
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

NEWS_FILE = DATA_DIR / "news.json"
EVENTS_FILE = DATA_DIR / "events.json"
STATS_FILE = DATA_DIR / "stats.json"
CACHE_FILE = CACHE_DIR / "news_llm.json"

TRANSLATE_LIMIT = int(os.environ.get("NEWS_TRANSLATE_LIMIT", "80"))
TRANSLATE_BATCH = int(os.environ.get("NEWS_TRANSLATE_BATCH", "25"))
EVENT_DAYS = int(os.environ.get("NEWS_EVENT_DAYS", "4"))
SIMILARITY = float(os.environ.get("NEWS_EVENT_SIMILARITY", "0.5"))
MAX_EVENTS = int(os.environ.get("NEWS_EVENT_LIMIT", "260"))
LLM_BUDGET_MIN = float(os.environ.get("NEWS_LLM_BUDGET_MIN", "6"))
SUMMARY_CHARS = int(os.environ.get("NEWS_SUMMARY_CHARS", "260"))

STAGES = {"征求意见", "已发布", "解读", "传闻", "不适用"}
LEVEL_RANK = {"L1": 0, "L2": 1, "L3": 2}


# ----------------------------------------------------------------------- LLM


def resolve_llm() -> dict | None:
    bigmodel = os.environ.get("BIGMODEL_API_KEY") or os.environ.get("ZHIPUAI_API_KEY") or ""
    if bigmodel:
        # 新闻链路固定用 flash（免费）；论文链路仍然用 BIGMODEL_MODEL 指定的模型
        model = os.environ.get("NEWS_LLM_MODEL") or "glm-4.7-flash"
        base = (os.environ.get("BIGMODEL_BASE_URL") or "https://open.bigmodel.cn/api/paas/v4").rstrip("/")
        return {"label": f"智谱 {model}", "model": model, "key": bigmodel,
                "url": base + "/chat/completions", "provider": "bigmodel"}
    dashscope = os.environ.get("DASHSCOPE_API_KEY") or os.environ.get("MODELSCOPE_API_KEY") or ""
    if dashscope:
        model = os.environ.get("NEWS_LLM_MODEL") or os.environ.get("DASHSCOPE_MODEL") or "qwen-flash"
        base = (os.environ.get("DASHSCOPE_BASE_URL")
                or "https://dashscope.aliyuncs.com/compatible-mode/v1").rstrip("/")
        return {"label": f"DashScope {model}", "model": model, "key": dashscope,
                "url": base + "/chat/completions", "provider": "dashscope"}
    return None


SYSTEM_PROMPT = "你是严谨的技术新闻译者。只输出严格合法的 JSON，不要 markdown 代码块，不要解释。"

TRANSLATE_TEMPLATE = """把下面 {count} 条英文新闻标题翻译成简洁、准确的中文标题。要求：
1. 保留 OpenAI、Anthropic、Claude、GPT-5、Llama 等专有名词和技术缩写；
2. 只翻译，不添加原文没有的信息，不写评论，不加书名号；
3. 每条都要返回，id 原样带回；
4. 只输出 JSON：{{"items":[{{"id":"...","title_zh":"..."}}]}}

标题：
{payload}
"""


def _extract_json(text: str) -> dict | None:
    if not text:
        return None
    raw = text.strip()
    raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw, flags=re.MULTILINE).strip()
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        pass
    match = re.search(r"\{.*\}", raw, flags=re.DOTALL)
    if match:
        try:
            return json.loads(match.group(0))
        except json.JSONDecodeError:
            return None
    return None


def has_cjk(text: str | None) -> bool:
    return bool(re.search(r"[\u4e00-\u9fff]", text or ""))


def build_payload(batch: list[dict]) -> str:
    rows = [
        {"id": item["id"], "title": truncate(item.get("title", ""), 200)}
        for item in batch
    ]
    return json.dumps(rows, ensure_ascii=False)


def call_llm(llm: dict, batch: list[dict], drop_response_format: bool = False) -> dict[str, str]:
    payload = {
        "model": llm["model"],
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": TRANSLATE_TEMPLATE.format(count=len(batch), payload=build_payload(batch))},
        ],
        "temperature": 0.2,
        "max_tokens": 2000,
    }
    if not drop_response_format:
        payload["response_format"] = {"type": "json_object"}
    if llm.get("provider") == "bigmodel":
        # glm-4.7-flash 也可能走思考链；不关掉的话思维链会吃光 max_tokens，正文为空
        payload["thinking"] = {"type": "disabled"}

    response = None
    for attempt in range(1, 5):
        try:
            response = http_post_json(llm["url"], payload, timeout=120, retries=1,
                                      headers={"Authorization": f"Bearer {llm['key']}"})
            break
        except Exception as exc:  # noqa: BLE001
            message = str(exc)
            if "429" in message or "1305" in message:
                delay = 8 * attempt
                log("llm", f"rate limited, retry in {delay}s ({attempt}/4)")
                time.sleep(delay)
                continue
            if ("response_format" in message or "400" in message) and not drop_response_format:
                return call_llm(llm, batch, drop_response_format=True)
            log("llm", f"call failed: {message[:160]}")
            return {}
    if response is None:
        log("llm", "giving up after retries")
        return {}

    try:
        content = response["choices"][0]["message"].get("content") or ""
    except (KeyError, IndexError, TypeError, AttributeError):
        log("llm", f"unexpected response: {str(response)[:160]}")
        return {}
    data = _extract_json(content)
    if not data:
        log("llm", f"unparseable JSON: {content[:120]}")
        return {}

    rows = data.get("items") if isinstance(data, dict) else None
    if not isinstance(rows, list):
        return {}
    out: dict[str, str] = {}
    for row in rows:
        if not isinstance(row, dict) or not row.get("id"):
            continue
        title = collapse(str(row.get("title_zh") or ""))
        if title:
            out[str(row["id"])] = title
    return out


def translate_titles(items: list[dict], llm: dict | None) -> tuple[dict[str, dict], int]:
    """Translate only the titles that have no Chinese, in batches. Everything
    else (summary, policy stage, topics) is produced by deterministic rules."""
    cache: dict = read_json(CACHE_FILE, {})
    cutoff = now_utc() - timedelta(days=EVENT_DAYS)
    todo = [
        item for item in items
        if item.get("id")
        and not has_cjk(item.get("title"))
        and not (cache.get(item["id"]) or {}).get("title_zh")
        and (parse_dt(item.get("published")) or now_utc()) >= cutoff
    ][:TRANSLATE_LIMIT]
    if not llm or not todo:
        return cache, 0

    batches = [todo[i:i + TRANSLATE_BATCH] for i in range(0, len(todo), TRANSLATE_BATCH)]
    log("llm", f"translating {len(todo)} titles in {len(batches)} batches with {llm['label']}")
    done = 0
    started = time.monotonic()
    budget = LLM_BUDGET_MIN * 60
    for batch in batches:
        if time.monotonic() - started > budget:
            log("llm", f"time budget {LLM_BUDGET_MIN:.0f}min reached - the rest runs next time")
            break
        result = call_llm(llm, batch)
        for item in batch:
            title = result.get(item["id"])
            if not title:
                continue
            cache[item["id"]] = {"title_zh": title, "model": llm["model"], "at": iso(now_utc())}
            done += 1
        time.sleep(0.5)
    write_json(CACHE_FILE, cache)
    log("llm", f"translated {done}/{len(todo)} titles")
    return cache, done


# ------------------------------------------------------------------ clustering


def normalize_for_similarity(text: str) -> str:
    return re.sub(r"[^0-9a-z\u4e00-\u9fff]+", "", (text or "").lower())


# 公文标题里的套话，不参与相似度计算，否则「关于…的通知」会互相命中
BOILERPLATE = re.compile(
    r"(关于|印发|公布|的通知|的公告|的决定|公告|通知|决定|实施方案|征求意见|"
    r"公开征求|年第\d+号|第\d+号|等\d+项)"
)


def core_title(text: str) -> str:
    return BOILERPLATE.sub("", text or "")


def bigrams(text: str) -> set[str]:
    clean = normalize_for_similarity(core_title(text))
    if len(clean) < 2:
        return {clean} if clean else set()
    return {clean[i:i + 2] for i in range(len(clean) - 1)}


STOPWORDS = {
    "the", "a", "an", "of", "to", "and", "for", "with", "on", "in", "is", "are",
    "as", "its", "new", "says", "said", "how", "why", "what", "after", "over",
    "into", "from", "by", "at", "be", "will", "can", "has", "have", "this",
    "that", "it", "you", "your", "we", "our", "not", "but", "or", "more",
}


def word_tokens(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9]{3,}", (text or "").lower()) if w not in STOPWORDS}


def similarity(a: str, b: str) -> float:
    left, right = bigrams(a), bigrams(b)
    bigram_sim = len(left & right) / (len(left) + len(right) - len(left & right)) if left and right else 0.0
    words_a, words_b = word_tokens(a), word_tokens(b)
    # 词级 Jaccard 只对「两边都有足够英文词」的标题可信；中文标题里往往只剩
    # 一个专有名词（Anthropic），会把不相干的文章算成 1.0
    if len(words_a) >= 3 and len(words_b) >= 3:
        word_sim = len(words_a & words_b) / len(words_a | words_b)
    else:
        word_sim = 0.0
    return max(bigram_sim, word_sim)


DIGEST_TITLE = re.compile(r"(早报|晚报|日报|周报|简报|速递|要闻|今日看点)")


def is_digest(title: str) -> bool:
    return bool(DIGEST_TITLE.search(title or ""))


def guess_stage(item: dict) -> str:
    """政策阶段只由规则判断：只有 L1 部委公告才有意义。"""
    if item.get("level") != "L1":
        return "不适用"
    title = item.get("title", "")
    if "征求意见" in title or "公开征求" in title:
        stage = "征求意见"
    elif any(word in title for word in ("公告", "决定", "通知", "办法", "规定", "规划", "意见")):
        stage = "已发布"
    else:
        stage = "不适用"
    return stage if stage in STAGES else "不适用"


def topics_of(item: dict) -> list[str]:
    """主题词直接复用关键词命中的规则标签，零 LLM。"""
    labels: list[str] = []
    for raw in item.get("matched") or []:
        label = collapse(str(raw))
        if not label or label == "*" or label.startswith("/") or len(label) > 12:
            continue
        if label not in labels:
            labels.append(label)
        if len(labels) >= 3:
            break
    return labels


def build_events(items: list[dict], cache: dict) -> list[dict]:
    cutoff = now_utc() - timedelta(days=EVENT_DAYS)
    pool = []
    for item in items:
        published = parse_dt(item.get("published")) or now_utc()
        if published < cutoff:
            continue
        pool.append((published, item, cache.get(item.get("id"), {})))
    pool.sort(key=lambda row: row[0], reverse=True)

    clusters: list[dict] = []
    for published, item, data in pool:
        title = data.get("title_zh") or item.get("title", "")
        target = None
        for cluster in clusters:
            if item.get("source") in cluster["sources"]:
                continue  # 同一家媒体的两条各自成事件，避免系列报道被合并
            if is_digest(title) or any(is_digest(t) for t in cluster["titles"]):
                continue  # 早报/周报是合集，不和单条事件合并
            best = max(
                (similarity(title, member_title) for member_title in cluster["titles"]),
                default=0.0,
            )
            # 只有标题足够相似才合并；宁可不合并，也不要把两条不相干的新闻并成一条。
            if best >= SIMILARITY:
                target = cluster
                break
        if target is None:
            clusters.append({
                "titles": [title],
                "items": [(published, item, data)],
                "sources": {item.get("source")},
            })
        else:
            target["titles"].append(title)
            target["items"].append((published, item, data))
            target["sources"].add(item.get("source"))

    events = []
    for cluster in clusters:
        members = cluster["items"]
        # 代表条目：官方优先，其次摘要最完整
        rep = min(
            members,
            key=lambda row: (
                LEVEL_RANK.get(row[1].get("level", "L3"), 9),
                -len(row[1].get("summary") or ""),
                -row[0].timestamp(),
            ),
        )
        _, rep_item, rep_data = rep
        links = []
        seen_urls = set()
        for _, item, data in sorted(members, key=lambda row: LEVEL_RANK.get(row[1].get("level", "L3"), 9)):
            url = item.get("link")
            if not url or url in seen_urls:
                continue
            seen_urls.add(url)
            links.append({
                "source": item.get("source"),
                "level": item.get("level", "L2"),
                "title": data.get("title_zh") or item.get("title", ""),
                "url": url,
                "published": item.get("published"),
            })
        first_seen = min((item.get("first_seen") or item.get("published") or iso(now_utc()))
                         for _, item, _ in members)
        hot_seen = min((item.get("published") for _, item, _ in members
                        if item.get("level") == "L3" and item.get("published")), default=None)
        published = min(row[0] for row in members)

        events.append({
            "id": "e_" + hashlib.sha1(
                (rep_item.get("link") or rep_item.get("id") or "").encode("utf-8")
            ).hexdigest()[:10],
            "title_zh": rep_data.get("title_zh") or rep_item.get("title"),
            "event_summary": truncate(rep_item.get("summary") or "", SUMMARY_CHARS),
            "source_level": rep_item.get("level", "L2"),
            "policy_stage": guess_stage(rep_item),
            "key_entities": [],
            "potential_impact": None,
            "topics": topics_of(rep_item),
            "first_seen_at": first_seen,
            "hot_seen_at": hot_seen,
            "published": iso(published),
            "primary_url": rep_item.get("link"),
            "source_count": len(links),
            "links": links,
            "needs_translation": bool(
                not rep_data.get("title_zh") and not has_cjk(rep_item.get("title"))
            ),
        })

    events.sort(key=lambda e: (e.get("first_seen_at") or ""), reverse=True)
    return events[:MAX_EVENTS]


# ----------------------------------------------------------------------- main


def main() -> None:
    parser = argparse.ArgumentParser(description="Build events from the news pool")
    parser.add_argument("--limit", type=int, default=0, help="override NEWS_TRANSLATE_LIMIT")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--no-llm", action="store_true", help="use the cache only, no API calls")
    args = parser.parse_args()

    load_local_env()
    global TRANSLATE_LIMIT
    if args.limit:
        TRANSLATE_LIMIT = args.limit

    news = read_json(NEWS_FILE, [])
    if isinstance(news, dict):
        news = news.get("items", [])
    if not isinstance(news, list) or not news:
        log("events", "news.json is empty - run fetch_sources.py first")
        return
    log("events", f"{len(news)} news items in the pool")

    llm = None if args.no_llm else resolve_llm()
    if not llm:
        log("events", "no LLM key (or --no-llm) - clustering cached / raw titles only")
    cache, translated = translate_titles(news, llm)
    events = build_events(news, cache)
    log("events", f"{len(news)} items -> {len(events)} events"
                  + (f" ({translated} titles translated)" if translated else ""))

    today = cn_day(now_utc())
    stats = read_json(STATS_FILE, {})
    if not isinstance(stats, dict):
        stats = {}
    stats["events"] = {
        "run_at": iso(now_utc()),
        "items": len(news),
        "events": len(events),
        "translated": translated,
        "new_today": len([e for e in events if cn_day(parse_dt(e.get("first_seen_at")) or now_utc()) == today]),
        "model": llm["model"] if llm else None,
    }

    if args.dry_run:
        print(json.dumps({"stages": stats["events"], "sample": events[:3]}, ensure_ascii=False, indent=2))
        return

    write_json(EVENTS_FILE, events)
    write_json(STATS_FILE, stats)

    meta = read_json(DATA_DIR / "meta.json", {})
    meta["events"] = {
        "generated_at": stats["events"]["run_at"],
        "count": len(events),
        "new_today": stats["events"]["new_today"],
        "model": stats["events"]["model"],
        "l1": len([e for e in events if e.get("source_level") == "L1"]),
        "policy": len([e for e in events if e.get("policy_stage") in ("征求意见", "已发布")]),
    }
    write_json(DATA_DIR / "meta.json", meta)
    log("events", f"wrote {len(events)} events, "
                  f"{meta['events']['l1']} L1 / {meta['events']['policy']} 政策阶段")


if __name__ == "__main__":
    main()
