// The smoke checks (core.cjs) through the package's ESM build, as an app imports it.
import { createRequire } from 'node:module'
import { LSquare, SDK_VERSION } from '@lsquare/node'

const smoke = createRequire(import.meta.url)('./core.cjs')
await smoke(LSquare, `esm ${SDK_VERSION}`, process.argv[2])
