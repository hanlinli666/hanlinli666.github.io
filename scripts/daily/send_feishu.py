#!/usr/bin/env python3
"""Build the daily Feishu (Lark) message card from data/*.json and POST it to a
custom-bot webhook.

    python scripts/send_feishu.py            # send (needs FEISHU_WEBHOOK_URL)
    python scripts/send_feishu.py --dry-run  # print the card JSON, send nothing

Environment:
    FEISHU_WEBHOOK_URL   required  the custom bot webhook
    FEISHU_SIGNKEY       optional  only when 签名校验 is enabled on the bot
    PWA_URL              optional  defaults to the GitHub Pages URL of this repo
    GITHUB_REPOSITORY    optional  "owner/repo", used to derive PWA_URL in CI
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import json
import os
import sys
import time
import urllib.parse
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from common import (  # noqa: E402
    CN_TZ,
    CONFIG_DIR,
    DATA_DIR,
    cn_day,
    http_post_json,
    load_local_env,
    log,
    now_utc,
    parse_dt,
    read_json,
    truncate,
)

WEEK = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]
MAX_PAPERS = 5
MAX_NEWS = 5

# The PWA lives in a sub-folder of the Pages site (e.g. hanlinli666.github.io/daily/).
APP_SUBPATH = (os.environ.get("PWA_SUBPATH") or "daily").strip("/")


# --------------------------------------------------------------------- helpers


def resolve_pwa_url() -> str:
    """Absolute URL of the PWA, with a trailing slash."""
    explicit = (os.environ.get("PWA_URL") or "").strip()
    if explicit:
        return explicit if explicit.endswith("/") else explicit + "/"

    base = ""
    repo = (os.environ.get("GITHUB_REPOSITORY") or "").strip()
    if "/" in repo:
        owner, name = repo.split("/", 1)
        if name.lower() == f"{owner.lower()}.github.io":
            base = f"https://{owner}.github.io/"
        else:
            base = f"https://{owner}.github.io/{name}/"

    if not base:
        # Fall back to a checked-in config for local runs / custom domains.
        site = read_json(CONFIG_DIR / "site.json", {})
        if isinstance(site, dict) and site.get("url"):
            url = str(site["url"]).strip()
            return url if url.endswith("/") else url + "/"
        return ""

    if not base.endswith("/"):
        base += "/"
    return base + (APP_SUBPATH + "/" if APP_SUBPATH else "")


def sign_payload(secret: str) -> dict:
    """Feishu custom-bot signature: HMAC-SHA256 over "{ts}\\n{secret}" with an empty msg."""
    timestamp = str(int(time.time()))
    string_to_sign = f"{timestamp}\n{secret}"
    digest = hmac.new(
        string_to_sign.encode("utf-8"), b"", digestmod=hashlib.sha256
    ).digest()
    return {"timestamp": timestamp, "sign": base64.b64encode(digest).decode("utf-8")}


def human_day(day: datetime) -> str:
    return f"{day.month} 月 {day.day} 日 · {WEEK[day.weekday()]}"


def pwa_link(base: str, anchor: str = "", query: str = "") -> str:
    base = base or "https://github.com/"
    parts = urllib.parse.urlsplit(base)
    qs = parts.query
    if query:
        qs = (qs + "&" if qs else "") + "q=" + urllib.parse.quote(query)
    return urllib.parse.urlunsplit(
        (parts.scheme, parts.netloc, parts.path, qs, anchor or parts.fragment)
    )


def search_key(title: str, limit: int = 46) -> str:
    """A prefix of the title, without the ellipsis, so the PWA's search can match it."""
    clean = " ".join((title or "").split())
    return clean[:limit].strip()


def paper_line(index: int, paper: dict, base: str) -> str:
    title = paper.get("title_zh") or paper.get("title") or "未命名论文"
    link = pwa_link(base, "papers", search_key(title))
    return f"**{index}.** [{truncate(title, 62)}]({link})"


def news_line(index: int, item: dict) -> str:
    title = truncate(item.get("title") or "无标题", 70)
    source = item.get("source") or "新闻"
    return f"**{index}.** [{title}]({item.get('link', '#')}) · {source}"


# ----------------------------------------------------------------------- build


def pick_papers(papers: list[dict], today: str) -> tuple[list[dict], int, str]:
    """Return (top papers, count of the reference day, human label for the day)."""
    todays = [p for p in papers if (p.get("day") or cn_day(parse_dt(p.get("published")) or now_utc())) == today]
    if todays:
        ranked = sorted(todays, key=lambda p: (p.get("score") or 0, p.get("published") or ""), reverse=True)
        return ranked[:MAX_PAPERS], len(todays), "今日"

    if not papers:
        return [], 0, "今日"

    best_day = max((p.get("day") or "") for p in papers)
    batch = [p for p in papers if (p.get("day") or "") == best_day]
    ranked = sorted(batch, key=lambda p: (p.get("score") or 0, p.get("published") or ""), reverse=True)
    return ranked[:MAX_PAPERS], len(batch), f"最近（{best_day[5:].replace('-', '/')}）"


