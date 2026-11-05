import { defineConfig } from 'tsup'

export default defineConfig({
  // Each entry self-contained: lsquare/mcp reaches the client through it, never by sharing code.
  entry: { index: 'src/index.ts', mcp: 'src/mcp/index.ts' },
  format: ['esm', 'cjs'],
  splitting: false,
  dts: true,
  target: 'node18',
  platform: 'node',
  clean: true,
})
