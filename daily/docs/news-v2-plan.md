# 新闻层 v2 规划：「源头优先」的 AI / LLM / Agent 情报流

> 目标读者：执行者（AI Agent）与用户本人
> 现状基线：2026-10-20 数据，`daily/data/news.json` 共 160 条 / 7 天，11 个源
> 本文只规划 `/daily/` 的新闻链路，**不动论文链路、不动博客站**

---

## 0. 一句话结论

新闻「太少」不是抓取能力问题，而是**源太少 + 每天只跑一次 + 30 条/天硬截断 + 没有中文源**这四件事叠加。
最小改动的最优路径：**把 11 个源扩到 20–25 个（含 2–3 个部委源头）+ 改成每 2 小时增量 + 去掉条数上限 + 加一层确定性关键词过滤和英文标题翻译**，全部复用现有 Python 标准库 + GitHub Actions，不引入新服务。

---

## 1. 现状量化诊断

从仓库里读出来的事实，不是猜测：

| 维度 | 现状（代码 / 数据） | 后果 |
|---|---|---|
| 源数量 | `daily/config/news_sources.txt` 共 11 个启用源 | 每天自然产出 ~23 条 |
| 中文源 | 只有 `量子位` 1 个 | 国内政策、财联社、澎湃、36氪全缺 |
| 日条数上限 | `NEWS_PER_DAY_LIMIT=30`、`NEWS_TOTAL_LIMIT=160` | 就算源变多，也会被截断 |
| 抓取频率 | `deploy.yml` 每天 1 次（19:30 UTC 提前起跑 + 睡到 07:50） | 08:00 之后发生的新闻要等 24h |
| 翻译 | **完全没有** | 英文源只能看原文 |
| 关键词过滤 | 新闻链路**没有**（只有论文有 `keywords.txt`） | 所有条目无差别进入列表 |
| LLM | 新闻链路**不调用** | 无摘要、无事件结构 |
| 层级 | 无 L1/L2/L3 概念 | 部委公告和 Hacker News 平铺混排 |
| 热榜 | 无 | 无法判断「是否已发酵」 |

数据佐证：`meta.json` 记录 7 天 160 条，其中 `量子位` 约 32 条、其余全是英文源；10-20 当日 29 条——这已经是**上限附近**，说明瓶颈在源和上限，不在抓取成功率。

另外，`开发文档.md` / `daily/README.md` 里已经写清楚：GitHub 的 `schedule` 在本仓库会被拖后约 4.5 小时。**这一条决定了「及时」不能只靠一次 cron**，见 §8。

---

## 2. 目标（对齐这次需求）

1. 每天稳定产出 **40–80 条**中文可读的 AI / LLM / Agent / 科技条目，而不是 20 条。
2. 源头事件（部委公告、征求意见）在 **数小时内**进入推送，不等热搜。
3. 每条是「**中文标题 + 原文摘要（≤260 字）+ 一个原始链接**」，点击直达原文。
4. 保留零服务器：GitHub Actions + Pages + 飞书 Webhook，不新增付费服务。
5. LLM 只做英文标题翻译；时间、层级、去重、关键词、摘要全部由确定性脚本或原文产生。

**非目标**：全量新闻存档、情感分析、趋势预测、实时流式。

---

## 3. 总体架构

