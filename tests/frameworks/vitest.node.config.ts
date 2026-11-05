import { defineConfig } from 'vitest/config'

// The built package in Node, served by Vercel's mcp-handler.
export default defineConfig({
  test: {
    name: 'node',
    include: ['node/**/*.test.ts'],
  },
})
