import { readFile, mkdir, writeFile } from "node:fs/promises";
const root = new URL("../", import.meta.url);
const schema = JSON.parse(
  await readFile(new URL("../backend/contracts/openapi.json", root), "utf8"),
);
const types = await readFile(
  new URL("../backend/contracts/api-types.ts", root),
  "utf8",
);
const operations = {};
for (const [path, methods] of Object.entries(schema.paths)) {
  for (const [method, op] of Object.entries(methods)) {
    if (!op.operationId) continue;
    operations[op.operationId] = {
      method: method.toUpperCase(),
      path,
      idempotent:
        op.parameters?.some(
          (p) => p.in === "header" && p.name === "Idempotency-Key",
        ) ?? false,
      versioned:
        op.parameters?.some(
          (p) => p.in === "header" && p.name === "If-Match",
        ) ?? false,
      binary: [
        "getPreviewMedia",
        "getSnapshot",
        "getEvidence",
        "getReferenceContent",
      ].includes(op.operationId),
    };
  }
}
const outputs = {
  "src/api/generated/types.ts": types.replace(/\r\n/g, "\n"),
  "src/api/generated/operations.ts":
    "// 自动生成：npm run generate；请勿手工编辑。\nexport const operations = " +
    JSON.stringify(operations, null, 2) +
    " as const;\n",
  "src/api/generated/fields.ts":
    "// 自动生成自 OpenAPI 字段约束；请勿手工编辑。\nexport const fields = " +
    JSON.stringify(
      Object.fromEntries(
        [
          "SettingsConfig",
          "ProviderWrite",
          "ProviderPricingConfig",
          "ProviderPricingWrite",
        ].map((k) => [
          k,
          schema.components.schemas[k].properties,
        ]),
      ),
      null,
      2,
    ) +
    " as const;\n",
  "src/test/fixtures/contracts.json": (
    await readFile(new URL("../backend/contracts/fixtures.json", root), "utf8")
  ).replace(/\r\n/g, "\n"),
};
for (const [path, body] of Object.entries(outputs)) {
  const url = new URL(path, root);
  if (process.argv.includes("--check")) {
    if ((await readFile(url, "utf8")).replace(/\r\n/g, "\n") !== body)
      throw new Error(`${path} 已过期，请运行 npm run generate`);
  } else {
    await mkdir(new URL(".", url), { recursive: true });
    await writeFile(url, body);
  }
}
console.log(
  `已校验 ${Object.keys(operations).length} 个 OpenAPI 操作及同源类型、样例。`,
);
