// The smoke checks (core.cjs) through the package's CommonJS build, as an app requires it.
const { LSquare, SDK_VERSION } = require('@lsquare/node')

// Failed until the checks finish: a promise left pending with nothing to keep Node alive would
// otherwise end the process with 0, the checks unrun.
process.exitCode = 1
require('./core.cjs')(LSquare, `cjs ${SDK_VERSION}`, process.argv[2])
