#!/usr/bin/env node
/**
 * 博客清单生成器（纯 Node，无第三方依赖）
 *
 * 作用：扫描 assets/ 目录下的 *.html 文件，提取每篇文章的元信息，
 * 生成 manifest.json 供主页读取并渲染为卡片。
 *
 * 提取规则（按优先级）：
 *   - title:       <title> 标签内容，缺省时用文件名（去掉扩展名）
 *   - description: <meta name="description"> 或 og:description
 *   - tags:        <meta name="keywords">，按逗号拆分
 *   - date:        <meta name="date"> / article:published_time / <time datetime>
 *                  缺省时回退到该文件最近一次 git 提交时间
 *
 * 这样你以后只需把新的 html 丢进 assets/ 目录即可，无需改动其它文件。
 *
 * 用法：  node scripts/generate-manifest.js
 * 输出：  项目根目录下的 manifest.json
 */

'use strict';

const fs = require('fs');
const path = require('path');
const { execSync } = require('child_process');

const ROOT = path.resolve(__dirname, '..');
const ASSETS_DIR = path.join(ROOT, 'assets');
const OUT_FILE = path.join(ROOT, 'manifest.json');

// 只读取文件头部即可拿到 head 中的元信息，避免把整篇大文件读进内存
const HEAD_BYTES = 200 * 1024;

/** 去除标签、压缩空白、解码常用 HTML 实体 */
function cleanText(raw) {
  if (!raw) return '';
  return raw
    .replace(/<[^>]*>/g, ' ') // 去掉嵌套标签
    .replace(/&nbsp;/gi, ' ')
    .replace(/&amp;/gi, '&')
    .replace(/&lt;/gi, '<')
    .replace(/&gt;/gi, '>')
    .replace(/&quot;/gi, '"')
    .replace(/&#39;/gi, "'")
    .replace(/&#(\d+);/g, (_, n) => String.fromCodePoint(Number(n)))
    .replace(/\s+/g, ' ')
    .trim();
}

function firstMatch(re, text) {
  const m = text.match(re);
  return m ? m[1] : null;
}

function pickMeta(text, names) {
  for (const name of names) {
    // 匹配 <meta name="..." content="..."> 或 <meta content="..." name="...">
    const re = new RegExp(
      `<meta[^>]+(?:name|property)=["']\\s*${name}\\s*["'][^>]*?>`,
      'i'
    );
    const tag = firstMatch(re, text);
    if (tag) {
      const c = firstMatch(/content=["']([^"']*)["']/i, tag);
      if (c !== null) return c;
    }
    // 反过来 content 在前
    const re2 = new RegExp(
      `<meta[^>]+content=["']([^"']*)["'][^>]*?(?:name|property)=["']\\s*${name}\\s*["'][^>]*?>`,
      'i'
    );
    const m = text.match(re2);
    if (m) return m[1];
  }
  return null;
}

/** 取该文件最近一次 git 提交的 ISO 时间，失败返回 null */
function gitFileDate(relPath) {
  try {
    const out = execSync(`git log -1 --format=%cI -- ${JSON.stringify(relPath)}`, {
      cwd: ROOT,
      stdio: ['ignore', 'pipe', 'ignore'],
    })
      .toString()
      .trim();
    return out || null;
  } catch (e) {
    return null;
  }
}

function extractMeta(filePath, relPath) {
  let head = '';
  const fd = fs.openSync(filePath, 'r');
  try {
    const buf = Buffer.alloc(HEAD_BYTES);
    const n = fs.readSync(fd, buf, 0, HEAD_BYTES, 0);
    head = buf.slice(0, n).toString('utf8');
  } finally {
    fs.closeSync(fd, () => {});
  }

  const fileName = path.basename(filePath);

  // 标题
  let title = firstMatch(/<title[^>]*>([\s\S]*?)<\/title>/i, head);
  title = cleanText(title);
  if (!title) title = fileName.replace(/\.html?$/i, '');

  // 描述
  const description = cleanText(
    pickMeta(head, ['description', 'og:description']) || ''
  );

  // 标签
  const keywordsRaw = pickMeta(head, ['keywords', 'article:tag']);
  const tags = (keywordsRaw || '')
    .split(/[,，]/)
    .map((t) => cleanText(t))
    .filter(Boolean);

  // 日期
  let date =
    pickMeta(head, ['date', 'article:published_time']) ||
    firstMatch(/<time[^>]*datetime=["']([^"']+)["']/i, head);
  date = date ? cleanText(date) : gitFileDate(relPath);
  // 规整为 YYYY-MM-DD（保留原始字符串用于排序）
  let dateShort = date;
  if (date) {
    const d = new Date(date);
    if (!isNaN(d.getTime())) dateShort = d.toISOString().slice(0, 10);
  }

  return {
    file: fileName,
    url: 'assets/' + fileName,
    title,
    description,
    tags,
    date: dateShort || null,
  };
}

function main() {
  if (!fs.existsSync(ASSETS_DIR)) {
    fs.mkdirSync(ASSETS_DIR, { recursive: true });
  }

  const files = fs
    .readdirSync(ASSETS_DIR)
    .filter((f) => /\.html?$/i.test(f))
    .sort((a, b) => a.localeCompare(b));

  const posts = files.map((f) => {
    const fullPath = path.join(ASSETS_DIR, f);
    const relPath = 'assets/' + f;
    return extractMeta(fullPath, relPath);
  });

  // 按日期倒序，无日期的排到后面并按文件名
  posts.sort((a, b) => {
    if (a.date && b.date) return b.date.localeCompare(a.date);
    if (a.date) return -1;
    if (b.date) return 1;
    return a.file.localeCompare(b.file);
  });

  const manifest = {
    generatedAt: new Date().toISOString(),
    count: posts.length,
    posts,
  };

  fs.writeFileSync(OUT_FILE, JSON.stringify(manifest, null, 2), 'utf8');
  console.log(`✔ 已生成 manifest.json，共 ${posts.length} 篇文章`);
  posts.forEach((p) =>
    console.log(`   - ${p.date || '????-??-??'}  ${p.title}  (${p.file})`)
  );
}

main();
