import { readFile, writeFile, mkdir } from "node:fs/promises";
const repo = new URL("../../", import.meta.url);
const stages = [];
for (let stage = 1; stage <= 6; stage++) {
  const path = `output/playwright/production-stage${stage}.log`;
  const log = await readFile(new URL(path, repo), "utf8");
  const match = log.match(
    /### Result\r?\n([\s\S]*?)\r?\n### Ran Playwright code/,
  );
  if (!match) throw new Error(`第 ${stage} 组没有成功结果：${path}`);
  const result = JSON.parse(match[1]);
  if (!Array.isArray(result.checks) || result.error || result.errors?.length)
    throw new Error(`第 ${stage} 组存在失败`);
  stages.push({ stage, checks: result.checks });
}
const folder = new URL("docs/frontend-evidence/", repo);
await mkdir(folder, { recursive: true });
const generatedAt =
  new Intl.DateTimeFormat("sv-SE", {
    timeZone: "Asia/Shanghai",
    dateStyle: "short",
    timeStyle: "medium",
  }).format(new Date()) + " +08:00";
const output = {
  generated_at_beijing: generatedAt,
  environment:
    "生产 dist + 本机真实 FastAPI + 合成 RTSP + 本地合成模型响应；Chromium 手机尺寸仿真，不等同真实手机或外部模型验证",
  checks_count: stages.reduce((n, s) => n + s.checks.length, 0),
  stages,
};
await writeFile(
  new URL("browser-results.json", folder),
  JSON.stringify(output, null, 2) + "\n",
);
console.log(
  JSON.stringify({
    checks: output.checks_count,
    stages: stages.length,
    generatedAt,
  }),
);
