// The smoke check (core.cjs) through the package's ESM build, as the ingestion imports it.
import { createRequire } from 'node:module'
import { fileURLToPath } from 'node:url'
import * as contract from '@lsquare/contract'

const smoke = createRequire(import.meta.url)('./core.cjs')
await smoke(contract, 'esm', fileURLToPath(import.meta.resolve('@lsquare/contract')))
