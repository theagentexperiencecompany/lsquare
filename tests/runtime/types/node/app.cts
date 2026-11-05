// A CommonJS app that loads one build with import(): its client to the ESM wrapper, and the ESM
// build's client to its own wrapper.
import { LSquare } from 'lsquare'
import { instrument } from 'lsquare/mcp'
export async function main(): Promise<void> {
  const esm = await import('lsquare/mcp')
  esm.instrument({}, new LSquare())
  const core = await import('lsquare')
  instrument({}, new core.LSquare())
}
