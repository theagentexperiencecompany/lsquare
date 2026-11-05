// Prints the `phone` pattern's source: `node scripts/phone-pattern.mjs`. src/redact.ts and
// packages/python/src/lsquare/_redact.py hold what it prints, and tests check that they do.
//
// 0.1's international alternative is `\+[1-9]\d{0,2}[ .-]?\(?\d{1,4}\)?(?:[ .-]?\d{2,4}){2,4}\b`:
// a country code, an area code and two to four groups, each of them digits, with optional
// characters between them. Since nothing is required between two pieces, one run of digits can be
// split among them in hundreds of ways, and when the number then fails at its end, as a `+`
// followed by a dozen digits and a letter does, the engine tries every split before giving up.
//
// The rewrite matches a run of digits with one counted repeat, whichever pieces it holds: a branch
// for each piece the run can end at, then at least one of the characters that may follow that
// piece, then the next run. A repeat must be followed by something that isn't a digit, so it can
// only end where the run does: adjacent repeats can't split one run in more than one way, and a run
// is tried once for each piece it can end at instead of once for each split. The branches are in
// the order 0.1's backtracking reaches them, fewest pieces first and going on before ending, and
// the tests check that every match is the one 0.1's gives (test/v1-patterns.ts).

/** 0.1's separator. */
const SEPARATOR = ' .-'

/**
 * 0.1's pieces, in order: the digits each one takes, and what may follow it, each optional and in
 * this order. The country code is followed by `[ .-]?\(?`, the area code by `\)?` and the first
 * group's `[ .-]?`, and a group by the next one's `[ .-]?`.
 */
const COUNTRY = { digits: [1, 3], then: [SEPARATOR, '('] }
const AREA = { digits: [1, 4], then: [')', SEPARATOR] }
const GROUP = { digits: [2, 4], then: [SEPARATOR] }
const PIECES = [COUNTRY, AREA, GROUP, GROUP, GROUP, GROUP]
/** A number can end after its second group, or any piece after it. */
const FIRST_END = 3

/** 0.1's two US formats, as they are. */
const US = String.raw`\(\d{3}\) ?\d{3}-\d{4}\b|\b\d{3}-\d{3}-\d{4}\b`

const one = (chars) => (chars.length > 1 ? `[${chars}]` : `\\${chars}`)
const either = (options) => (options.length > 1 ? `(?:${options.join('|')})` : options[0])

/** What may follow a piece, at least one character of it: `[ .-]?\(?` gives `[ .-]\(?|\(`. */
function between(then) {
  return either(then.map((chars, i) => one(chars) + then.slice(i + 1).map((rest) => `${one(rest)}?`).join('')))
}

/** A run of digits that starts at piece `first`, and the rest of the number after it. */
function run(first) {
  const branches = []
  let fewest = 0
  let most = 0
  for (let last = first; last < PIECES.length; last++) {
    fewest += PIECES[last].digits[0]
    most += PIECES[last].digits[1]
    const next = []
    if (last + 1 < PIECES.length) next.push(between(PIECES[last].then) + run(last + 1))
    if (last >= FIRST_END) next.push('\\b')
    branches.push(`\\d{${fewest},${most}}` + either(next))
  }
  return either(branches)
}

/**
 * Every number has at least `digits` digits, the first 1 to 9 (0.1's `[1-9]`, which only this
 * lookahead checks), with at most `widest` of the characters that may follow a piece between two
 * of them. Checking that first makes a `+` that starts no number cheap.
 */
const digits = PIECES.slice(0, FIRST_END + 1).reduce((sum, piece) => sum + piece.digits[0], 0)
const widest = Math.max(...PIECES.map((piece) => piece.then.length))
const chars = [...new Set(PIECES.flatMap((piece) => [...piece.then.join('')]))]
const anyBetween = `[${chars.filter((char) => char !== '-').join('')}${chars.includes('-') ? '-' : ''}]`
const guard = `(?=[1-9](?:${anyBetween}{0,${widest}}\\d){${digits - 1}})`

process.stdout.write(String.raw`\+` + guard + run(0) + '|' + US + '\n')
