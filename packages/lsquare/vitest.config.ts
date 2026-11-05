import { defineConfig } from 'vitest/config'

export default defineConfig({
  // --expose-gc lets the host-safety tests measure memory that the SDK must not retain.
  test: { execArgv: ['--expose-gc'] },
})
