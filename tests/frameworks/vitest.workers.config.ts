import { cloudflareTest } from '@cloudflare/vitest-plugin'
import { defineConfig } from 'vitest/config'

// The built package inside the Workers runtime, served by Cloudflare's agents.
export default defineConfig({
  plugins: [
    cloudflareTest({
      wrangler: { configPath: './wrangler.jsonc' },
      // The Worker's own fetch reaches a stand-in for the ingestion, so a client built without a
      // fetch option sends through Workers' fetch, as in production.
      miniflare: {
        outboundService: 'ingest',
        workers: [{ name: 'ingest', modules: true, compatibilityDate: '2026-10-01', scriptPath: './workers/ingest.js' }],
      },
    }),
  ],
  test: {
    name: 'workers',
    include: ['workers/**/*.test.ts'],
    // Vitest 5 caches transformed modules on disk by default; the Workers runtime can't read it.
    fsModuleCache: false,
  },
})
