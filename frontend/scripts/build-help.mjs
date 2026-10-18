import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import MarkdownIt from "markdown-it";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const rootDir = path.resolve(__dirname, "../..");
const docsDir = path.join(rootDir, "docs");
const outDir = path.resolve(__dirname, "../public/help");

const docMapping = [
  {
    src: "02-监控视频流远程接入与部署方案.md",
    dest: "remote-access.html",
    title: "监控视频流远程接入与部署方案",
  },
  {
    src: "01-摄像头AI工作台-PRD.md",
    dest: "01-prd.html",
    title: "摄像头 AI 工作台 PRD",
  },
  {
    src: "03-前端详细设计与交互规范.md",
    dest: "03-frontend.html",
    title: "前端详细设计与交互规范",
  },
  {
    src: "04-后端详细设计与接口契约.md",
    dest: "04-backend.html",
    title: "后端详细设计与接口契约",
  },
  {
    src: "05-后端实现与验收清单.md",
    dest: "05-backend-checklist.html",
    title: "后端实现与验收清单",
  },
  {
    src: "06-前端实现与验收清单.md",
    dest: "06-frontend-checklist.html",
    title: "前端实现与验收清单",
  },
];

function rewriteLink(href) {
  if (!href) return href;
  const [urlPart, hash] = href.split("#");
  const fragment = hash ? `#${hash}` : "";
  const base = path.basename(urlPart);
  for (const m of docMapping) {
    if (base === m.src) {
      return `${m.dest}${fragment}`;
    }
  }
  return href;
}

const md = new MarkdownIt({
  html: false,
  linkify: true,
  typographer: true,
});

const defaultLinkRender =
  md.renderer.rules.link_open ||
  function (tokens, idx, options, _env, self) {
    return self.renderToken(tokens, idx, options);
  };

md.renderer.rules.link_open = function (tokens, idx, options, env, self) {
  const token = tokens[idx];
  const hrefIndex = token.attrIndex("href");
  if (hrefIndex >= 0) {
    token.attrs[hrefIndex][1] = rewriteLink(token.attrs[hrefIndex][1]);
  }
  return defaultLinkRender(tokens, idx, options, env, self);
};

fs.mkdirSync(outDir, { recursive: true });

// Copy frontend-evidence images
const evidenceSrc = path.join(docsDir, "frontend-evidence");
const evidenceDest = path.join(outDir, "frontend-evidence");
if (fs.existsSync(evidenceSrc)) {
  fs.cpSync(evidenceSrc, evidenceDest, { recursive: true });
}

const css = `
body {
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif;
  color: #1f2328;
  background-color: #ffffff;
  line-height: 1.6;
  max-width: 960px;
  margin: 0 auto;
  padding: 32px 24px 64px 24px;
}
nav.doc-nav {
  display: flex;
  flex-wrap: wrap;
  gap: 12px;
  padding: 12px 16px;
  background: #f6f8fa;
  border: 1px solid #d0d7de;
  border-radius: 8px;
  margin-bottom: 32px;
  font-size: 14px;
}
nav.doc-nav a {
  color: #0969da;
  text-decoration: none;
}
nav.doc-nav a:hover {
  text-decoration: underline;
}
nav.doc-nav a.active {
  font-weight: 600;
  color: #1f2328;
}
h1, h2, h3, h4, h5, h6 {
  margin-top: 24px;
  margin-bottom: 16px;
  font-weight: 600;
  line-height: 1.25;
}
h1 { font-size: 2em; border-bottom: 1px solid #d0d7de; padding-bottom: 0.3em; }
h2 { font-size: 1.5em; border-bottom: 1px solid #d0d7de; padding-bottom: 0.3em; }
h3 { font-size: 1.25em; }
table {
  border-collapse: collapse;
  width: 100%;
  margin: 16px 0;
  display: block;
  overflow-x: auto;
}
th, td {
  border: 1px solid #d0d7de;
  padding: 6px 13px;
}
tr:nth-child(2n) {
  background-color: #f6f8fa;
}
pre {
  background-color: #f6f8fa;
  border-radius: 6px;
  padding: 16px;
  overflow-x: auto;
  font-size: 85%;
  line-height: 1.45;
  border: 1px solid #d0d7de;
}
code {
  font-family: ui-monospace, SFMono-Regular, "SF Mono", Menlo, Consolas, "Liberation Mono", monospace;
  background-color: rgba(175, 184, 193, 0.2);
  padding: 0.2em 0.4em;
  border-radius: 6px;
  font-size: 85%;
}
pre code {
  background-color: transparent;
  padding: 0;
  border-radius: 0;
  font-size: 100%;
}
blockquote {
  padding: 0 1em;
  color: #57606a;
  border-left: 0.25em solid #d0d7de;
  margin: 16px 0;
}
img {
  max-width: 100%;
  height: auto;
  border-radius: 6px;
  border: 1px solid #d0d7de;
}
a {
  color: #0969da;
  text-decoration: none;
}
a:hover {
  text-decoration: underline;
}
`;

function buildNav(currentDest) {
  return `<nav class="doc-nav" aria-label="帮助文档导航">
${docMapping
  .map(
    (d) =>
      `  <a href="${d.dest}" class="${d.dest === currentDest ? "active" : ""}">${d.title}</a>`,
  )
  .join("\n")}
</nav>`;
}

for (const doc of docMapping) {
  const fullSrc = path.join(docsDir, doc.src);
  if (!fs.existsSync(fullSrc)) {
    console.warn(`Warning: doc source file not found: ${fullSrc}`);
    continue;
  }
  const content = fs.readFileSync(fullSrc, "utf8");
  const rendered = md.render(content);
  const html = `<!DOCTYPE html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>${doc.title} - Camera AI Workbench 帮助文档</title>
  <style>${css}</style>
</head>
<body>
${buildNav(doc.dest)}
<main>
${rendered}
</main>
</body>
</html>
`;
  fs.writeFileSync(path.join(outDir, doc.dest), html, "utf8");
}

// Write index.html linking/redirecting to remote-access.html
const indexHtml = `<!DOCTYPE html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta http-equiv="refresh" content="0; url=remote-access.html">
  <title>帮助文档 - Camera AI Workbench</title>
  <style>${css}</style>
</head>
<body>
${buildNav("remote-access.html")}
<p>正在跳转到 <a href="remote-access.html">监控视频流远程接入与部署方案</a> ...</p>
</body>
</html>
`;
fs.writeFileSync(path.join(outDir, "index.html"), indexHtml, "utf8");

console.log("Help documentation generated successfully in frontend/public/help/");