```
┌─ 采集层（Python 标准库，全部在 Actions 里跑） ─────────────────────┐
│  daily/config/sources.json  ← 唯一源清单（增删源只改这里）          │
│    ├─ rss   : RSS/Atom（沿用现有解析器，容错已打磨过）              │
│    ├─ json  : 声明式字段映射（知乎热榜 / 澎湃 / 华尔街见闻 …）       │
│    ├─ html  : 列表页正则抽取（科技部通知 / 商务部首页政策区块）      │
│    └─ rsshub: 公共镜像路由（部委 / 36氪 / 财联社 / 百度），双路回退  │
│  输出：raw 条目（title, summary, url, published, source, level）    │
└──────────────────────────────┬─────────────────────────────────────┘
                               ▼
┌─ 预处理层（零 LLM） ────────────────────────────────────────────────┐
│  ① 时间窗 + URL 归一化去重（已有）                                  │
│  ② 确定性关键词过滤  daily/config/news_keywords.txt                │
│     普通词 | + 必须词 | ! 排除 | [GLOBAL_FILTER] | /正则/ | @N      │
│     双语词表：中文词 + 英文词（AI/LLM/agent/OpenAI…）               │
│  ③ 按源分档：filter=keyword（默认） / filter=all（AI 垂媒整feed收） │
│  输出：候选池（目标 80–150 条/天，通常 < 原始量 20%）               │
└──────────────────────────────┬─────────────────────────────────────┘
                               ▼
┌─ LLM 层（glm-4.7-flash，只做标题翻译 + 缓存） ─────────────────────┐
│  翻译：仅对「标题里没有中文」的条目，25 条/次批量 → title_zh        │
│  其余全部零 LLM：摘要用原文（≤260 字）、主题词用关键词命中、        │
│  政策阶段用 L1 标题规则、相关度由关键词过滤把关                      │
│  缓存：daily/data/cache/news_llm.json（按 URL），重跑不重复花钱      │
│  （2026-10-22 修正：取消「精炼」，见 §7）                            │
└──────────────────────────────┬─────────────────────────────────────┘
                               ▼
┌─ 事件层（零 LLM） ─────────────────────────────────────────────────┐
│  跨源聚类（标题 bigram 相似度 + 72h 窗口）→ 一个事件多条来源        │
│  计算 first_seen_at（本系统最早看到）/ hot_seen_at（首次进热榜）     │
│  source_level 由源清单派生，不交给 LLM                              │
└──────────────────────────────┬─────────────────────────────────────┘
                               ▼
        daily/data/events.json   ← PWA「事件」流（3 天）
        daily/data/hot.json      ← L3 热榜快照（AI 相关才展示）
        daily/data/stats.json    ← 每级漏斗计数（可观测性 N3）
                               ▼
        飞书：每日简报（08:10） + L1 即时告警（发现即推）
```

---

## 4. 数据模型（事件，而不是文章）

```jsonc
{
  "id": "e_9f2c1a",                      // 规则生成，稳定
  "title_zh": "商务部就 AI 芯片出口管制公开征求意见",   // 中文标题（英文源翻译）
  "event_summary": "征求意见稿拟将…纳入出口许可清单，截止 11/15。", // 原文摘要 ≤260 字
  "source_level": "L1",                  // L1官方 / L2媒体 / L3社区（源清单派生）
  "policy_stage": "征求意见",             // 征求意见/已发布/解读/传闻/不适用
  "key_entities": ["商务部", "AI 芯片"],
  "potential_impact": "若落地，先进算力出口需逐单审批。",
  "topics": ["AI", "政策", "芯片"],       // 前端筛选用
  "first_seen_at": "2026-10-16T02:11:00Z", // ★ 本系统最早看到的时间（脚本算）
  "first_source": "商务部",
  "hot_seen_at": null,                    // 首次进入 L3 热榜的时间
  "published": "2026-10-16T01:40:00Z",    // 原文发布时间
  "links": [                              // 一个事件多个来源
    {"source": "商务部", "level": "L1", "title": "关于…征求意见的通知",
     "url": "https://www.mofcom.gov.cn/…", "published": "2026-10-16T01:40:00Z"},
    {"source": "财联社", "level": "L2", "title": "…", "url": "https://www.cls.cn/…"}
  ],
  "primary_url": "https://www.mofcom.gov.cn/…",  // 前端「原文」按钮用
  "needs_translation": false
}
```

**字段分工（关键设计）**

