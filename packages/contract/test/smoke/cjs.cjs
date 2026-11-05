// The smoke check (core.cjs) through the package's CommonJS build, as a script requires it.
const contract = require('@lsquare/contract')

// Failed until the check finishes: a promise left pending with nothing to keep Node alive would
// otherwise end the process with 0, the check unrun.
process.exitCode = 1
require('./core.cjs')(contract, 'cjs', require.resolve('@lsquare/contract')).then(() => {
  process.exitCode = 0
})
