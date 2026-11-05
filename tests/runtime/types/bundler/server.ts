// A bundler: import takes the ESM build.
import { LSquare } from 'lsquare'
import { instrument, type InstrumentOptions } from 'lsquare/mcp'
const options: InstrumentOptions = { inputs: true }
instrument({}, new LSquare(), options)
// @ts-expect-error not a client
instrument({}, {})
