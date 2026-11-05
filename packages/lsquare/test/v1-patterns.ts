// 0.1's phone and ip patterns, which 0.2 rewrites, and the inputs their matches are compared on.
// Python's tests/v1_patterns.py has the same inputs. The patterns are copied rather than imported
// from packages/node, so that a 0.1.x release with the rewrite can't turn the comparison into one
// of the rewrite with itself.

export const V1 = {
  phone: /\+[1-9]\d{0,2}[ .-]?\(?\d{1,4}\)?(?:[ .-]?\d{2,4}){2,4}\b|\(\d{3}\) ?\d{3}-\d{4}\b|\b\d{3}-\d{3}-\d{4}\b/g,
  ip: /\b(?:25[0-5]|2[0-4]\d|1?\d?\d)(?:\.(?:25[0-5]|2[0-4]\d|1?\d?\d)){3}\b|(?<![0-9A-Za-z:.])(?:(?:[0-9A-Fa-f]{1,4}:){7}[0-9A-Fa-f]{1,4}|(?:[0-9A-Fa-f]{1,4}:){1,7}:|(?:[0-9A-Fa-f]{1,4}:){1,6}:[0-9A-Fa-f]{1,4}|(?:[0-9A-Fa-f]{1,4}:){1,5}(?::[0-9A-Fa-f]{1,4}){1,2}|(?:[0-9A-Fa-f]{1,4}:){1,4}(?::[0-9A-Fa-f]{1,4}){1,3}|(?:[0-9A-Fa-f]{1,4}:){1,3}(?::[0-9A-Fa-f]{1,4}){1,4}|(?:[0-9A-Fa-f]{1,4}:){1,2}(?::[0-9A-Fa-f]{1,4}){1,5}|[0-9A-Fa-f]{1,4}:(?::[0-9A-Fa-f]{1,4}){1,6}|:(?::[0-9A-Fa-f]{1,4}){1,7})(?![0-9A-Za-z:]|\.\d)/g,
}

/** The digits 0.1's international phone pieces take: country code, area code, four groups. */
const FEWEST = [1, 1, 2, 2, 2, 2]
const MOST = [3, 4, 4, 4, 4, 4]
/**
 * Characters between two runs of digits, and the pieces each may follow. `.` and `-` match as ` `
 * does, and EXPLICIT has a few.
 */
const BETWEEN: Record<string, number[]> = { ' ': [0, 1, 2, 3, 4], '(': [0], ' (': [0], ')': [1], ') ': [1] }

/**
 * Phone numbers: `text`, then a run of digits that starts at one of the pieces `starts`, and so on
 * up to six runs (`runs` counts this one), each after a character that may follow a piece the one
 * before can end at. Each run is any length up to one more than the pieces left can take, and each
 * string ends after a run, or after a run and a letter.
 */
function* numbers(text: string, starts: number[], runs: number): Generator<string> {
  const longest = Math.max(...starts.map((start) => MOST.slice(start).reduce((sum, most) => sum + most, 0)))
  for (let length = 1; length <= longest + 1; length++) {
    const run = text + '1'.repeat(length)
    yield run
    yield run + 'a'
    if (runs === 6) continue
    const ends = new Set<number>()
    for (const start of starts) {
      let fewest = 0
      let most = 0
      for (let end = start; end < MOST.length; end++) {
        fewest += FEWEST[end] as number
        most += MOST[end] as number
        if (length >= fewest && length <= most) ends.add(end)
      }
    }
    for (const [between, after] of Object.entries(BETWEEN)) {
      const next = [...ends].filter((end) => end < MOST.length - 1 && after.includes(end)).map((end) => end + 1)
      if (next.length > 0) yield* numbers(run + between, next, runs + 1)
    }
  }
}

/** Every string of `length` characters from `alphabet`. */
function* words(alphabet: string, length: number): Generator<string> {
  if (length === 0) {
    yield ''
    return
  }
  for (const char of alphabet) for (const rest of words(alphabet, length - 1)) yield char + rest
}

/** Forms the generated inputs leave out: other separators, zeros, and what can border a number. */
const EXPLICIT = [
  '+44 20 7946 0958', '+44.20.7946.0958', '+44-20-7946-0958', '+1-555-123-4567', '+1.555.123.4567',
  '+1 (555) 123-4567', '+1-(555)-123-4567', '+1.(555).123.4567', '+1 555--123 4567', '+1..555.123.4567',
  '+1-555-123-4567-', '+1.555.123.4567.', '+1 555 123 4567-8', '+1 (555)-123.4567', '+1(555)123-4567',
  '+44 (0)20 7946 0958', '+44 020 7946 0958', '+0 20 7946 0958', '+01 234 5678', '+1 000 000 0000', '+100000000000',
  '+1_555_123_4567', '_+15551234567', '+15551234567_', '+1 555 123_4567', 'a+15551234567', '1+15551234567',
  '+44 20 7946 0958é', 'é+44 20 7946 0958', '+44 20 7946 0958٣', '+٤٤ 20 7946 0958',
  '+44 20 7946 ٠9 58', '+44 20 7946 0958\u{1F600}', '+44 20 7946 0958', '+44​20 7946 0958',
  '(555) 123-4567', '(555)123-4567', '555-123-4567', '1555-123-4567', '555-123-45678', '(555) 123-4567_', '_555-123-4567',
  '192.168.1.20', '192.168.01.1', '192.168.001.001', '010.001.000.001', '00.0.0.0', '0.0.0.0', '255.255.255.255',
  '1.2.3.4.', '1.2.3.4-5', '10.0.0.1.5', '10.0.0.1_', '_10.0.0.1', '10.0.0.1é', 'é10.0.0.1', '10.0.0.١',
  '١٠.0.0.1', '10-0-0-1', '10.0.0.1-', '-10.0.0.1', '10.0.0.1:8080', 'v1.2.3.4', '1.2.3.4a',
]

/** The inputs: phone numbers, every short string after a `+`, IPv4 addresses, and EXPLICIT. */
export function* inputs(): Generator<string> {
  yield* numbers('+', [0], 1)
  for (let length = 0; length <= 5; length++) for (const word of words('10 ()a#', length)) yield '+' + word
  const octets = ['0', '1', '01', '11', '19', '25', '99', '100', '199', '200', '249', '250', '255', '256', '300', '1111']
  for (const a of octets) {
    for (const b of octets) {
      for (const end of ['', 'a', ' ', '.', '.1', ':']) {
        yield `${a}.${b}.${b}.${a}${end}`
        yield ` ${a}.${a}.${b}.${b}.${a}${end}`
      }
    }
  }
  yield* EXPLICIT
}

const spans = (pattern: RegExp, text: string) => [...text.matchAll(pattern)].map((match) => `${match.index}+${match[0].length}`).join()

/** The first input on which the two patterns' matches differ, if any. */
export function firstDifference(pattern: RegExp, reference: RegExp): string | undefined {
  for (const text of inputs()) if (spans(pattern, text) !== spans(reference, text)) return text
  return undefined
}