def pick_news(news: list[dict], today: str) -> tuple[list[dict], int, str]:
    todays = [n for n in news if (n.get("day") or "") == today]
    if len(todays) >= 3:
        ranked = sorted(todays, key=lambda n: n.get("published") or "", reverse=True)[:MAX_NEWS]
        return ranked, len(todays), "今日"
    if news:
        newest = max((n.get("day") or "") for n in news)
        batch = [n for n in news if (n.get("day") or "") == newest]
        ranked = sorted(batch, key=lambda n: n.get("published") or "", reverse=True)[:MAX_NEWS]
        label = "最近" if newest[:10] else "今日"
        return ranked, len(batch), label
    return [], 0, "今日"


def build_card() -> dict:
    papers = read_json(DATA_DIR / "papers.json", [])
    news = read_json(DATA_DIR / "news.json", [])
    meta = read_json(DATA_DIR / "meta.json", {})
    if isinstance(papers, dict):
        papers = papers.get("items", [])
    if isinstance(news, dict):
        news = news.get("items", [])

    today_dt = datetime.now(CN_TZ)
    today = today_dt.strftime("%Y-%m-%d")
    base = resolve_pwa_url()

    top_papers, paper_count, paper_label = pick_papers(papers, today)
    top_news, news_count, news_label = pick_news(news, today)
    ai_ready = bool(meta.get("papers", {}).get("ai_summary"))

    elements: list[dict] = []

    if top_papers:
        lines = [f"**📄 {paper_label}论文 · {paper_count} 篇**", ""]
        lines += [paper_line(i, p, base) for i, p in enumerate(top_papers, 1)]
        if paper_count > len(top_papers):
            lines += ["", f"[查看全部 {paper_count} 篇 →]({pwa_link(base, 'papers')})"]
        elements.append({"tag": "div", "text": {"tag": "lark_md", "content": "\n".join(lines)}})
    else:
        elements.append(
            {
                "tag": "div",
                "text": {"tag": "lark_md", "content": "**📄 今日论文**\n\n今天还没有抓到新的论文，稍后再看看。"},
            }
        )

    elements.append({"tag": "hr"})

    if top_news:
        lines = [f"**📰 {news_label}新闻 · {news_count} 条**", ""]
        lines += [news_line(i, n) for i, n in enumerate(top_news, 1)]
        elements.append({"tag": "div", "text": {"tag": "lark_md", "content": "\n".join(lines)}})
    else:
        elements.append(
            {"tag": "div", "text": {"tag": "lark_md", "content": "**📰 新闻**\n\n今天还没有新的新闻。"}}
        )

    elements.append({"tag": "hr"})
    elements.append(
        {
            "tag": "action",
            "actions": [
                {
                    "tag": "button",
                    "text": {"tag": "plain_text", "content": "📄 打开论文页"},
                    "url": pwa_link(base, "papers"),
                    "type": "primary",
                },
                {
                    "tag": "button",
                    "text": {"tag": "plain_text", "content": "📰 打开新闻页"},
                    "url": pwa_link(base, "news"),
                    "type": "default",
                },
            ],
        }
    )

    footer = "由 GitHub Actions 每日 08:30（北京时间）自动推送"
    ai_model = (meta.get("papers") or {}).get("ai_model")
    if ai_ready and ai_model:
        footer += f" · 中文摘要由 {ai_model} 生成"
    elif not ai_ready:
        footer += " · 未配置 AI 摘要，中文摘要将以英文原文呈现"
    elements.append(
        {"tag": "note", "elements": [{"tag": "plain_text", "content": footer}]}
    )

    return {
        "msg_type": "interactive",
        "card": {
            "config": {"wide_screen_mode": True, "enable_forward": True},
            "header": {
                "template": "indigo",
                "title": {"tag": "plain_text", "content": f"今日精进 · {human_day(today_dt)}"},
                "subtitle": {"tag": "plain_text", "content": f"{paper_count} 篇论文 · {news_count} 条新闻"},
            },
            "elements": elements,
        },
    }


# ------------------------------------------------------------------------ send


def main() -> None:
    parser = argparse.ArgumentParser(description="Send the daily digest to Feishu")
    parser.add_argument("--dry-run", action="store_true", help="print the payload, do not send")
    parser.add_argument("--webhook", default="", help="override FEISHU_WEBHOOK_URL")
    args = parser.parse_args()

    load_local_env()
    card = build_card()
    payload = card

    webhook = (args.webhook or os.environ.get("FEISHU_WEBHOOK_URL") or "").strip()
    secret = (os.environ.get("FEISHU_SIGNKEY") or "").strip()

    if secret:
        payload = {**card, **sign_payload(secret)}

    if args.dry_run:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return

    if not webhook:
        log("feishu", "FEISHU_WEBHOOK_URL is empty - printing the card instead of sending")
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return

    try:
        response = http_post_json(webhook, payload, timeout=25, retries=3)
    except Exception as exc:  # noqa: BLE001
        log("feishu", f"send failed: {exc}")
        sys.exit(1)

    if not isinstance(response, dict):
        response = {"_raw": response}

    code = response.get("code", response.get("StatusCode", -1))
    ret = response.get("data") or {}
    ok = code in (0, "0") and not (isinstance(ret, dict) and ret.get("code") not in (None, 0, "0"))
    if ok:
        log("feishu", "card delivered ✔")
        return

    # Usually: wrong webhook, bot removed from the group, or 签名校验 mismatch.
    log("feishu", f"Feishu rejected the card: {json.dumps(response, ensure_ascii=False)[:400]}")
    sys.exit(1)


if __name__ == "__main__":
    main()