| 字段 | 谁产生 | 原因 |
|---|---|---|
| `source_level` | 源清单（规则） | LLM 会瞎标；源清单里写死才可靠 |
| `first_seen_at` | 事件库（规则） | LLM 不知道「我们第几次抓到」，只有持久化状态知道 |
| `hot_seen_at` | 热榜快照（规则） | 同上 |
| `title_zh` | GLM 翻译（仅英文标题） | 唯一必须用到模型的地方 |
| `event_summary` | 原文摘要截断 | 100–260 字的事实陈述，比模型改写更可靠 |
| ↑ 源没有摘要时 | 抓一次正文，取前 200 字 | 部委列表页 / 热榜只有标题；每轮最多补 12 篇，失败记缓存不重试 |
| `policy_stage` | L1 标题规则 | 只对部委公告有意义，规则足够 |
| `topics` | 关键词命中标签 | 零 LLM，和过滤口径天然一致 |
| `key_entities` / `potential_impact` | 不再生成（空值） | 前端已做空值兼容 |

> `开发文档.md` 把 `first_seen_at` 放在 LLM 的输出 JSON 里，这是不可靠的；本方案改为流水线计算，LLM 不参与。

---

## 5. 数据源清单 v2（已实测）

**验证环境**：2026-10-22 21:58，本机（广东电信出口），62 个候选源，脚本 [`probe_sources.py`](../../scripts/daily/probe_sources.py)，第一轮原始输出 [`source-probe.md`](source-probe.md)；RSSHub 镜像与备用接口的第二轮结果直接记在下表里。
**验证局限**：GitHub Actions 走海外 IP，与这里结果会不同；P0 仍要在 Actions 复跑一次再定 `enabled`。但「源站本身没有 RSS / 有 WAF / 需要签名」这类结论与出口 IP 无关，可以直接采信。

**一句话结论**：国内站点**直连成功率很低**（36氪 WAF、机器之心改版、微博 403、B站要 wbi 签名、部委栏目页是 JS 壳），所以采集层必须是「**直连 RSS/JSON + RSSHub 公共镜像 + 少量 HTML**」的组合，不能只靠一种。

### 5.1 L1 政策源头

| 源 | 栏目 | 接入方式（已跑通） | 实测 |
|---|---|---|---|
| 国务院 | 最新政策 | RSSHub `/gov/zhengce/zuixin` @ slarker | ✅ 20 条，首条即「推动科技创新和产业创新深度融合的意见」 |
| 工信部 | 政策文件 | RSSHub `/gov/miit/zcwj` @ slarker | ✅ 10 条，首条为「产业基础能力提升十五五规划」 |
| 工信部 | **意见征集** | RSSHub `/gov/miit/yjzj` @ slarker | ✅ 24 条，正是「公开征求意见」栏目 |
| 科技部 | 通知通告 | 直连 HTML `https://www.most.gov.cn/tztg/` | ✅ 20 条；相对链接 `./202610/t20261021_197538.html`，日期在路径里 |
| 商务部 | 政策发布 / 征求意见 | 直连**首页**，抽 `/zwgk/zcfb/art/` 与 `/zqyj/` 链接 | ✅ 首页 42 KB 有内容；**栏目页是 JS 壳（5 KB），抓不到** |
| 发改委 | 政策发布 | 直连 `/xxgk/zcfb/`、RSSHub `/gov/ndrc/zfxxgk` | ❌ 直连返回 1 字节，镜像 50 s 超时 |
| 网信办 | 信息公开 | 直连 / RSSHub `/gov/cac/*` | ❌ 本机 TLS 握手失败 |

> 商务部这条要特别处理：`/zqyj/zcfgzqyj/index.html` 只有 5 KB，正文由 JS 拉取；**只能监控首页里「政策法规征求意见」区块的链接**。这是「当天发现问题」的可用近似——首页更新通常跟栏目同步。

### 5.2 L2 中文专业媒体

