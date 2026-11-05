// Values under secret key names in JSON held in a string. What they become is in the shared cases
// (packages/contract/cases/redaction.json); this is what those can't say: that the work is linear,
// that every span is sound on any input, and that @lsquare/node has the same scanner.
import { readFileSync } from 'node:fs'
import { describe, expect, it } from 'vitest'
import { Redactor, resolvePatterns } from '../src/redact.js'
import { isValidJson, SCAN_WORK, type ScanWork, secretSpans } from '../src/secret-spans.js'

const isSecret = (key: string) => key.toLowerCase().endsWith('password')
const unlimited = (): ScanWork => ({ left: Number.POSITIVE_INFINITY })

function time(text: string): number {
  let best = Number.POSITIVE_INFINITY
  for (let i = 0; i < 3; i++) {
    const started = performance.now()
    secretSpans(text, isSecret, unlimited())
    best = Math.min(best, performance.now() - started)
  }
  return best
}

/** Encodes `text` as a JSON string `times` times over, as a string inside a string would be. */
function encoded(text: string, times: number): string {
  let out = text
  for (let i = 0; i < times; i++) out = `{"a":${JSON.stringify(out)}}`
  return out
}

/** Texts and the work each takes: the same numbers are in secret_spans_test.py. */
const WORK_SAMPLES: [string, number][] = [
  ['{"password":"x"}', 4],
  ['{"a":"b","c":"d\\n"}', 14],
  ['{"a":"{\\"password\\":\\"x\\"}"}', 17],
  ['\'{\\"a\\": 1}\'', 14],
  ['plain "quoted": text', 3],
  ['"password":["a",{"b":1}]', 9],
  ['\\\\"x":\\\\"x":\\\\"x":', 39],
  ['"a\\q":1', 10],
  ['{"password":["xxxx"]}', 6],
  ['{"password":["\\\\\\\\"]}', 9],
  ['{"password":["' + '\\'.repeat(100) + '"]}', 9],
  ["{'password': 'x', 'a': [1, 'b']}", 4],
  ["{'password': (1, ['a'], {'b': 2})}", 12],
  ['https://x.io/?password=abc&a=1#b=2', 4],
  ['password\n  x [type=t, input_value=1, input_type=int]', 2],
  [`https://x.io/?${'a'.repeat(64)}=1`, 6],
  ['%%%%3D', 6],
]

const JSON_SAMPLES = [
  '',
  ' ',
  'null',
  'nul',
  'true',
  'truex',
  ' false ',
  '0',
  '-0',
  '01',
  '-',
  '1.',
  '1.5',
  '.5',
  '1e5',
  '1E+5',
  '1e',
  '+1',
  'NaN',
  'Infinity',
  '""',
  '"',
  '"a',
  '"\\"',
  '"\\\\"',
  '"\\n"',
  '"\\x"',
  '"\\u12"',
  '"\\u1234"',
  '"\\ud800"',
  '"\n"',
  '"\t"',
  '"\u007f"',
  '"\ud800"',
  '[]',
  '[ ]',
  '[1]',
  '[1,]',
  '[,1]',
  '[1 2]',
  '[1,2',
  '{}',
  '{ }',
  '{"a":1}',
  '{"a":1,}',
  '{"a"}',
  '{"a":}',
  '{a:1}',
  '{1:1}',
  '{"a":1 "b":2}',
  '{"a":1]',
  '[1}',
  '{}{}',
  '[] x',
  '\f[]',
  '\u00a0[]',
  '\ufeff[]',
  '[\n1\r,\t2 ]',
  `[${'1'.repeat(5000)}]`,
  '{"a":[{"b":[{"c":null}]}],"d":"e"}',
  '{a":1}',
  '"\u0000"',
  '"\u001f"',
  '"\\n\t"',
  '"\\uzzzz"',
  '"\\uABCD"',
  '"\\uabcd"',
  '"\\u123"',
  '"\\u\u0661\u0662\u0663\u0664"',
  '"\\/\\b\\f\\r\\t"',
  '"\\ "',
  '{]',
  '[}',
  '[[1] ]',
  '{"a"x1}',
  '{"a" :1}',
  '1e-5',
]

