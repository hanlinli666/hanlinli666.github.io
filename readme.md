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
│   └── generate-manifest.js  # 清单生成器（纯 Node，无依赖）
├── .github/workflows/
│   └── deploy.yml            # 自动构建并部署到 GitHub Pages
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
