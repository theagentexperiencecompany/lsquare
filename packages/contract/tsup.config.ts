import { defineConfig } from 'tsup'

export default defineConfig({
  entry: ['src/index.ts'],
  format: ['esm', 'cjs'],
  dts: true,
  // Node 20+ and Workers alike: nothing of Node's, and no dependencies to resolve.
  target: 'es2022',
  platform: 'neutral',
  // Not cleaned first: wrangler dev rebuilds it while other test files load it, and a clean would
  // leave them a moment with no dist. Its outputs keep their names, so nothing stale is left.
  clean: false,
})
