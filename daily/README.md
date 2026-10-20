# 每日精进 · `/daily/`

博客站的每日阅读面板：**零服务器、零数据库**，全部由 GitHub Actions 在每天早上生成静态数据。

- 📄 arXiv 上 AI / Agent / LLM 方向的新论文，按 `keywords.txt` 过滤 + 相关性打分
- 📰 11 个 RSS 源的技术新闻（Hacker News / OpenAI / DeepMind / TechCrunch AI / 量子位 …）
- 🈶 智谱 GLM-4.7 生成中文标题 + 三条中文摘要 + 主题词
- 🃏 每天早上（北京时间 8 点左右）把卡片推送到飞书群
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
GitHub Actions 定时（UTC 19:30 = 北京 03:30 提前起跑）
   ├─ 等到 07:50（北京）——GitHub 通常把运行拖后 4 个多小时，正好拖到 08:00
   ├─ scripts/daily/fetch_papers.py → daily/data/papers.json
   ├─ scripts/daily/fetch_news.py   → daily/data/news.json
   ├─ scripts/daily/send_feishu.py  → 飞书卡片 + last_push.json
   ├─ 提交 daily/data 并 push
   └─ 构建 dist/ → 部署 GitHub Pages（博客 + /daily/ 一起发布）
```

> 抓取步骤只在非 `push` 事件、且「今天还没推过」时执行；平时推文只走构建部署。

## 为什么以前总是 13:00 才收到卡片

不是巧合，也不是脚本慢，是 **GitHub 自己的 cron 被整体拖后了**：

| 时间（北京） | 事件 |
| --- | --- |
| 08:30 | cron `30 0 * * *` 到点，GitHub 把这次运行放进队列 |
| **12:56** | 运行才真正开始（`04:56 UTC`，晚了 **4 小时 26 分**） |
| 13:00:52 | `fetch_papers.py` 跑完，`meta.json` 里就是 `2026-10-19T05:00:52Z` |
| 13:01 | 卡片才发到飞书 |

`daily/data/meta.json` 里那行 `generated_at: 2026-10-19T05:00:52Z`，换算成北京时间正好是
13:00:52——这就是最硬的证据：卡片的**内容**是 13:00 生成的，说明整条流水线 13:00 才拿到 CPU，
而不是 08:30 生成、在队列里躺了 4 小时。

之前那版「一天打三枪（`30 0,1,2 * * *`）+ 幂等判断」为什么没用：

1. **延迟是系统性的**，三枪一起被拖后 4 个多小时，最早那枪照样落在 13:00。补跑只能补「失败」，
   补不了「排队」。
2. **`concurrency` 组里只允许一个 pending**。00:30 那次还在排队时，01:30 那次进来会把它取消；
   02:30 再进来又取消 01:30。所以名义上三枪，实际只有「最后一枪」在排队——连三次机会都没有。
3. 幂等判断（`last_push.json`）解决的是**重复推送**，不能把一次运行**提前**。

这是 GitHub 的平台行为，不是仓库配置能调好的：官方文档写明 `schedule` 是尽力而为，
高负载时会被延迟甚至丢弃；社区里同样的报告很多
（[延迟 4h23m](https://github.com/orgs/community/discussions/210625)、
[连续每晚都晚](https://dev.to/ji_ai/github-actions-cron-was-late-30-nights-straight-then-skipped-one-4751)、
[新账号/新仓库被拖 8–14 小时](https://github.com/orgs/community/discussions/201738)）。
新账号、低活跃仓库尤其容易被排在后面。

## 现在怎么让卡片在早上到

核心一句：**既然 GitHub 固定拖后约 4.5 小时，那就提前 4.5 小时起跑；起来之后先等到
07:50 再干活，避免它哪天突然变准点、凌晨 3:30 就把卡片发出去。**

```yaml
- cron: "30 19 * * *"   # UTC 19:30 = 北京 03:30，提前起跑
```

作业里的 `等到不早于 07:50（北京）` 这一步会算出现在离 07:50 还有多少秒，然后 `sleep`
（最多睡 5 小时，保住 GitHub 单作业 6 小时的上限）。所以：

| GitHub 实际延迟 | 起跑（北京） | 卡片送达 |
| --- | --- | --- |
| 0（突然变准点） | 03:30 | 先睡到 07:50 → 07:55 送达 |
| **4.5 小时（当前）** | **08:00** | **08:05 送达** |
| 6 小时 | 09:30 | 09:35（晚了，见下面调法） |
| 10 小时 | 13:30 | 13:35（同上） |

**哪天又开始晚到，就说明延迟变长了**：晚 2 小时就把 `30 19` 改成 `30 17`，以此类推。
判断依据是 `last_push.json` 里的 `late_min`，或卡片底部那行
「实际送达 13:0x，比计划晚 4 小时 30 分」。连着几天 `late_min` 都是 0，就说明当前 cron 是对的。

**备选方案（更稳，但要一次性配置）**：上面的做法是「猜延迟」，延迟一变就得手动调。
想彻底不等延迟，就用仓库外面的定时器在 08:20 调一次 dispatch——`workflow_dispatch` 走即时队列，
几秒就开跑：

1. 建 fine-grained PAT：只选这个仓库，Repository permissions 只给 **Actions: Read and write**；
2. 在 cron-job.org（免费）建每天的 00:20 UTC 任务：`POST`
   `https://api.github.com/repos/hanlinli666/hanlinli666.github.io/actions/workflows/deploy.yml/dispatches`，
   header `Authorization: Bearer <PAT>` + `Content-Type: application/json`，body `{"ref":"main"}`；
