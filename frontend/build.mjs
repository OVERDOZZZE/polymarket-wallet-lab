// Bundles src/ into dist/ with esbuild (WebAssembly build: no native binaries, so Windows Application
// Control never blocks it). `node build.mjs` = production build, `node build.mjs --watch` = rebuild on save
// (refresh the browser at http://127.0.0.1:8000 - the Python server serves dist/).
import * as esbuild from "esbuild-wasm";
import { cpSync, mkdirSync, readFileSync, rmSync, writeFileSync } from "node:fs";

const watch = process.argv.includes("--watch");
rmSync("dist", { recursive: true, force: true });
mkdirSync("dist/assets", { recursive: true });

const options = {
  entryPoints: { app: "src/main.tsx" },
  bundle: true,
  outdir: "dist/assets",
  format: "esm",
  target: "es2022",
  jsx: "automatic",
  minify: !watch,
  sourcemap: watch ? "inline" : false,
  loader: { ".woff2": "file", ".woff": "file" },
  assetNames: "[name]-[hash]",
  define: { "process.env.NODE_ENV": JSON.stringify(watch ? "development" : "production") },
  logLevel: "info",
};

// public/ (manifest, app icons) is served from the site root
cpSync("public", "dist", { recursive: true });

// index.html: the dev entry tag is swapped for the built bundle; ?v= busts caches (a home-screen app can't hard-refresh)
const v = Date.now().toString(36);
const html = readFileSync("index.html", "utf8").replace(
  '<script type="module" src="/src/main.tsx"></script>',
  `<link rel="stylesheet" href="/assets/app.css?v=${v}" />\n    <script type="module" src="/assets/app.js?v=${v}"></script>`,
);
writeFileSync("dist/index.html", html);

if (watch) {
  const ctx = await esbuild.context(options);
  await ctx.watch();
  console.log("watching src/ ... (Ctrl+C to stop)");
} else {
  await esbuild.build(options);
}