| 源 | 接入 | 实测 | 备注 |
|---|---|---|---|
| 量子位 | `qbitai.com/feed`（直连） | ✅ 10 | AI 垂媒，`filter=all` |
| IT之家 | `ithome.com/rss/`（直连） | ✅ 60 | 量大，必须过关键词 |
| 爱范儿 | `ifanr.com/feed` | ✅ 20 | |
| 钛媒体 | `tmtpost.com/rss.xml` | ✅ 17 | |
| 雷峰网 | `leiphone.com/feed` | ✅ 20 | AI 报道质量高 |
| 极客公园 | `geekpark.net/rss` | ✅ 30 | |
| InfoQ 中文 | `infoq.cn/feed` | ✅ 20 | 原仓库标「不可用」，实测**可用** |
| 新浪科技 | `rss.sina.com.cn/tech/rollnews.xml` | ✅ 15 | |
| 少数派 | `sspai.com/feed` | ✅ 10 | |
| 财联社·电报 | RSSHub `/cls/telegraph` @ slarker | ✅ 20 | 时效最好的中文快讯 |
| 36氪·快讯 | RSSHub `/36kr/newsflashes` @ rssforever | ✅ 20 | 官方 `36kr.com/feed` 被 **WAF 挑战页**拦 |
| 澎湃·热新闻 | 直连 `cache.thepaper.cn/contentapi/wwwIndex/rightSidebar` | ✅ 20 | 官方 JSON，无需签名 |
| 华尔街见闻·快讯 | 直连 `api-one.wallstcn.com/apiv1/content/lives?channel=global-channel&limit=30` | ✅ 30 | 官方 JSON |
| 机器之心 | `jiqizhixin.com/rss` | ❌ | 302 到「数据服务」页，**已无 RSS** |
| 虎嗅 | `huxiu.com/rss/0.xml` | ❌ | 15 s 超时 |
| cnBeta | `cnbeta.com.tw/backend.php` | ❌ | 域名已跳到 MSN |
| 凤凰科技 / Solidot | — | ❌ | 404 / 超时 |

### 5.3 L3 社区热榜

| 源 | 接入 | 实测 |
|---|---|---|
| 知乎热榜 | 直连 `api.zhihu.com/topstory/hot-lists/total?limit=50` | ✅ 30 条（含 `detail_text` 热度） |
| 百度热搜 | RSSHub `/baidu/top` @ rssforever | ✅ 51 条 |
| 今日头条热榜 | 直连 `toutiao.com/hot-event/hot-board/?origin=toutiao_pc` | ✅ 50 条 |
| 掘金热榜 | 直连 `api.juejin.cn/.../article_rank?...` | ✅ 50 条 |
| GitHub Trending | 直连 HTML `github.com/trending?since=daily` | ✅ 72 个仓库链接 |
| Hacker News | `hnrss.org/frontpage?points=100` | ✅ 20 条 |
| 微博热搜 | 直连 JSON / HTML、两个 RSSHub 镜像 | ❌ 403 / 503，需自建 NewsNow |
| B站排行 | 直连 API | ❌ 返回 39 字节错误（需 wbi 签名），镜像超时 |

### 5.4 国外

全部实测可用的有：TechCrunch AI（20）、Ars Technica（20）、MIT TR（10）、The Verge AI（10）、OpenAI News（1313 条历史，**只取最新 15 条**）、Google DeepMind（100）、Google AI Blog（20）、Simon Willison（30）、Lilian Weng（53）、Product Hunt（50）、GitHub Blog、BAIR、MIT News AI（50）。`sources.json` 里启用了其中 10 个，其余（Product Hunt / GitHub Blog / BAIR）留作备选。

不可用：Anthropic `/rss.xml`（404）、Hugging Face（超时）、Import AI（超时）、VentureBeat AI（连接失败）。Reuters/Bloomberg 无稳定 RSS，**放弃**，用 TechCrunch + HN 兜底。

### 5.5 开源聚合层怎么选（这次的重点）