3. 返回 `204` 就通了，Actions 里会立刻多一条 `workflow_dispatch` 运行，08:25 左右卡片到。

Token 只放在 cron-job.org 的任务里，不要提交进仓库。两条通道并存也不会重复发卡片——
`last_push.json` 会拦住第二次。

## 兜底与幂等

```yaml
- cron: "30 19 * * *"   # 北京 03:30，主通道（提前起跑 + 等到 07:50）
- cron: "30 4 * * *"    # 北京 12:30，只在 19:30 那次被 GitHub 整个丢掉时兜底
```

每次 `schedule` 运行都会先看 `daily/data/last_push.json` 里的 `date`（北京时间）：

- 已经是今天 → 抓取、推送全部跳过（只做一次部署，几秒钟），所以兜底运行**不会重复发卡片**
- 还不是今天 → 正常抓取 + 推送，成功后写新的 `last_push.json` 并和数据一起提交

另外几条约定：

- 飞书步骤带 `continue-on-error: true`：机器人挂了也不会挡住站点发布；失败时不写
  `last_push.json`，下一次运行会自动重试。
- `concurrency.cancel-in-progress: false`：避免后一次运行把还在跑的那次掐断。
- 睡醒之后会 `git pull --ff-only` 一次，避免拿几个小时前的旧提交部署站点。

`last_push.json` 里除了 `date`，还记了 `at`（UTC）、`late_min`（比计划晚多少分钟）、
`trigger`（`schedule` / `workflow_dispatch` / `local`）——排查「到底几点推的」看这一个文件就够。

手动补跑：Actions 页面 `Run workflow`（默认就是「抓取 + 推送」），勾 `force` 则忽略今天的标记
强制再发一次；也可以走 API：

```bash
curl -X POST -H "Authorization: Bearer <TOKEN>" -H "Accept: application/vnd.github+json" \
  -H "Content-Type: application/json" -d '{"ref":"main"}' \
  https://api.github.com/repos/hanlinli666/hanlinli666.github.io/actions/workflows/deploy.yml/dispatches
```

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

**卡片又变成中午 13:00 才到？** 说明这次是被拖后了约 4.5 小时，而 cron 还是北京时间 03:30 起跑。
两点确认：① Actions 里那条 `schedule` 运行的「开始时间」是几点（这是 GitHub 的真实延迟量）；
② `daily/data/last_push.json` 里的 `late_min`。把 `30 19` 按延迟量往前挪即可
（晚 2 小时 → `30 17`）。另外看一眼 `trigger`：如果写的是 `workflow_dispatch`，那是手动触发的。

**周末论文是 0？** arXiv 周六日不发布，卡片与页面会自动退回到最近一个工作日。
