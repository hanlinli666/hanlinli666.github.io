# 每日精进 · `/daily/`

博客站的每日阅读面板：**零服务器、零数据库**，全部由 GitHub Actions 在每天早上生成静态数据。

- 📄 arXiv 上 AI / Agent / LLM 方向的新论文，按 `keywords.txt` 过滤 + 相关性打分
- 📰 11 个 RSS 源的技术新闻（Hacker News / OpenAI / DeepMind / TechCrunch AI / 量子位 …）
- 🈶 智谱 GLM-4.7 生成中文标题 + 三条中文摘要 + 主题词
- 🃏 每天 08:30（北京时间）把卡片推送到飞书群
- 📱 可安装到主屏幕，独立窗口、离线可读
- 💾 收藏 / 已读 / 连续打卡都存在浏览器本地

## 目录

```
daily/
├── index.html              # 三栏：论文 / 新闻 / 收藏（无框架、无构建）
├── assets/app.css, app.js
├── sw.js                   # 壳缓存 + 数据「网络优先」
├── manifest.webmanifest    # start_url=./  scope=./  standalone
├── icons/                  # 192 / 512 / maskable / apple-touch
├── data/
│   ├── papers.json         # 论文（约 21 天滚动窗口）
│   ├── news.json           # 新闻（7 天窗口，每天上限 30 条）
│   ├── meta.json           # 生成时间 / 关键词 / 计数
│   └── cache/summaries.json# AI 摘要缓存（随仓库提交，换机器不丢）
├── keywords.txt
└── config/
    ├── news_sources.txt
    └── site.json
```

配套脚本在 `../scripts/daily/`（Python 3.9+，**纯标准库**，Actions 无需 pip install）。

## 数据流

```
GitHub Actions（每天 00:30 UTC = 北京 08:30）
   ├─ scripts/daily/fetch_papers.py → daily/data/papers.json
   ├─ scripts/daily/fetch_news.py   → daily/data/news.json
   ├─ 提交 daily/data 并 push
   ├─ scripts/daily/send_feishu.py  → 飞书卡片
   └─ 构建 dist/ → 部署 GitHub Pages（博客 + /daily/ 一起发布）
```

> 抓取步骤只在 `schedule` 事件或手动勾选 `fetch` 时执行；日常推文只走构建部署。

## 定时与补跑

GitHub Actions 的 `schedule` 是**尽力而为**，不是精确闹钟：高峰期限流时，整点的任务可能被推迟
（实测 2026-10-19 那次 00:30 UTC 的触发被拖到了 04:56 UTC 才真正开始，晚了 4 小时 26 分）。

所以这里用了「一天打三枪 + 幂等判断」的做法：

```yaml
- cron: "30 0,1,2 * * *"   # 北京 08:30 / 09:30 / 10:30
```

每次运行先看 `daily/data/last_push.json` 里的 `date`：

- 已经是今天 → `skip=true`，抓取、推送全部跳过（只做一次部署，几秒钟）
- 还不是今天 → 正常抓取 + 推送，成功后 `send_feishu.py` 写下新的 `last_push.json`

`last_push.json` 由 `send_feishu.py` 在**推送成功之后**写入，并且和数据一起提交。
好处是「今天到底推没推过」有据可查，而不是靠时间猜——08:30 那次被拖到 12:00 也无所谓，
只要它成功了，09:30/10:30 就不会重复发；如果它失败了（没写标记），下一枪自动补上。

配套的两个细节：

- 飞书步骤带 `continue-on-error: true`：机器人挂了也不会挡住站点发布。
- `concurrency.cancel-in-progress: false`：避免 09:30 那次把还在跑的 08:30 那次掐断。

万一哪天真的一整天都没跑（比如 Actions 被禁用），可以手动 `Run workflow` 勾 `fetch` 补跑，
手动触发**不受** `last_push.json` 限制，一定执行。

## 需要的 Secrets / Variables