describe('secretSpans', () => {
  it.each([
    ['quotes', (n: number) => '"'.repeat(n)],
    ['backslashes', (n: number) => '\\'.repeat(n)],
    ['escaped quotes', (n: number) => '\\"'.repeat(n)],
    ['a quote, then backslashes', (n: number) => `"${'\\'.repeat(n)}`],
    ['one backslash at the end', (n: number) => `${'"a"'.repeat(n)}\\`],
    ['strings holding quotes', (n: number) => '"\\"",'.repeat(n)],
    ['keys', (n: number) => '"a":'.repeat(n)],
    ['secret keys', (n: number) => '"password":'.repeat(n)],
    ['secret values', (n: number) => '"password":"x",'.repeat(n)],
    ['open brackets under a secret key', (n: number) => `"password":${'['.repeat(n)}`],
    ['open brackets, many times', (n: number) => '"password":['.repeat(n)],
    ['a long payload encoded four times', (n: number) => encoded(`{"password":"${'x'.repeat(n)}"}`, 4)],
    ['a long payload encoded six times', (n: number) => encoded(`{"a":"${'x'.repeat(n)}"}`, 6)],
    ['printed keys', (n: number) => '\\\\"x":'.repeat(n)],
    ['keys full of escapes', (n: number) => '"\\\\\\\\\\\\\\\\x":'.repeat(n)],
    ['escapes surviving every decoding', (n: number) => `${'\\'.repeat(16 * n)}x`],
    ['commas after a quote', (n: number) => `"password":"${'",'.repeat(n)}`],
    ['keys after a question mark', (n: number) => `[${'1,'.repeat(n)}${'?"password":1,'.repeat(n / 100)}`],
    ['blank lines in a value holding a quote', (n: number) => `"password":"a" b${'\n'.repeat(n)}c`],
    ['long keys', (n: number) => `{"${'x'.repeat(n)}":1,"${'y'.repeat(n)}":2}`],
    ['quotes past the nesting limit', (n: number) => encoded(`"${' '.repeat(n)}`, 5)],
    ['backslashes past the nesting limit', (n: number) => encoded(`"${'\\'.repeat(n)}`, 5)],
    ['apostrophes', (size: number) => "'".repeat(size)],
    ['single-quoted keys', (size: number) => "'a': ".repeat(size)],
    ['single-quoted secret keys', (size: number) => "{'password': 'x', ".repeat(size)],
    ['open containers under them', (size: number) => "{'password': (".repeat(size)],
    ['open strings under them', (size: number) => "{'password': 'x ".repeat(size)],
    ['a run of apostrophes in a value', (size: number) => `{'password': '${"'".repeat(size)}x`],
    ['strings in a container over lines', (size: number) => `{'password': (${"\n\\'".repeat(size)}`],
    ['strings side by side', (size: number) => `{'password': ${"'a' ".repeat(size)}`],
    ['named containers', (size: number) => "{'password': a(".repeat(size)],
    ['placeholders that never close', (size: number) => '&password=[x&x=1'.repeat(size)],
    ['input values without a type', (size: number) => '[type=t, input_value=[], '.repeat(size)],
    ['open keys followed by a far space', (size: number) => `${"{'password':a".repeat(size)} x`],
    ['input values with a far type', (size: number) => `${'[type=t, input_value=x\n'.repeat(size)}, input_type=y]`],
    ['input values and types on one line', (size: number) => '[type=t, input_value=x, input_type=y] '.repeat(size)],
    ['percent-encoded equals signs', (size: number) => '%3D'.repeat(size)],
    ['percent-encoded parameters', (size: number) => '%3Fpassword%3Dx'.repeat(size)],
    ['equals signs', (size: number) => '='.repeat(size)],
    ['long parameter names', (size: number) => `${'a'.repeat(300)}=`.repeat(size >> 6)],
    ['parameters', (size: number) => '&a=1'.repeat(size)],
    ['input values', (size: number) => '[type=t, input_value='.repeat(size)],
    ['input values and types', (size: number) => '[type=t, input_value=x, input_type=y]\n'.repeat(size)],
    ['fields before input values', (size: number) => `password\n${' '.repeat(200)}[type=t, input_value=1, input_type=int] `.repeat(size >> 6)],
    ['input values with written line ends', (size: number) => '[type=t, input_value=x\\n'.repeat(size)],
    ['words that look like the end of an input value', (size: number) => `[type=t, input_value=x${', input_type=y'.repeat(size)}`],
    ['a run of backslashes in an input value', (size: number) => `[type=t, input_value=${'\\'.repeat(size)}`],
    ['names ending in list brackets', (size: number) => `&a${'[]'.repeat(120)}=`.repeat(size >> 8)],
    ['a run of percent signs', (size: number) => `${'%'.repeat(size)}%3D`],
    ['angle brackets', (size: number) => "{'password': <a ".repeat(size)],
    ['closed angle brackets', (size: number) => "{'password': <a>, ".repeat(size)],
    ['containers with their size', (size: number) => "{'password': Set(2) { 'a' }, ".repeat(size)],
    ['references', (size: number) => "{'password': <ref *1> ".repeat(size)],
    ['backticks', (size: number) => "{'password': `a ".repeat(size)],
    ['arrows after double quotes', (size: number) => '"password" => '.repeat(size)],
    ['escaped line ends in a container', (size: number) => `{'password': (${"a\\n'".repeat(size)}`],
    ['private key blocks under keys', (size: number) => "{'private_key': '-----BEGIN PRIVATE KEY-----\na\n-----END PRIVATE KEY-----\n', ".repeat(size >> 3)],
    ['private key blocks that never end', (size: number) => `{'private_key': '${'-----BEGIN PRIVATE KEY-----'.repeat(size >> 3)}`],
    ['open containers before a far escaped quote', (size: number) => `${",'token':[".repeat(size >> 3)}${"'\n".repeat(size >> 3)}\\"`],
    ['containers closed by a far escaped quote', (size: number) => `${",'token':[\\\"".repeat(size >> 3)}x\\"`],
    ['named containers under double-quoted keys', (size: number) => `{ "user's password": [Object: null prototype] { a: 1 } }, `.repeat(size >> 5)],
    ['lists in single quotes under double-quoted keys', (size: number) => `{"user's passwords": ['old]pass', 'x']}, `.repeat(size >> 5)],
    ['strings that a line ends, in a container', (size: number) => `{'password': (${"'\n".repeat(size)}`],
    ['containers read decoded, each closing at a line end', (size: number) => `{"a":"${"{'password': ('x\\n), ".repeat(size >> 3)}"}`],
    ['long container names', (size: number) => `${"{'password': ".repeat(size >> 5)}${'a'.repeat(size)}`],
    ['custom error types', (size: number) => '[type=aaaaaaaa'.repeat(size)],
    ['escapes before patterns', (size: number) => '\\u201c4111 '.repeat(size)],
  ])('is linear on %s', (_name, build) => {
    // A few tries: the heavier test files run alongside and can slow one measurement.
    let ratio = Number.POSITIVE_INFINITY
    for (let attempt = 0; attempt < 5 && ratio >= 10; attempt++) {
      ratio = Math.min(ratio, time(build(40_000)) / Math.max(time(build(10_000)), 0.5))
    }
    expect(ratio).toBeLessThan(10)
  })

  it('gives spans in order, not overlapping, inside the string, on any input', () => {
    const pieces = ['"', '\\', '\\"', '\\\\', '\\u0022', '\\n', ':', ',', '{', '}', '[', ']', 'password', '"password":', 'abc', '12', 'true', ' ', '\n', '😀']
    let seed = 7
    const next = (limit: number) => {
      seed = (seed * 1_103_515_245 + 12_345) % 2_147_483_648
      return seed % limit
    }
    for (let round = 0; round < 3000; round++) {
      const text = Array.from({ length: next(30) }, () => pieces[next(pieces.length)]).join('')
      let at = 0
      for (const span of secretSpans(text, isSecret, unlimited())) {
        expect(span.start, text).toBeGreaterThanOrEqual(at)
        expect(span.end, text).toBeGreaterThan(span.start)
        expect(span.end, text).toBeLessThanOrEqual(text.length)
        at = span.end
      }
    }
  })

  it('leaves a string with no key alone, and the same object comes back', () => {
    const redactor = new Redactor(resolvePatterns(undefined))
    for (const text of ['no quotes: here', '"no colon"', 'C:\\Users\\me', '{"a":"b"}']) expect(redactor.string(text)).toBe(text)
  })

  it('counts its work as the Python SDK does', () => {
    const used = WORK_SAMPLES.map(([text]) => {
      const work: ScanWork = { left: SCAN_WORK }
      secretSpans(text as string, isSecret, work)
      return SCAN_WORK - work.left
    })
    expect(used).toEqual(WORK_SAMPLES.map(([, count]) => count))
  })

  it('reads to the last unit of its work, and replaces the string whole past it', () => {
    expect(SCAN_WORK).toBe(1_000_000)
    for (const [text, count] of WORK_SAMPLES) {
      const read = secretSpans(text, isSecret, unlimited())
      expect(secretSpans(text, isSecret, { left: count })).toEqual(read)
      expect(secretSpans(text, isSecret, { left: count - 1 })).toEqual([{ start: 0, end: text.length, identity: undefined, quote: '' }])
    }
  })

  it('replaces a string whole once the event has used its work, and sends nothing unread', () => {
    const redactor = new Redactor(['secret'])
    const crafted = `":"{\\{${'\\'.repeat(7)}`.repeat(20_000)
    expect(redactor.string(crafted)).toBe('[SECRET_1]')
    expect(redactor.string('{"note":"not a secret"}')).toBe('[SECRET_2]')
    expect(redactor.string('no quote and colon here')).toBe('no quote and colon here')
    expect(redactor.counts).toEqual({ secret: 2 })
  })

  it('has work to spare on the largest ordinary event', () => {
    const value = (index: number) => `value "${index}"\n`
    const nested = JSON.stringify({ a: JSON.stringify(Object.fromEntries(Array.from({ length: 2000 }, (_, i) => [`k${i}`, value(i)]))) })
    const work: ScanWork = { left: SCAN_WORK }
    const times = Math.ceil(1_000_000 / nested.length)
    for (let i = 0; i < times; i++) secretSpans(nested, isSecret, work)
    expect(work.left).toBeGreaterThan(0)
  })

  it('is the same file in @lsquare/node', () => {
    const read = (path: string) => readFileSync(new URL(path, import.meta.url), 'utf8')
    expect(read('../../node/src/secret-spans.ts')).toBe(read('../src/secret-spans.ts'))
  })
})

