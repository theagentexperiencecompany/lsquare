// A client from the ESM build, passed to the CommonJS wrapper.
import { LSquare } from 'lsquare'
import mcp = require('lsquare/mcp')
mcp.instrument({}, new LSquare())