| 方案 | 覆盖 | 实测 | 结论 |
|---|---|---|---|
| **RSSHub 公共镜像** | 部委 / gov / 36氪 / 财联社 / 百度 / 知乎 / 澎湃 / 华尔街见闻 | `hub.slarker.me` ✅ 8 条路由；`rsshub.rssforever.com` ✅ 4 条；`rsshub.app` ❌ 30 s 超时 | **采用**：双镜像 + 路由级 fallback；冷路由可能要 20–30 s，超时给 60 s |
| **NewsNow 自建** | 50+ 源（`_36kr` `baidu` `bilibili` `cls` `thepaper` `wallstreetcn` `weibo` `douyin` `jin10` …） | 公共实例 403；自建需 Cloudflare Workers | **可选二期**：只为补微博/抖音/B站时才值得上 |
| **TrendRadar** | 聚合 + 推送，底层就是 NewsNow API | 需要 R2/S3 存状态，GPL-3.0 | **不引入运行时**，只借鉴 `frequency_words.txt` 语法 |
| 第三方热榜 API（vvhan / tenapi） | 全网红榜 | 本机均连接失败，且不可控 | 不采用 |

> 因此 §14 原来写的「不依赖 RSSHub 公共实例」要修正：**主链路要依赖它，但必须双镜像 + 单源失败隔离 + 连续 0 条告警**。这正是「复用成熟开源聚合层、把精力放在预处理和 LLM」的做法。

**总量控制**：已落 [`daily/config/sources.json`](../config/sources.json) **34 个启用源**（L1 5 / 中文 L2 13 / L3 8 / 国外 L2 8），另有 9 个实测不可用条目保留为 `enabled:false` 作为记录。类型分布：rss 20、rsshub 6、json 5、html 3。

---

## 6. 预处理：关键词语法（复用 TrendRadar 的约定）

`daily/config/news_keywords.txt`：

```
# [AI] 行内用 + 表示必须同时出现，! 表示排除
LLM + agent
AI + 芯片
大模型
生成式人工智能
Agent + 工具调用
# 英文（英文源在翻译前就能命中，省 LLM）
LLM agent
AI agent
OpenAI
Anthropic
DeepSeek
Gemini
Claude
GPT
# 正则（避免 ai 命中 training 这类子串）
/\bAI\b/
# 排除项
! 招聘
! 课程
! 广告

[GLOBAL_FILTER]
! 抽奖
```

规则：
- 普通词 = OR；`+` = AND；`!` = 排除；`/…/` = 正则；`@N` = 每组最多 N 条。
- 命中范围为 `title + summary`，双语词表混排。
- **英文源不必先翻译再过滤**：标题里 `OpenAI`、`LLM`、`agent` 本身就是英文，双语词表能直接命中，只有存活条目才进 LLM 做「标题翻译」。这比「先全量翻译再过滤」省 80% 以上 token，也符合 N2（能不用 LLM 就不用）。

---

## 7. LLM 分层（已收敛为「只翻标题」）

**2026-10-22 修正：取消精炼。** 原方案每批 5 条做「粗筛 + 摘要 + 实体 + 影响」，按每 2 小时一轮算，最多约 **96 次调用/天**，免费 `glm-4.7-flash` 会被限流（10-22 那轮就出现 `enriched: 0`）。而原文摘要本身就是 100–260 字的事实陈述，用户明确接受 260 字；其余字段都能由规则产生。所以只保留唯一无法替代的一步：**英文标题翻译**。

| 阶段 | 模型 | 批量 | 调用量 |
|---|---|---|---|
| 标题翻译（仅无中文的标题） | `glm-4.7-flash` | 25 条/次 | **≤12 次/天**（每轮最多 1 批；首次回填最多 4 批） |
| 摘要 | 无 | — | 原文摘要截断到 ≤260 字；源没有摘要就抓正文前 200 字 |
| 政策阶段 | 无 | — | L1 标题关键词（征求意见 / 公告 / 通知 / 规划…） |
| 主题词 | 无 | — | 命中的关键词规则标签 |
| 相关度 | 无 | — | 关键词过滤就是闸门；`filter=all` 源本身已是 AI 垂媒 |

