// node10 ignores `exports`: lsquare/mcp's types come through typesVersions.
import { LSquare } from 'lsquare'
import { instrument, type InstrumentOptions } from 'lsquare/mcp'
const options: InstrumentOptions = { inputs: true }
instrument({}, new LSquare(), options)
// @ts-expect-error not a client
instrument({}, {})
