# Personal · 博客与思考

个人博客与思考站点，部署在 GitHub Pages。
本站用于记录博客文章与日常思考。

## 站点结构

```
.
├── index.html                # 主页（搜索 / 筛选 / 文章卡片）
├── assets/                   # 博客文章（独立的 HTML 文件）
│   └── wealth-plan.html
├── scripts/
│   ├── generate-manifest.js  # 清单生成器（纯 Node，无依赖）
│   └── daily/                # 每日精进数据管道（Python，纯标准库）
├── daily/                    # 每日精进 PWA，发布在 /daily/
│   ├── index.html            # 论文 / 新闻 / 收藏
│   ├── sw.js                 # Service Worker（离线可读）
│   ├── data/                 # papers.json / news.json / meta.json（自动生成）
│   └── keywords.txt          # 论文关键词
├── .github/workflows/
│   └── deploy.yml            # 自动构建并部署到 GitHub Pages（含每日抓取）
└── manifest.json             # 自动生成，无需手动维护（已加入 .gitignore）
```

## 如何新增一篇文章

只需把一个独立的 HTML 文件放进 `assets/` 目录，然后提交推送即可，
**不需要改动任何其它文件**——主页会自动出现新的文章卡片。

建议在文章 `<head>` 中补充以下元信息，主页会自动读取并展示：

```html
<head>
  <title>文章标题</title>
  <meta name="description" content="一句话摘要，会显示在卡片上">
  <meta name="keywords" content="思考, AI, 投资">
  <meta name="date" content="2026-08-09">
</head>
```

字段说明（均可选，缺省会自动回退）：

| 字段         | 来源                                              | 缺省回退             |
| ------------ | ------------------------------------------------- | -------------------- |
| 标题 title   | `<title>` 标签                                     | 文件名（去掉 .html） |
| 摘要 description | `<meta name="description">`                   | 留空                 |
| 标签 tags    | `<meta name="keywords">`，按逗号拆分              | 无标签               |
| 日期 date    | `<meta name="date">`                              | 该文件最近一次 git 提交时间 |

## 工作原理

GitHub Pages 是纯静态托管，浏览器无法直接“列出目录”，因此本站采用：

1. `scripts/generate-manifest.js` 扫描 `assets/*.html`，提取每篇文章的元信息，
   生成一份轻量的 `manifest.json`；
2. 主页 `index.html` 通过 `fetch('manifest.json')` 读取清单，再渲染为文章卡片；
3. `.github/workflows/deploy.yml` 在每次推送到 `main` 时自动执行第 1 步并部署，
   所以你只要把新文章丢进 `assets/` 并推送，站点就会自动更新。

这样的好处是：卡片列表只加载一个很小的 JSON，**首屏极快**；
搜索与标签筛选全部在前端完成，**即时响应**。

## 首次启用部署（一次性设置）

1. 进入仓库 **Settings → Pages**；
2. **Build and deployment → Source** 选择 **GitHub Actions**；
3. 推送代码到 `main` 分支，工作流会自动构建并部署。

部署完成后访问地址为：<https://hanlinli666.github.io/hanlinli666/>
（所有资源均使用相对路径，因此在子路径下也能正常工作。）

## 本地预览

需要本地安装 Node（用于生成清单）和任意静态服务器：

```bash
# 1. 生成清单
node scripts/generate-manifest.js

# 2. 启动静态服务器（任选其一）
python3 -m http.server 8000
#   然后浏览器打开 http://localhost:8000
```

> 说明：直接用 `file://` 打开主页无法读取 `manifest.json`（浏览器安全限制），
> 因此请通过本地服务器预览。

## 主题

主页支持浅色 / 深色主题，点击右上角图标切换，偏好会保存在本地。

## 每日精进（/daily/）

站点根目录下挂了一个可安装的每日阅读 PWA：每天早上抓 arXiv 论文 + 34 个新闻源（部委政策、中文媒体、热榜、国外 RSS），
新闻先过一层确定性关键词过滤，英文标题用智谱 GLM-4.7 翻成中文（摘要直接用原文，不调用模型），最后把摘要卡片推送到飞书群；论文链路仍会生成中文标题与三条 AI 摘要。

- 入口：<https://hanlinli666.github.io/daily/>（主页顶栏与首屏也有入口）
- 详细说明：见 [`daily/README.md`](daily/README.md)
- 新闻每 2 小时由 `collect.yml` 增量采集并提交数据，`deploy.yml` 再通过 `workflow_run` 重新发布；论文与飞书简报仍在每天 07:50 的那次部署里抓取
- 新增论文关键词 → 改 `daily/keywords.txt`；增删新闻源 → 改 `daily/config/sources.json`；改进出词 → 改 `daily/config/news_keywords.txt`

日常写文章完全不受影响：推送到 `main` 只会走「生成 manifest → 构建 → 部署」，
`deploy.yml` 里的论文抓取与飞书推送只在定时或手动勾选 `fetch` 时才执行（新闻由 `collect.yml` 独立采集）。