- 只翻译 4 天事件窗口内、标题没有中文的条目；中文标题不调用。
- 稳定性兜底：
  1. 请求带 `response_format: {"type": "json_object"}`；模型不支持时自动去掉重试一次；
  2. 输出按 `json → 代码块剥离 → 首尾大括号截取` 三级解析；
  3. 单批失败只影响这一批，下一轮重试，不阻塞事件生成。
- 结果按 item id（URL 归一化）缓存进 `daily/data/cache/news_llm.json`，**同一个链接只翻一次**。
- 失败降级（N5）：翻译失败 → 保留原文标题 + `needs_translation: true`，事件照常入流。
- 键缺失（没配 `BIGMODEL_API_KEY`）→ 全链路仍可跑，只是英文标题不翻译，不报错。

---

## 8. 调度：每 2 小时增量

### 8.1 拆成两个工作流

| 工作流 | 触发 | 职责 |
|---|---|---|
| `collect.yml` | `schedule` 每 2 小时 + `workflow_dispatch` | 抓取 → 过滤 → LLM → 事件库 → 提交 data → L1 告警 |
| `deploy.yml` | `push`（`paths-ignore: daily/data/**` 除外） + 每日定时 | 构建 + 发布 Pages + 08:10 每日简报 |

数据提交后由 `deploy.yml` 的 push 触发重新发布（约 1 分钟），站点随即更新。给 `daily/data/**` 加 `paths-ignore` 防止自己触发自己。

### 8.2 cron 与延迟兜底（已确认：不配外部定时器）

`cron: "5 */2 * * *"`（UTC）= 北京 08:05 / 10:05 / … / 06:05，每天 12 轮。

已有实测（`daily/README.md`）：GitHub 在本仓库的 `schedule` 会被拖后约 4.5 小时。不配外部定时器时，靠「**多轮触发**」抵消：一轮被拖后，下一轮仍在队列里，事件不会整体丢掉，只是这一轮的延迟变长。

判断依据：`daily/data/last_push.json` 的 `late_min` 和 `stats.json` 的 `run_at`。若连续 3 天 `late_min > 240`，再把 `5 */2` 改成 `5 * * * *`（每小时）或补上外部 dispatcher（README 已有 cron-job.org 方案）；**在那之前不加配置**。

### 8.3 推送

| 类型 | 时机 | 内容 |
|---|---|---|
| 每日简报 | 北京 08:10 | 前 5 条事件（按 L1 > L2 > L3、再按时间）+ 热榜 AI 相关 Top 5 + PWA 入口 |
| L1 即时告警 | 发现即推（同一次 collect 内） | `source_level=L1` 且 `policy_stage ∈ {征求意见, 已发布}`，一条短卡片 + 原文链接 |
| 静默规则 | 22:30–07:30 | 只推 L1 告警，简报不改时间 |

告警去重：`daily/data/last_alert.json` 记录已告警的 `event_id`，同事件不重复推。

---

## 9. 前端改造（`daily/`）

保持「无框架、无构建、纯静态 JSON」的现状，只改数据和两张卡片。

**已确认：原来的「新闻」Tab 直接改成「事件」**（不新增第五个 Tab），热榜作为二级切换放在事件页顶部。

- Tab：`事件`（默认） · `论文` · `热榜` · `收藏`。
- 旧链接兼容：`#news` 自动重定向到 `#events`，`?q=` 深链继续可用（飞书卡片里的搜索链接不受影响）。
- 顶部统计从「论文 / 新闻 / 已读 / 打卡」改为「**事件 / 今日新增 / 政策 / 已读**」。
- 事件卡片（对齐需求第 6 条）：
  ```
  [L1 官方] [征求意见]                      3 小时前
  商务部就 AI 芯片出口管制公开征求意见            ← 中文标题（大）
  拟将先进算力芯片纳入出口许可清单，意见截止 11/15。   ← 50 字摘要
  影响：相关出口需逐单审批，云厂商海外算力采购受影响。
  #商务部 #AI芯片           首次发现 今天 10:12 · 来源 2 家
  ─────────────────────────────────────────────
  原文：mofcom.gov.cn ↗                          ← 唯一主链接
  ```
