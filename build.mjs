/**
 * Production frontend build.
 *
 * The app is authored as one HTML file with its JSX in a <script type="text/plain">, which
 * the page transpiles in the browser with Babel on every load. That is a real cost on the
 * devices this service is for: ~600 KB of compiler shipped to a phone, then a full parse
 * and transform of the whole app before the first pixel — several seconds on a low-end
 * Android over a slow connection, in front of someone who may be in distress. It also
 * forces 'unsafe-eval' into the Content-Security-Policy.
 *
 * This transpiles the same source ahead of time with esbuild and emits:
 *   static/app.<hash>.js  — the compiled app, content-hashed and immutably cacheable
 *   static/app.js         — a stable copy, so the deploy preflight can check one path
 *   static/index.html     — the same document with Babel removed and the bundle linked
 *
 * raahat.html stays the single editable source; nothing is generated into it.
 *
 *   node build.mjs          build
 *   node build.mjs --watch  rebuild on change
 */
import { createHash } from "node:crypto";
import { readFileSync, writeFileSync, readdirSync, unlinkSync } from "node:fs";
import { join, dirname } from "node:path";
import { fileURLToPath } from "node:url";
import * as esbuild from "esbuild";

const ROOT = dirname(fileURLToPath(import.meta.url));
const SOURCE = join(ROOT, "raahat.html");
const STATIC = join(ROOT, "static");

const SOURCE_OPEN = '<script type="text/plain" id="app-source">';
const SOURCE_CLOSE = "</script>";

function extractAppSource(html) {
  const start = html.indexOf(SOURCE_OPEN);
  if (start === -1) throw new Error(`Could not find ${SOURCE_OPEN} in raahat.html`);
  const from = start + SOURCE_OPEN.length;
  const end = html.indexOf(SOURCE_CLOSE, from);
  if (end === -1) throw new Error("Unterminated app-source script block");
  return { jsx: html.slice(from, end), start, end: end + SOURCE_CLOSE.length };
}

async function build() {
  const html = readFileSync(SOURCE, "utf8");
  const { jsx, start, end } = extractAppSource(html);

  const result = await esbuild.transform(jsx, {
    loader: "jsx",
    // The classic runtime emits React.createElement against the vendored global React.
    // The automatic runtime would emit an ESM import that cannot resolve without a
    // module graph, which this deliberately does not have.
    jsx: "transform",
    jsxFactory: "React.createElement",
    jsxFragment: "React.Fragment",
    minify: true,
    target: ["es2019"],  // covers the Android WebView versions still in the field
    legalComments: "none",
  });
  if (result.warnings.length) {
    for (const warning of result.warnings) console.warn("warning:", warning.text);
  }

  const hash = createHash("sha256").update(result.code).digest("hex").slice(0, 12);
  const hashedName = `app.${hash}.js`;

  // Clear previous hashed bundles so old deploys do not accumulate in the static mount.
  for (const name of readdirSync(STATIC)) {
    if (/^app\.[0-9a-f]{12}\.js$/.test(name) && name !== hashedName) unlinkSync(join(STATIC, name));
  }
  writeFileSync(join(STATIC, hashedName), result.code);
  writeFileSync(join(STATIC, "app.js"), result.code);

  const document = html.slice(0, start)
    + `<script src="/static/${hashedName}" defer></script>`
    + html.slice(end);

  const productionHtml = document
    // Babel is the whole reason this build exists; the bootstrap that used it goes with it.
    .replace(/\s*<script src="\/static\/vendor\/babel\.min\.js"><\/script>/, "")
    .replace(/<script>\s*\/\/ Manually transform the JSX source[\s\S]*?<\/script>/, "");

  if (productionHtml.includes("babel")) {
    throw new Error("Babel reference survived the production HTML rewrite — check build.mjs");
  }
  writeFileSync(join(STATIC, "index.html"), productionHtml);

  const kb = (bytes) => `${(bytes / 1024).toFixed(1)} KB`;
  console.log(`built static/${hashedName} (${kb(Buffer.byteLength(result.code))}) `
    + `from ${kb(Buffer.byteLength(jsx))} of JSX; static/index.html written`);
}

await build();
if (process.argv.includes("--watch")) {
  const { watch } = await import("node:fs");
  let pending = null;
  watch(SOURCE, () => {
    clearTimeout(pending);
    pending = setTimeout(() => build().catch((error) => console.error(error.message)), 120);
  });
  console.log("watching raahat.html");
}
