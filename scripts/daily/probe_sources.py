#!/usr/bin/env python3
"""Probe the candidate news sources and print a reachability / volume table.

    python scripts/daily/probe_sources.py                 # probe everything
    python scripts/daily/probe_sources.py --only zhihu,cls
    python scripts/daily/probe_sources.py --out daily/docs/source-probe.md

Why this exists: half of the Chinese sources cannot be checked from a dev box
(geo / anti-bot), and the other half cannot be trusted until they run from the
GitHub Actions runner. This script makes the answer a table instead of a guess.

Standard library only, same as the rest of scripts/daily/.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from common import ROOT, http_get, log  # noqa: E402

# ---------------------------------------------------------------------------
# Candidates: everything the v2 plan considers. Kept here (not in sources.json)
# because this is a diagnostic run, not the runtime source list.
# ---------------------------------------------------------------------------
CANDIDATES: list[dict] = [
    # ---------------- L1 政策源头 ----------------
    {"id": "gov-zuixin", "name": "国务院·最新政策", "level": "L1", "type": "html",
     "url": "https://www.gov.cn/zhengce/zuixin/"},
    {"id": "gov-zuixin-rsshub", "name": "国务院·最新政策(RSSHub)", "level": "L1", "type": "rss",
     "url": "https://rsshub.app/gov/zhengce/zuixin"},
    {"id": "mofcom-home", "name": "商务部", "level": "L1", "type": "html",
     "url": "https://www.mofcom.gov.cn/"},
    {"id": "most-tztg", "name": "科技部·通知公告", "level": "L1", "type": "html",
     "url": "https://www.most.gov.cn/tztg/"},
    {"id": "ndrc-zcfb", "name": "发改委·政策发布", "level": "L1", "type": "html",
     "url": "https://www.ndrc.gov.cn/xxgk/zcfb/"},
    {"id": "miit-zcwj", "name": "工信部·政策文件", "level": "L1", "type": "html",
     "url": "https://www.miit.gov.cn/zwgk/zcwj/index.html"},
    {"id": "cac-xxgk", "name": "网信办·信息公开", "level": "L1", "type": "html",
     "url": "https://www.cac.gov.cn/xxgk/index.htm"},

    # ---------------- L2 中文专业媒体 ----------------
    {"id": "qbitai", "name": "量子位", "level": "L2", "type": "rss",
     "url": "https://www.qbitai.com/feed"},
    {"id": "jiqizhixin", "name": "机器之心", "level": "L2", "type": "rss",
     "url": "https://www.jiqizhixin.com/rss"},
    {"id": "36kr-feed", "name": "36氪", "level": "L2", "type": "rss",
     "url": "https://36kr.com/feed"},
    {"id": "36kr-flash", "name": "36氪·快讯", "level": "L2", "type": "html",
     "url": "https://www.36kr.com/newsflashes"},
    {"id": "ithome-rss", "name": "IT之家", "level": "L2", "type": "rss",
     "url": "https://www.ithome.com/rss/"},
    {"id": "ifanr", "name": "爱范儿", "level": "L2", "type": "rss",
     "url": "https://www.ifanr.com/feed"},
    {"id": "sspai", "name": "少数派", "level": "L2", "type": "rss",
     "url": "https://sspai.com/feed"},
    {"id": "huxiu", "name": "虎嗅", "level": "L2", "type": "rss",
     "url": "https://www.huxiu.com/rss/0.xml"},
    {"id": "tmtpost", "name": "钛媒体", "level": "L2", "type": "rss",
     "url": "https://www.tmtpost.com/rss.xml"},
    {"id": "cnbeta", "name": "cnBeta", "level": "L2", "type": "rss",
     "url": "https://www.cnbeta.com.tw/backend.php"},
    {"id": "infoq-cn", "name": "InfoQ 中文", "level": "L2", "type": "rss",
     "url": "https://www.infoq.cn/feed"},
    {"id": "sina-tech", "name": "新浪科技", "level": "L2", "type": "rss",
     "url": "https://rss.sina.com.cn/tech/rollnews.xml"},
    {"id": "leiphone", "name": "雷峰网", "level": "L2", "type": "rss",
     "url": "https://www.leiphone.com/feed"},
    {"id": "geekpark", "name": "极客公园", "level": "L2", "type": "rss",
     "url": "https://www.geekpark.net/rss"},
    {"id": "solidot", "name": "Solidot", "level": "L2", "type": "rss",
     "url": "https://www.solidot.org/index.rss"},
    {"id": "ifeng-tech", "name": "凤凰科技", "level": "L2", "type": "rss",
     "url": "https://tech.ifeng.com/rss/index.xml"},
    {"id": "thepaper-hot", "name": "澎湃·热新闻", "level": "L2", "type": "json",
     "url": "https://cache.thepaper.cn/contentapi/wwwIndex/rightSidebar"},
    {"id": "wallstreetcn-live", "name": "华尔街见闻·快讯", "level": "L2", "type": "json",
     "url": "https://api-one.wallstcn.com/apiv1/content/lives?channel=global-channel&limit=30"},
    {"id": "cls-telegraph", "name": "财联社·电报", "level": "L2", "type": "json",
     "url": "https://www.cls.cn/nodeapi/telegraphList?app=CailianpressWeb&os=web&sv=8.4.6"},

    # ---------------- L3 社区热榜 ----------------
    {"id": "zhihu", "name": "知乎热榜", "level": "L3", "type": "json",
     "url": "https://api.zhihu.com/topstory/hot-lists/total?limit=50"},
    {"id": "weibo-json", "name": "微博热搜(JSON)", "level": "L3", "type": "json",
     "url": "https://weibo.com/ajax/side/hotSearch"},
    {"id": "weibo-html", "name": "微博热搜(HTML)", "level": "L3", "type": "html",
     "url": "https://s.weibo.com/top/summary?cate=realtimehot"},
    {"id": "baidu", "name": "百度热搜", "level": "L3", "type": "html",
     "url": "https://top.baidu.com/board?tab=realtime"},
    {"id": "bilibili", "name": "B站排行", "level": "L3", "type": "json",
     "url": "https://api.bilibili.com/x/web-interface/ranking/v2?rid=0&type=all"},
    {"id": "toutiao", "name": "今日头条热榜", "level": "L3", "type": "json",
     "url": "https://www.toutiao.com/hot-event/hot-board/?origin=toutiao_pc"},
    {"id": "juejin", "name": "掘金热榜", "level": "L3", "type": "json",
     "url": "https://api.juejin.cn/content_api/v1/content/article_rank?category_id=1&type=hot"},
    {"id": "v2ex", "name": "V2EX", "level": "L3", "type": "rss",
     "url": "https://www.v2ex.com/index.xml"},
    {"id": "github-trending", "name": "GitHub Trending", "level": "L3", "type": "html",
     "url": "https://github.com/trending?since=daily"},
    {"id": "hn", "name": "Hacker News", "level": "L3", "type": "rss",
     "url": "https://hnrss.org/frontpage?points=100"},

    # ---------------- 聚合层 ----------------
    {"id": "newsnow-busiyi", "name": "NewsNow 公共实例", "level": "L3", "type": "json",
     "url": "https://newsnow.busiyi.world/api/s?id=zhihu"},
    {"id": "newsnow-cc", "name": "NewsNow 官方演示", "level": "L3", "type": "json",
     "url": "https://newsnow.cc/api/s?id=zhihu"},
    {"id": "rsshub-app", "name": "RSSHub 官方实例", "level": "L3", "type": "rss",
     "url": "https://rsshub.app/zhihu/hot"},
    {"id": "rsshub-rssforever", "name": "RSSHub 镜像 rssforever", "level": "L3", "type": "rss",
     "url": "https://rsshub.rssforever.com/zhihu/hot"},
    {"id": "rsshub-slarker", "name": "RSSHub 镜像 slarker", "level": "L3", "type": "rss",
     "url": "https://hub.slarker.me/zhihu/hot"},
    {"id": "rsshub-pseudoyu", "name": "RSSHub 镜像 pseudoyu", "level": "L3", "type": "rss",
     "url": "https://rsshub.pseudoyu.com/zhihu/hot"},
    {"id": "rsshub-feeded", "name": "RSSHub 镜像 feeded", "level": "L3", "type": "rss",
     "url": "https://rsshub.feeded.xyz/zhihu/hot"},
    {"id": "vvhan-hotlist", "name": "vvhan 热榜聚合 API", "level": "L3", "type": "json",
     "url": "https://api.vvhan.com/api/hotlist/all"},
    {"id": "tenapi-weibo", "name": "tenapi 微博热搜", "level": "L3", "type": "json",
     "url": "https://tenapi.cn/v2/weibohot"},

    # ---------------- 国外 ----------------
    {"id": "techcrunch-ai", "name": "TechCrunch AI", "level": "L2", "type": "rss",
     "url": "https://techcrunch.com/category/artificial-intelligence/feed/"},
    {"id": "ars", "name": "Ars Technica", "level": "L2", "type": "rss",
     "url": "https://feeds.arstechnica.com/arstechnica/index"},
    {"id": "mittr", "name": "MIT Technology Review", "level": "L2", "type": "rss",
     "url": "https://www.technologyreview.com/feed/"},
    {"id": "verge-ai", "name": "The Verge AI", "level": "L2", "type": "rss",
     "url": "https://www.theverge.com/rss/ai-artificial-intelligence/index.xml"},
    {"id": "venturebeat-ai", "name": "VentureBeat AI", "level": "L2", "type": "rss",
     "url": "https://venturebeat.com/category/ai/feed/"},
    {"id": "openai", "name": "OpenAI News", "level": "L2", "type": "rss",
     "url": "https://openai.com/news/rss.xml"},
    {"id": "deepmind", "name": "Google DeepMind", "level": "L2", "type": "rss",
     "url": "https://deepmind.google/blog/rss.xml"},
    {"id": "google-ai", "name": "Google AI Blog", "level": "L2", "type": "rss",
     "url": "https://blog.google/technology/ai/rss/"},
    {"id": "simonw", "name": "Simon Willison", "level": "L3", "type": "rss",
     "url": "https://simonwillison.net/atom/everything/"},
    {"id": "lilianweng", "name": "Lilian Weng", "level": "L3", "type": "rss",
     "url": "https://lilianweng.github.io/index.xml"},
    {"id": "huggingface", "name": "Hugging Face Blog", "level": "L2", "type": "rss",
     "url": "https://huggingface.co/blog/feed.xml"},
    {"id": "anthropic", "name": "Anthropic News", "level": "L2", "type": "rss",
     "url": "https://www.anthropic.com/rss.xml"},
    {"id": "importai", "name": "Import AI", "level": "L3", "type": "rss",
     "url": "https://importai.substack.com/feed"},
    {"id": "producthunt", "name": "Product Hunt", "level": "L3", "type": "rss",
     "url": "https://www.producthunt.com/feed"},
    {"id": "github-blog", "name": "GitHub Blog", "level": "L2", "type": "rss",
     "url": "https://github.blog/feed/"},
    {"id": "bair", "name": "BAIR Blog", "level": "L3", "type": "rss",
     "url": "https://bair.berkeley.edu/blog/feed.xml"},
    {"id": "mit-news-ai", "name": "MIT News AI", "level": "L2", "type": "rss",
     "url": "https://news.mit.edu/rss/topic/artificial-intelligence2"},
]

TITLE_RE = re.compile(r"(?is)<(?:title|name)>\s*(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?\s*</(?:title|name)>")


def count_rss(text: str) -> int:
    return len(re.findall(r"<(?:item|entry)\b", text, re.IGNORECASE))


def count_json(text: str) -> tuple[int, str]:
    """Best-effort item count + a sample title from an arbitrary JSON payload."""
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return 0, ""

    def walk(node, depth=0) -> tuple[int, str]:
        if depth > 6:
            return 0, ""
        if isinstance(node, list):
            if not node:
                return 0, ""
            sample = ""
            if isinstance(node[0], dict):
                for key in ("title", "name", "word", "query", "content"):
                    if isinstance(node[0].get(key), str):
                        sample = node[0][key]
                        break
            return len(node), sample
        if isinstance(node, dict):
            best = (0, "")
            for key, value in node.items():
                if key in ("data", "items", "list", "result", "results", "hotNews",
                           "day_items", "roll_data", "realtime", "trending"):
                    cand = walk(value, depth + 1)
                    if cand[0] > best[0]:
                        best = cand
                elif isinstance(value, (dict, list)) and best[0] == 0:
                    best = walk(value, depth + 1)
            return best
        return 0, ""

    return walk(data)


def count_html(text: str) -> tuple[int, str]:
    links = re.findall(r"(?is)<a\b[^>]*href=[\"'](https?://[^\"']+)[\"'][^>]*>(.*?)</a>", text)
    title_match = TITLE_RE.search(text)
    sample = ""
    for _, inner in links:
        clean = re.sub(r"<[^>]+>", "", inner).strip()
        if len(clean) >= 6:
            sample = clean
            break
    return len(links), sample or (title_match.group(1).strip() if title_match else "")


def probe(item: dict, timeout: float = 15.0) -> dict:
    started = time.monotonic()
    result = {
        "id": item["id"], "name": item["name"], "level": item["level"],
        "type": item["type"], "url": item["url"],
        "ok": False, "status": "-", "bytes": 0, "items": 0,
        "sample": "", "error": "", "ms": 0,
    }
    try:
        text = http_get(item["url"], timeout=timeout, retries=1, accept="*/*")
        result["ok"] = True
        result["status"] = "200"
        result["bytes"] = len(text.encode("utf-8", "ignore"))
        if item["type"] == "rss":
            result["items"] = count_rss(text)
            match = TITLE_RE.search(text)
            result["sample"] = re.sub(r"<[^>]+>", "", match.group(1)).strip() if match else ""
        elif item["type"] == "json":
            result["items"], result["sample"] = count_json(text)
        else:
            links, sample = count_html(text)
            result["items"] = links
            result["sample"] = sample
        if not result["items"]:
            result["error"] = "0 items (parsed but empty)"
    except Exception as exc:  # noqa: BLE001 - this is a diagnostic
        message = str(exc)
        match = re.search(r"HTTP(?: Error)? (\d{3})", message)
        result["status"] = match.group(1) if match else "ERR"
        result["error"] = message[:160]
    result["ms"] = int((time.monotonic() - started) * 1000)
    return result


def render(results: list[dict]) -> str:
    ok = sum(1 for r in results if r["ok"] and r["items"])
    lines = [
        "| 层级 | 源 | 类型 | 状态 | 字节 | 条目 | 耗时 | 样例/错误 |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for r in results:
        sample = (r["sample"] or r["error"]).replace("|", "\\|")
        sample = sample[:60]
        status = r["status"] if r["ok"] else f"❌ {r['status']}"
        lines.append(
            f"| {r['level']} | {r['name']} | {r['type']} | {status} | "
            f"{r['bytes']:,} | {r['items']} | {r['ms']}ms | {sample} |"
        )
    lines.append("")
    lines.append(f"**{ok}/{len(results)}** 个源可解析出条目。")
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description="Probe candidate news sources")
    parser.add_argument("--only", default="", help="comma separated ids")
    parser.add_argument("--out", default="", help="write the markdown table here")
    parser.add_argument("--workers", type=int, default=8)
    args = parser.parse_args()

    wanted = {s.strip() for s in args.only.split(",") if s.strip()}
    candidates = [c for c in CANDIDATES if not wanted or c["id"] in wanted]
    log("probe", f"probing {len(candidates)} sources with {args.workers} workers")

    results: list[dict] = []
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {pool.submit(probe, c): c for c in candidates}
        for future in as_completed(futures):
            result = future.result()
            results.append(result)
            flag = "ok " if result["ok"] and result["items"] else "FAIL"
            log("probe", f"{flag} {result['name']:<16} {result['items']:>4} items "
                         f"{result['ms']:>5}ms {result['error'][:60]}")

    order = {c["id"]: i for i, c in enumerate(CANDIDATES)}
    results.sort(key=lambda r: order.get(r["id"], 999))
    table = render(results)
    print("\n" + table)

    if args.out:
        out = ROOT / args.out
        out.parent.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y-%m-%d %H:%M:%S")
        out.write_text(
            f"# 源探测结果\n\n生成时间（本机）：{stamp}\n\n{table}\n",
            encoding="utf-8",
        )
        log("probe", f"wrote {out.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
