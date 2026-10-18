import { readFile } from 'node:fs/promises';
import { gzipSync } from 'node:zlib';
const root = new URL('../dist/', import.meta.url);
const manifest = JSON.parse(await readFile(new URL('.vite/manifest.json', root), 'utf8'));
const seen = new Set();
async function size(key) {
  if (seen.has(key)) return 0;
  seen.add(key);
  const chunk = manifest[key];
  return gzipSync(await readFile(new URL(chunk.file, root))).length + (await Promise.all((chunk.imports ?? []).map(size))).reduce((a,b) => a+b, 0);
}
const bytes = await size('index.html') + await size('src/features/overview/Overview.tsx');
console.log(`首屏 JS（含静态依赖）gzip ${(bytes / 1024).toFixed(1)} KiB / 300 KiB`);
if (bytes > 300 * 1024) process.exitCode = 1;
