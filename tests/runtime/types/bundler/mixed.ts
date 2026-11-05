// A bundler with require(): its client from the CommonJS build, passed to the ESM wrapper.
import core = require('lsquare')
import { instrument } from 'lsquare/mcp'
instrument({}, new core.LSquare())