- 多来源事件：主链接给 `primary_url`，下面「另见 财联社、澎湃」小字折叠。
- 热榜 Tab：紧凑排行（排名 / 标题 / 热度 / 首次上榜时间），只显示命中 AI/科技关键词的条目，附「查看完整热榜」。
- 数据量：事件 3 天约 100–200 条，按天分组 + 现有搜索/收藏逻辑不变，性能无压力。

---

## 10. 可观测性（N3）与配置（N4）

`daily/data/stats.json`，每轮覆盖写：

```jsonc
{
  "run_at": "2026-10-23T02:05:11Z",
  "stages": {"raw": 812, "fresh": 640, "keyword": 118, "candidate": 96,
             "llm_relevant": 41, "events_new": 27, "events_total": 165},
  "sources": [{"id": "mofcom", "ok": true, "count": 6, "ms": 1240},
              {"id": "weibo", "ok": false, "error": "HTTP 403", "count": 0}]
}
```

- Action 的 Step Summary 打印这张漏斗表，调参不用翻日志。
- PWA 页脚显示「本轮 采集 812 → 关键词 118 → 相关 41」。
- 源连续 3 次 0 条 / 连续失败 → Summary 里 `::warning::`。

配置集中在：
```
daily/config/sources.json      # 源清单（层级、类型、URL、过滤档位）
daily/config/news_keywords.txt # 关键词规则
daily/config/settings.json     # 模型名、批量大小、保留天数、推送时间
```

---

## 11. 成本预估

| 项 | 用量 | 费用 |
|---|---|---|
| GitHub Actions | 公开仓库，每 2 小时 1 次 × ~3 分钟 ≈ 1100 分钟/月 | ¥0（公开仓库免费） |
| GLM-4.7-flash（只翻标题） | ≤12 次/天（每轮 1 批），命中缓存后更少 | **¥0**（免费模型） |
| RSSHub 公共镜像 | 每次 ~10 个路由 | ¥0 |
| Cloudflare Worker Cron | 暂不配置 | ¥0 |

> 注意：若仓库是 **private**，Actions 免费额度 2000 分钟/月，每 2 小时约 1100 分钟仍在额度内，但余量不多；当前是 `hanlinli666.github.io`（public），无限制。

---

## 12. 实施阶段与验收

| 阶段 | 内容 | 验收标准 |
|---|---|---|
| **P0 探源**（✅ 已完成） | `probe_sources.py` 实测 62 个候选源 | 33 个可用（§5），13 个明确不可用，其余为超时/待 Actions 复跑 |
| **P1 采集+过滤**（✅ 已完成） | `sources.json` + 四种适配器（rss/rsshub/json/html）+ 关键词过滤 + `stats.json` | 实测 **raw 695 → 关键词 132**，34/34 源可用，页面从 ~30 条/天 变成 96 条/天 |
| **P2 事件层**（✅ 已完成） | GLM 只翻英文标题 + 原文摘要 + 规则字段 + 聚类 + `first_seen_at` + `events.json` | 148 条 → 139 事件（聚类偏保守，宁可不合并）；中文标题可用，摘要为原文 ≤260 字；无 Key / 限流时自动降级为原文标题 |
| **P3 前端**（✅ 已完成） | 「新闻」Tab 换成「事件」：层级徽章 + 政策阶段 + 影响 + 首次发现 + 原文链接；旧 `#news` 重定向 | 第一屏看到的是「工信部发布…」「国务院…意见」这类中文事件，而不是英文标题流 |
| **P4 时效**（✅ 代码完成，待 Actions 首跑） | `collect.yml` 每 2 小时 + `send_feishu.py --alert` L1 告警；`deploy.yml` 由 `workflow_run` 重新发布 | 部委公告发布到飞书告警 < 2h（取决于源站发布节奏） |
| **P5 调参** | 按 stats 删除噪声源、收紧关键词 | 噪音率（读完觉得没用）< 20% |