describe('isValidJson', () => {
  const parses = (text: string) => {
    try {
      JSON.parse(text)
      return true
    } catch {
      return false
    }
  }

  it.each(JSON_SAMPLES)('agrees with JSON.parse on %j', (text) => {
    expect(isValidJson(text, unlimited())).toBe(parses(text))
  })

  it('agrees with JSON.parse on inputs nobody chose', () => {
    const pieces = ['{', '}', '[', ']', ':', ',', '"', '\\', '\\n', '\\u12', '\\u1234', 'a', '1', '-', '0', '.', 'e', 'E', '+', 'true', 'false', 'null', 'NaN', ' ', '\n', '\t', '\f', '\u0001', '\u007f', '\ud800', '😀', '""', '{}', '[]', '"a":', '"b"', '1e5', '01', '-0', '1.', '.5']
    let seed = 11
    const next = (limit: number) => {
      seed = (seed * 1_103_515_245 + 12_345) % 2_147_483_648
      return (seed >> 8) % limit
    }
    let valid = 0
    for (let round = 0; round < 40_000; round++) {
      const text = Array.from({ length: next(9) }, () => pieces[next(pieces.length)]).join('')
      expect(isValidJson(text, unlimited()), text).toBe(parses(text))
      if (parses(text)) valid++
    }
    expect(valid).toBeGreaterThan(500)
  })

  it('takes any depth', () => {
    expect(isValidJson(`${'['.repeat(200_000)}${']'.repeat(200_000)}`, unlimited())).toBe(true)
    expect(isValidJson(`${'{"a":'.repeat(100_000)}1${'}'.repeat(100_000)}`, unlimited())).toBe(true)
    expect(isValidJson('['.repeat(200_000), unlimited())).toBe(false)
  })
})
