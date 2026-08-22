/// <reference types="vitest/config" />
import { resolve } from "node:path";

import { defineConfig } from "vitest/config";

const REPO = resolve(import.meta.dirname, "../..");

export default defineConfig({
  root: import.meta.dirname,

  // No dev server, ever. Django serves the island from STATIC_URL in every
  // environment and `npm run watch:js` is the dev loop, so the CSP is identical
  // in development and production. An HMR client would want a websocket in
  // connect-src and an inline script in script-src, and config/settings/base.py
  // grants neither.
  appType: "custom",

  // Vite's built-in esbuild JSX transform. @vitejs/plugin-react exists for Fast
  // Refresh, which needs the dev server we do not have, and it would drag the
  // @babel/core tree into a lockfile that has to keep `npm audit
  // --audit-level=low` green — and npm audit has no waiver mechanism.
  esbuild: { jsx: "automatic" },

  resolve: {
    alias: {
      // ROADMAP contract 2, consumed at build time. The artefact is generated
      // but committed (see the Makefile), so this build needs no Python;
      // `make schema` regenerates it and apps/flows/tests/test_export.py fails
      // when the committed copy is stale, so the inlined copy cannot drift.
      "@flow-schema": resolve(REPO, "static/flows/flow-schema.json"),
    },
  },

  // 43 KB of schema: JSON.parse of one string literal parses faster and
  // minifies smaller than an object literal. It also disables named exports,
  // which is why src/schema/artifact.ts uses the default import only.
  json: { stringify: true },

  build: {
    // NOT named `dist`: .gitignore carries a bare `dist/` under "Distribution /
    // packaging", which would match Vite's default outDir at any depth and hide
    // the bundle with no entry explaining why. Inside an app's static/ dir for
    // the same reason `theme` is an installed app — that is what puts a
    // gitignored compiled artefact in front of the app-directories finder.
    outDir: resolve(REPO, "apps/flows/static/flows/builder"),
    // outDir is outside `root`; Vite refuses to empty it without this.
    emptyOutDir: true,

    // Django rewrites `//# sourceMappingURL=` in every .js it collects (see the
    // note in scripts/vendor-js.mjs). Here the .map would exist, so it is not
    // that script's hard failure — the cost is megabytes of first-party source
    // published under /static/ in every image. Not "hidden" either: that still
    // emits and collects the map.
    sourcemap: false,

    cssCodeSplit: false,
    modulePreload: false,
    manifest: false,

    rollupOptions: {
      input: { builder: resolve(import.meta.dirname, "src/main.tsx") },
      output: {
        format: "es",
        // ONE .js file. django.contrib.staticfiles.storage.HashedFilesMixin
        // sets support_js_module_import_aggregation = False, and neither
        // ManifestStaticFilesStorage nor whitenoise's subclass turns it on, so
        // the only *.js pattern applied at collectstatic is sourceMappingURL: a
        // relative `import "./chunk-Xyz.js"` is left verbatim. It resolves today
        // only because collectstatic leaves the un-hashed original beside the
        // hashed copy — which stops being true under
        // WHITENOISE_KEEP_ONLY_HASHED_FILES — and in the meantime the largest
        // part of the bundle silently loses far-future caching.
        inlineDynamicImports: true,
        // Stable names, no content hash: Django hashes at collectstatic, and a
        // Vite hash nested inside that one would make the filename unknowable
        // to apps/flows/templatetags/flow_builder.py without a second manifest.
        entryFileNames: "[name].js",
        chunkFileNames: "[name].js",
        // A function, not "[name][extname]": the only asset is React Flow's
        // stylesheet, and that pattern names the output after the *source*
        // file, so it lands as `style.css`. The template tag looks for
        // `builder.css`, and a <link> pointing at nothing is a silently
        // unstyled canvas rather than a build failure.
        assetFileNames: (asset) =>
          asset.names?.some((name) => name.endsWith(".css")) ? "builder.css" : "[name][extname]",
      },
    },
  },

  test: {
    environment: "jsdom",
    globals: true,
    setupFiles: [resolve(import.meta.dirname, "vitest.setup.ts")],
    include: ["src/**/*.test.{ts,tsx}"],
    restoreMocks: true,
  },
});