| 类型 | 名称 | 必需 | 说明 |
| --- | --- | --- | --- |
| Secret | `FEISHU_WEBHOOK_URL` | ✅ | 飞书自定义机器人 Webhook |
| Secret | `BIGMODEL_API_KEY` | 建议 | 智谱 BigModel（`xxxx.yyyy` 格式） |
| Secret | `DASHSCOPE_API_KEY` | 备选 | 与上面二选一，同时存在时优先智谱 |
| Secret | `FEISHU_SIGNKEY` | 可选 | 机器人开了「签名校验」才需要 |
| Variable | `BIGMODEL_MODEL` | 可选 | 默认 `glm-4.7` |
| Variable | `LLM_THINKING` | 可选 | `off`（默认）/ `on` / `auto` |

本地调试时把同样的键写进仓库根目录的 `.env.local`（已在 `.gitignore` 中）。

## 调参

| 想改什么 | 改哪里 |
| --- | --- |
| 论文关键词 | `daily/keywords.txt` |
| 新闻源 | `daily/config/news_sources.txt` |
| 推送时间 | `.github/workflows/deploy.yml` 的 `cron`（**UTC**） |
| 每天抓多少论文 | `PAPERS_MAX_RESULTS`（默认 220）/ `PAPERS_LOOKBACK_DAYS`（默认 4 天） |
| 新闻回看 / 每天条数 | `NEWS_LOOKBACK_DAYS` / `NEWS_PER_DAY_LIMIT` |
| AI 摘要条数 | `SUMMARIZE_LIMIT`（默认 40/次，结果永久缓存） |
| 请求节流（防 429） | `LLM_MIN_INTERVAL`（默认 4 秒）/ `LLM_WORKERS`（默认 3） |

`keywords.txt`：每行一个「或」条件，行内空格视为整句短语，`+` 表示「且」：

```
LLM agent
agent harness
agent + reinforcement learning
```

`news_sources.txt`：`名称 = 地址`，名称前加 `!` 即停用。单个源超时 / 404 / XML 损坏都不影响其它源。

## 关于智谱 GLM

接口：`https://open.bigmodel.cn/api/paas/v4/chat/completions`（OpenAI 兼容）。两个坑：

1. **`glm-4.7` / `4.6` / `4.5-flash` 是推理模型**。不关 thinking 时思维链会先吃掉 `max_tokens`，
   给少了正文直接是空字符串。脚本里按 `LLM_THINKING` 决定是否带 `"thinking": {"type": "disabled"}`。
2. **免费额度有速率限制**（429 / `code=1302`）。请求串行节流（默认 4 秒一个），
   撞上限按 10s / 20s / 30s 退避，最多 5 次。

`LLM_THINKING` 实测对比（同一篇论文）：

| 模式 | 单篇 token | 单篇耗时 | 40 篇总耗时 |
| --- | --- | --- | --- |
| `off`（默认） | ~700 | ~8s | ~2 分钟 |
| `on` | ~2300（含 ~1600 思考） | ~45s | ~10 分钟 |
| `auto` | 混合 | 混合 | — |

差别主要在数字与措辞的保真度（`1.4 万条` vs `14126 个`），结构与可读性基本一致，
但 `on` 要多花 3.3 倍 token、5–6 倍等待，也更容易撞限流——所以默认 `off`。

## 本地预览

```bash
node scripts/generate-manifest.js      # 生成博客清单
python scripts/daily/fetch_papers.py   # 抓论文（需 .env.local 里的 Key 才有中文摘要）
python scripts/daily/fetch_news.py     # 抓新闻
python -m http.server 8099             # 开在仓库根目录
# → http://127.0.0.1:8099/daily/
```

Service Worker 只在 `https://` 或 `localhost / 127.0.0.1` 下注册，`file://` 打开会失去离线与安装能力。

## 排错

**改了样式手机上没变？** Service Worker 是「先给旧内容、后台更新」，刷新两次即可；
正式发布时把 `daily/sw.js` 顶部的 `VERSION` 改成 `v2` 会立即清掉旧缓存。

**`/daily/` 404？** 检查 `deploy.yml` 的「准备发布目录」里有没有 `cp -r daily/. dist/daily/`。

**飞书没收到？** 看 `推送飞书卡片` 步骤日志，脚本会把飞书返回的 `code` / `msg` 原样打印出来。

**周末论文是 0？** arXiv 周六日不发布，卡片与页面会自动退回到最近一个工作日。