P1 完成就能直接解决「新闻太少」；P2/P3 解决「只是标题堆砌、没有中文结构」；P4 解决「不及时」。

---

## 13. 风险与对策

| 风险 | 影响 | 对策 |
|---|---|---|
| 部委页面改版 | L1 断供 | 列表页正则 + 连续 0 条告警；RSSHub 路由作备选 |
| 国内站点拦海外 IP | L2/L3 抓不到 | 先探测再启用；重点源保留 RSSHub 镜像或 NewsNow 镜像 |
| GitHub cron 延迟 | 时效差 | 每 2 小时多轮触发兜底；`late_min` 连续超标再上外部 dispatcher（§8.2） |
| 事件聚类误合并 | 两条不相关新闻并成一条 | 相似度阈值可配（默认 0.62）+ 同源不合并 + 保留多链接可回溯 |
| 噪声变多 | 阅读负担 | `@N` 每组限流 + `!` 排除词 + stats 里按源看存活率 |
| 仓库提交变频繁 | commit 噪音 | 每 2 小时一次、无变化不提交；`paths-ignore` 防递归 |

---

## 14. 明确不做的

- 不把 TrendRadar 作为运行时依赖（GPL + 需要 R2/S3 存状态），只借鉴它的 `frequency_words.txt` 语法。若 P5 后发现微博/抖音仍缺失，再考虑自建 NewsNow（Cloudflare Workers，免费）只取 L3。
- 不自建 RSSHub（要服务器/Workers + 定期升级），只用公共镜像 + 单源隔离 + 告警；镜像全挂时才回到直连。
- 不采用第三方「热榜聚合 API」（vvhan / tenapi 等）：本机全部连不上，且可用性不可控。
- 不改论文链路、不改博客首页、不引入数据库。

---

## 15. 已确认决策

| 决策项 | 结论 |
|---|---|
| 抓取频次 | **每 2 小时**（`5 */2 * * *` UTC），每天 12 轮 |
| 外部定时器 | **暂不配置**，先靠多轮触发兜底；`late_min` 连续 3 天 > 240 再上 |
| 页面结构 | **「新闻」Tab 改为「事件」**，中文结构化卡片 + 一个原文链接；旧 `#news` 链接重定向 |
| L1 部委 | 国务院 + 工信部（政策文件 & 意见征集）+ 科技部 + 商务部（首页区块）；发改委 / 网信办本期缺，配置里留停用项 |
| LLM 用途 | **只翻英文标题**（`glm-4.7-flash`，25 条/次）；取消精炼，摘要直接用原文 ≤260 字 |
| 采集基座 | 直连 RSS/JSON + **RSSHub 公共镜像（slarker / rssforever）双路 fallback** + 少量 HTML；不引入 TrendRadar 运行时 |

**当前进度**：P0 / P1 / P2 / P3 已完成，P4 代码已就位 —— 34 个源实测跑通（raw ~700 → 关键词 ~140），事件只调用模型翻英文标题，前端「事件」Tab 已上线，`collect.yml` 每 2 小时采集并推送 L1 告警，`deploy.yml` 通过 `workflow_run` 在数据更新后自动发布。

**下一步（P4 验收 / P5）**：先在 Actions 里手动跑一次 `Collect news`，确认海外 IP 下的源可用性、L1 告警卡片和提交-发布链路；把新的 ❌ 源标 `enabled: false`。随后看 3 天 `stats.json` 的每源存活率，做 P5 调参。

