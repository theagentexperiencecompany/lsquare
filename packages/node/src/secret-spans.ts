/**
 * Finds the values under secret key names in JSON held in a string: a text block, an error quoting
 * a response, a stringified argument. They are found by scanning, not by parsing the string: it may
 * be cut, or be JSON inside prose or a code fence, and rewriting it would change what isn't a secret.
 * Only the spans of the values are found; everything else stays as it was.
 *
 * Runs on the host app's event loop, on strings the limits already bound, so its work is linear:
 * each piece of text between two unescaped quotes is read a fixed number of times per level of
 * nesting, and nesting stops at `MAX_NESTING`. Written to match `_secret_spans.py`, on UTF-16 code units in both.
 */

/** A value to replace. `start` and `end` are positions in the string given. */
export interface SecretSpan {
  start: number
  end: number
  /** What makes it "the same value" for numbering. Undefined: it couldn't be read whole, so it is numbered on its own. */
  identity: string | undefined
  /** What goes either side of its placeholder: nothing inside a string's quotes, otherwise a quote at its level of nesting. */
  quote: string
}

/** How many times a string may hold JSON in a string. Past this, a string still holding a key is replaced whole. */
const MAX_NESTING = 4
const WHOLE_PLACEHOLDER = /^\[(?:EMAIL|PHONE|CARD|SECRET|IP)_\d{1,9}\]$/
/** The longest whole placeholder: `[SECRET_` + 9 digits + `]`. */
const LONGEST_PLACEHOLDER = 18
const MAX_CODE_POINT = 0x10ffff
const HEX4 = /^[0-9A-Fa-f]{4}$/
const JSON_NUMBER = /-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?(?:[eE][+-]?[0-9]+)?/y
const LITERALS = ['true', 'false', 'null']
/** What text ending in isn't a key, unless it starts an entry of an object: it is code or a sentence before a string, as in `token == ":"` or `createToken(":", a)`. */
const NOT_A_KEY_END = ' \t=([+?,.<|&'
/** What a sentence or code can have after a closing quote, before a space or a line's end. */
const SENTENCE_ENDS = '.;)\'`'
/** A name is matched by its end, so only this much of a long key is read. */
const KEY_READ = 256
/** The escapes printed text and YAML have that JSON doesn't, besides those that take digits. */
const OTHER_ESCAPES: Record<string, string> = { ' ': ' ', '0': '\0', a: '\x07', e: '\x1b', v: '\v', N: '\x85', _: '\xa0', L: '\u2028', P: '\u2029' }
/** A key at the nesting limit: a quote, whitespace and a colon, each as it is or encoded any number of times. */
const ENCODED_KEY = /(?:"|\\u0022)(?:[ \t\r\n]|\\+[nrt]|\\+u00(?:20|09|0[aAdD]))*(?::|\\+u003[aA])/

const TAB = 9
const LINE_FEED = 10
const CARRIAGE_RETURN = 13
const SPACE = 32
const QUOTE = 34
const COMMA = 44
const MINUS = 45
const PERIOD = 46
const COLON = 58
const EQUALS = 61
const GREATER = 62
const QUESTION = 63
const OPEN_BRACKET = 91
const BACKSLASH = 92
const CLOSE_BRACKET = 93
const OPEN_BRACE = 123
const CLOSE_BRACE = 125

/**
 * How much scanning one event may do: one for each quote, backslash or bracket read, each JSON value
 * checked, and each 16 characters decoded. So a crafted string can't hold up the host app's event
 * loop, where ordinary text never comes near it. Past this, a string holding a quote and a colon is
 * replaced whole, as nothing unread may be sent.
 */
export const SCAN_WORK = 1_000_000

/** What's left of `SCAN_WORK` for an event. */
export interface ScanWork {
  left: number
}

const OVER_WORK = new Error('scan work used up')

/** What one call of `secretSpans` shares. */
interface Reading {
  isSecretKey: (key: string) => boolean
  spans: SecretSpan[]
  work: ScanWork
}

/** A string to scan, and where each of its positions is in the string given. The string itself has no map. */
interface View {
  text: string
  map: number[] | undefined
}

/** Whether a string could hold a key and a value at all: the cheap check before scanning it. It needs a quote and a colon, as they are or encoded any number of times. */
export function mayHoldKey(text: string): boolean {
  return (text.includes('"') || text.includes('u0022')) && (text.includes(':') || text.includes('u003a') || text.includes('u003A'))
}

/** The values under secret key names in `text`, in order and not overlapping. */
export function secretSpans(text: string, isSecretKey: (key: string) => boolean, work: ScanWork): SecretSpan[] {
  const reading: Reading = { isSecretKey, spans: [], work }
  try {
    read({ text, map: undefined }, 0, false, reading)
  } catch (error) {
    if (error !== OVER_WORK) throw error
    return [{ start: 0, end: text.length, identity: undefined, quote: '' }]
  }
  return merged(reading.spans)
}

function spend(work: ScanWork, amount = 1): void {
  work.left -= amount
  if (work.left < 0) throw OVER_WORK
}

/**
 * Python's repr and Node's util.inspect double JSON's backslashes and leave its quotes, so `\\"` reads
 * as the end of a string. Decoded once, it's JSON again. So a string that isn't JSON and holds a
 * backslash is read a second time decoded, once for each chain of nesting, which keeps the work
 * linear. JSON as it is doesn't need that, and could come out wrong: its `\\"` does end a string.
 */
function read(view: View, depth: number, printed: boolean, reading: Reading): void {
  scan(view, depth, printed, reading)
  if (printed || depth === MAX_NESTING || !view.text.includes('\\') || isValidJson(view.text, reading.work)) return
  const decoded = decode(view, 0, view.text.length, reading.work)
  if (decoded.text.length < view.text.length) read(decoded, depth + 1, true, reading)
}

/**
 * Whether the text is one JSON value. Checked here, not by `JSON.parse`, so that the two languages
 * agree at any depth, and text that isn't JSON costs no thrown error.
 */
export function isValidJson(text: string, work: ScanWork): boolean {
  const open: number[] = []
  let index: number | undefined = skipSpace(text, 0)
  for (;;) {
    spend(work)
    const unit = text.charCodeAt(index)
    let end: number | undefined
    if (unit === OPEN_BRACE || unit === OPEN_BRACKET) {
      const inner = skipSpace(text, index + 1)
      if (text.charCodeAt(inner) !== unit + 2) {
        open.push(unit)
        index = unit === OPEN_BRACE ? jsonKeyEnd(text, inner) : inner
        if (index === undefined) return false
        continue
      }
      end = inner + 1
    } else {
      end = jsonScalarEnd(text, index)
    }
    if (end === undefined) return false
    index = skipSpace(text, end)
    while (open.length > 0 && text.charCodeAt(index) === (open[open.length - 1] as number) + 2) {
      open.pop()
      index = skipSpace(text, index + 1)
    }
    if (open.length === 0) return index === text.length
    if (text.charCodeAt(index) !== COMMA) return false
    index = skipSpace(text, index + 1)
    if (open[open.length - 1] === OPEN_BRACE) index = jsonKeyEnd(text, index)
    if (index === undefined) return false
  }
}

/** Where the value after the key at `at` starts. */
function jsonKeyEnd(text: string, at: number): number | undefined {
  if (text.charCodeAt(at) !== QUOTE) return undefined
  const end = jsonStringEnd(text, at + 1)
  if (end === undefined) return undefined
  const colon = skipSpace(text, end)
  return text.charCodeAt(colon) === COLON ? skipSpace(text, colon + 1) : undefined
}

function jsonScalarEnd(text: string, at: number): number | undefined {
  if (text.charCodeAt(at) === QUOTE) return jsonStringEnd(text, at + 1)
  for (const literal of LITERALS) {
    if (text.startsWith(literal, at)) return at + literal.length
  }
  const end = jsonNumberEnd(text, at)
  return end > at ? end : undefined
}

function jsonStringEnd(text: string, from: number): number | undefined {
  for (let index = from; index < text.length; index++) {
    const unit = text.charCodeAt(index)
    if (unit === QUOTE) return index + 1
    if (unit < SPACE) return undefined
    if (unit === BACKSLASH) {
      index++
      if (text.charAt(index) === 'u') {
        if (!HEX4.test(text.slice(index + 1, index + 5))) return undefined
        index += 4
      } else if (escapedChar(text.charAt(index)) === undefined) {
        return undefined
      }
    }
  }
  return undefined
}

function jsonNumberEnd(text: string, at: number): number {
  JSON_NUMBER.lastIndex = at
  return JSON_NUMBER.test(text) ? JSON_NUMBER.lastIndex : at
}

function merged(spans: SecretSpan[]): SecretSpan[] {
  spans.sort((a, b) => a.start - b.start || b.end - a.end)
  const out: SecretSpan[] = []
  for (const span of spans) {
    const last = out[out.length - 1]
    if (last === undefined || span.start >= last.end) out.push(span)
    else if (span.end > last.end) out[out.length - 1] = { start: last.start, end: span.end, identity: undefined, quote: last.quote }
  }
  return out
}

/**
 * Every piece of text before an unescaped quote is a candidate, whether it really is a string or
 * lies between two of them. A key is one that a colon follows. So a stray quote in prose can't put
 * the scan out of step, and a value holding quotes needs no special case.
 */
function scan(view: View, depth: number, printed: boolean, reading: Reading): void {
  const { text } = view
  let start = 0
  for (;;) {
    spend(reading.work)
    const { end, escaped } = lex(text, start)
    // A key is read too: hiding a value in JSON used as a key is worth two such keys coming out the same.
    if (escaped) nest(view, start, end, depth, printed, reading)
    if (end >= text.length) return
    const colon = skipSpace(text, end + 1)
    if (text.charCodeAt(colon) === COLON && isKey(view, start, end, colon, escaped, reading)) {
      const next = readValue(view, skipSpace(text, colon + 1), printed ? depth - 1 : depth, startsEntry(text, start), reading)
      if (next === undefined) return
      start = next
    } else {
      start = end + 1
    }
  }
}

/**
 * A key with a secret key name. What's before a quote can be code or a sentence, as in
 * `cond ? "token" : "x"`, but not where an object has a key: there, JSON's keys are read as they are.
 */
function isKey(view: View, start: number, end: number, colon: number, escaped: boolean, reading: Reading): boolean {
  const key = body(view, start, end, escaped, reading.work)
  if (!reading.isSecretKey(key.length > KEY_READ ? key.slice(-KEY_READ) : key)) return false
  const { text } = view
  const before = beforeKey(text, start)
  const unit = text.charCodeAt(before)
  if (unit === QUESTION && (before === start - 2 || colon > end + 1)) return false
  if (!NOT_A_KEY_END.includes(text.charAt(end - 1)) || unit === OPEN_BRACE || unit === COMMA || unit === LINE_FEED || unit === CARRIAGE_RETURN) return true
  return before < 0 && text.charCodeAt(start - 1) === QUOTE
}

/**
 * Whether a value that isn't a quoted string can follow this key. Not when the key comes straight
 * after a word or an operator, where it is code or a sentence quoting something: `case "token":
 * return x`, `x == "token": y`, `"users"."token"::text`. A line number, a timestamp or a colour code
 * isn't a word: a word is two letters.
 */
function startsEntry(text: string, start: number): boolean {
  const index = beforeKey(text, start)
  const unit = text.charCodeAt(index)
  const previous = text.charCodeAt(index - 1)
  if (unit === EQUALS || (isLetter(unit) && isLetter(previous))) return false
  // `-> "Token":`, not a quoted line's `> "token":`.
  if (unit === GREATER) return previous !== MINUS && previous !== EQUALS
  // `Failed. "token":` and `"users"."token":`, not a list's `1. "token":` or an elided `..."token":`.
  return unit !== PERIOD || !(isLetter(previous) || previous === QUOTE)
}

/** Where what's before the quote opening the piece at `start` is, past its escapes, spaces and tabs. -1: nothing is, or no quote opens it, as in a string cut in its key. */
function beforeKey(text: string, start: number): number {
  if (text.charCodeAt(start - 1) !== QUOTE) return -1
  let index = start - 2
  while (index >= 0 && (text.charCodeAt(index) === SPACE || text.charCodeAt(index) === TAB || text.charCodeAt(index) === BACKSLASH)) index--
  return index
}

/** JSON in a string: its quotes, colons and spaces are all encoded, so it is scanned decoded. */
function nest(view: View, start: number, end: number, depth: number, printed: boolean, reading: Reading): void {
  const inner = decode(view, start, end, reading.work)
  // With nothing decoded, it holds no more than it shows: a Windows path, a regular expression.
  if (inner.text.length === end - start || !mayHoldKey(inner.text)) return
  if (depth < MAX_NESTING) read(inner, depth + 1, printed, reading)
  else if (ENCODED_KEY.test(inner.text)) reading.spans.push({ start: position(view, start), end: position(view, end), identity: undefined, quote: '' })
}

/** Adds the span of the value at `at`, if it can hold a secret. Where scanning goes on; undefined: it ran to the end of the text. */
function readValue(view: View, at: number, level: number, entry: boolean, reading: Reading): number | undefined {
  const { text } = view
  const { spans, work } = reading
  if (at >= text.length) return at
  const first = text.charCodeAt(at)
  if (first === QUOTE) {
    // Replaced already, whatever follows it: a hook's event is read a second time.
    const quoteAt = lex(text, at + 1).end
    const whole = lineEnd(text, at + 1, quoteAt) ?? quoteAt
    if (isPlaceholder(view, at + 1, whole)) return whole < text.length ? whole + 1 : undefined
    const { end, escaped } = valueEnd(text, at + 1, false, work)
    const closed = end < text.length
    if (end > at + 1) {
      spans.push({
        start: position(view, at + 1),
        end: position(view, end),
        identity: closed ? body(view, at + 1, end, escaped, work) : undefined,
        quote: '',
      })
    }
    return closed ? end + 1 : undefined
  }
  // Only a quoted string is a value wherever its key is: not `Post "https://x/token": timeout`, or `case "token": return x`.
  if (!entry) return at
  const quote = '\\'.repeat(2 ** level - 1) + '"'
  if (first === OPEN_BRACE || first === OPEN_BRACKET) {
    const close = containerEnd(text, at, work)
    if (close === undefined) {
      spans.push({ start: position(view, at), end: position(view, text.length), identity: undefined, quote })
      return undefined
    }
    if (endsToken(text, close)) {
      if (skipSpace(text, at + 1) === close - 1) return close
      spans.push({ start: position(view, at), end: position(view, close), identity: text.slice(at, close), quote })
      return close
    }
    return bareValue(view, at, close, quote, spans)
  }
  for (const literal of LITERALS) {
    if (text.startsWith(literal, at) && endsToken(text, at + literal.length)) return at + literal.length
  }
  const number = jsonNumberEnd(text, at)
  if (number > at && endsToken(text, number)) {
    spans.push({ start: position(view, at), end: position(view, number), identity: text.slice(at, number), quote })
    return number
  }
  return bareValue(view, at, at, quote, spans)
}

/** A value that isn't JSON, from `at`, read on from `from` up to the end of its line or the next comma or closing bracket. */
function bareValue(view: View, at: number, from: number, quote: string, spans: SecretSpan[]): number {
  const { text } = view
  let next = from
  while (next < text.length && !endsBare(text.charCodeAt(next))) next++
  let end = next
  while (end > at && isSpace(text.charCodeAt(end - 1))) end--
  if (end > at) spans.push({ start: position(view, at), end: position(view, end), identity: text.slice(at, end), quote })
  return next
}

/**
 * Where a string value ends: at an unescaped quote that something able to follow a string follows.
 * So a value with a quote left unescaped in it, as hand-built JSON has, is read whole, up to a line
 * that starts as JSON's do.
 */
function valueEnd(text: string, from: number, inContainer: boolean, work: ScanWork): { end: number; escaped: boolean } {
  let escaped = false
  let index = from
  for (;;) {
    spend(work)
    const piece = lex(text, index)
    escaped ||= piece.escaped
    const line = index > from ? entryLineEnd(text, index, piece.end) : undefined
    if (line !== undefined) return { end: line, escaped }
    if (piece.end >= text.length) return { end: text.length, escaped }
    if (endsString(text, piece.end + 1, inContainer)) return { end: piece.end, escaped }
    index = piece.end + 1
  }
}

/** The end of the first line in the range whose next line starts with a quote or a closing bracket. */
function entryLineEnd(text: string, from: number, to: number): number | undefined {
  let index = from
  while (index < to) {
    const unit = text.charCodeAt(index)
    if (unit !== LINE_FEED && unit !== CARRIAGE_RETURN) {
      index++
      continue
    }
    const next = skipSpace(text, index + 1)
    const first = text.charCodeAt(next)
    if (first === QUOTE || first === CLOSE_BRACE || first === CLOSE_BRACKET) return index
    index = next
  }
  return undefined
}

function lineEnd(text: string, from: number, to: number): number | undefined {
  for (let index = from; index < to; index++) {
    const unit = text.charCodeAt(index)
    if (unit === LINE_FEED || unit === CARRIAGE_RETURN) return index
  }
  return undefined
}

/** Whether what's at `at`, after spaces and tabs, can follow a number, a literal or a container. */
function endsToken(text: string, at: number): boolean {
  const index = skipBlank(text, at)
  return index >= text.length || endsBare(text.charCodeAt(index)) || isLineEnd(text, index)
}

/**
 * Whether what's at `at`, after spaces and tabs, can follow a string: the text's or a line's end, a
 * comma that a value or a line's end follows, a closing bracket with no word straight after it, what
 * ends a sentence, or inside a container, a key's colon.
 */
function endsString(text: string, at: number, inContainer: boolean): boolean {
  const index = skipBlank(text, at)
  if (index >= text.length || isLineEnd(text, index)) return true
  const unit = text.charCodeAt(index)
  if (unit === COMMA) return followsComma(text, skipBlank(text, index + 1))
  if (unit === CLOSE_BRACE || unit === CLOSE_BRACKET) return !isWord(text.charCodeAt(index + 1))
  return (inContainer && unit === COLON) || endsSentence(text, index)
}

function endsSentence(text: string, at: number): boolean {
  let index = at
  while (index < text.length && SENTENCE_ENDS.includes(text.charAt(index))) index++
  return index > at && (index >= text.length || isSpace(text.charCodeAt(index)) || isLineEnd(text, index))
}

function followsComma(text: string, at: number): boolean {
  if (at >= text.length || isLineEnd(text, at)) return true
  const unit = text.charCodeAt(at)
  if (unit === QUOTE || unit === OPEN_BRACE || unit === OPEN_BRACKET || unit === CLOSE_BRACE || unit === CLOSE_BRACKET || unit === MINUS) return true
  return isDigit(unit) || LITERALS.some((literal) => text.startsWith(literal, at))
}

/** A line's end, written as it is or as printed JSON has it. */
function isLineEnd(text: string, at: number): boolean {
  const unit = text.charCodeAt(at)
  if (unit === LINE_FEED || unit === CARRIAGE_RETURN) return true
  if (unit !== BACKSLASH) return false
  const next = text.charAt(at + 1)
  return next === 'n' || next === 'r'
}

function isDigit(unit: number): boolean {
  return unit >= 48 && unit <= 57
}

function isLetter(unit: number): boolean {
  return (unit >= 65 && unit <= 90) || (unit >= 97 && unit <= 122)
}

function isWord(unit: number): boolean {
  return isDigit(unit) || isLetter(unit)
}

/** Where a container ends, past its closing bracket. Undefined: its brackets don't match, or it never closes. */
function containerEnd(text: string, at: number, work: ScanWork): number | undefined {
  const open: number[] = []
  let index = at
  while (index < text.length) {
    const unit = text.charCodeAt(index)
    if (unit === QUOTE) {
      index = valueEnd(text, index + 1, true, work).end + 1
      continue
    }
    if (unit === OPEN_BRACE || unit === OPEN_BRACKET) {
      spend(work)
      open.push(unit)
    } else if (unit === CLOSE_BRACE || unit === CLOSE_BRACKET) {
      spend(work)
      if (open.pop() !== unit - 2) return undefined
      if (open.length === 0) return index + 1
    }
    index++
  }
  return undefined
}

/** The next unescaped quote from `from`, or the end of the text, and whether a backslash came before it. */
function lex(text: string, from: number): { end: number; escaped: boolean } {
  let escaped = false
  for (let index = from; index < text.length; index++) {
    const unit = text.charCodeAt(index)
    if (unit === QUOTE) return { end: index, escaped }
    if (unit === BACKSLASH) {
      escaped = true
      index++
    }
  }
  return { end: text.length, escaped }
}

function body(view: View, start: number, end: number, escaped: boolean, work: ScanWork): string {
  return escaped ? decode(view, start, end, work).text : view.text.slice(start, end)
}

/**
 * Escapes decoded, keeping where each character came from: JSON's, and those printed text and YAML
 * have, including YAML's folded lines. Anything else stays as it is, so a truncated escape or a
 * Windows path does no harm. JSON itself holds none of the others.
 */
function decode(view: View, start: number, end: number, work: ScanWork): View {
  const { text } = view
  const out: string[] = []
  const map: number[] = []
  spend(work, 1 + ((end - start) >> 4))
  let index = start
  while (index < end) {
    const found = text.indexOf('\\', index)
    const stop = found === -1 || found >= end ? end : found
    if (stop > index) {
      out.push(text.slice(index, stop))
      for (let at = index; at < stop; at++) map.push(position(view, at))
    }
    if (stop === end) break
    spend(work)
    const next = found + 1 < end ? text.charAt(found + 1) : ''
    let chars = escapedChar(next) ?? OTHER_ESCAPES[next]
    let length = 2
    if (next === 'u' && hasHex(text, found + 2, 4, end)) {
      chars = String.fromCharCode(Number.parseInt(text.slice(found + 2, found + 6), 16))
      length = 6
    } else if (next === 'x' && hasHex(text, found + 2, 2, end)) {
      chars = String.fromCharCode(Number.parseInt(text.slice(found + 2, found + 4), 16))
      length = 4
    } else if (next === 'U' && hasHex(text, found + 2, 8, end) && Number.parseInt(text.slice(found + 2, found + 10), 16) <= MAX_CODE_POINT) {
      chars = String.fromCodePoint(Number.parseInt(text.slice(found + 2, found + 10), 16))
      length = 10
    } else if (next === '\n' || next === '\r') {
      chars = ''
      length = foldEnd(text, found + 1, end) - found
    } else if (chars === undefined) {
      chars = '\\'
      length = 1
    }
    out.push(chars)
    const from = position(view, found)
    for (let unit = 0; unit < chars.length; unit++) map.push(from)
    index = found + length
  }
  map.push(position(view, end))
  return { text: out.join(''), map }
}

function hasHex(text: string, at: number, count: number, end: number): boolean {
  if (at + count > end) return false
  for (let index = at; index < at + count; index++) {
    const unit = text.charCodeAt(index)
    if (!(isDigit(unit) || (unit >= 65 && unit <= 70) || (unit >= 97 && unit <= 102))) return false
  }
  return true
}

/** Past the line's end at `at` and the next line's indentation. */
function foldEnd(text: string, at: number, end: number): number {
  let index = at + (text.charAt(at) === '\r' && text.charAt(at + 1) === '\n' && at + 1 < end ? 2 : 1)
  while (index < end && (text.charCodeAt(index) === SPACE || text.charCodeAt(index) === TAB)) index++
  return index
}

function escapedChar(char: string): string | undefined {
  switch (char) {
    case '"':
    case '\\':
    case '/':
      return char
    case 'b':
      return '\b'
    case 'f':
      return '\f'
    case 'n':
      return '\n'
    case 'r':
      return '\r'
    case 't':
      return '\t'
    default:
      return undefined
  }
}

/** Spelled out as a placeholder in the string given, so that `reserve` has seen its number. */
function isPlaceholder(view: View, start: number, end: number): boolean {
  if (end - start > LONGEST_PLACEHOLDER || position(view, end) - position(view, start) !== end - start) return false
  return WHOLE_PLACEHOLDER.test(view.text.slice(start, end))
}

function position(view: View, index: number): number {
  return view.map === undefined ? index : (view.map[index] as number)
}

function skipSpace(text: string, from: number): number {
  let index = from
  while (index < text.length && isSpace(text.charCodeAt(index))) index++
  return index
}

function skipBlank(text: string, from: number): number {
  let index = from
  while (index < text.length && (text.charCodeAt(index) === SPACE || text.charCodeAt(index) === TAB)) index++
  return index
}

/** JSON's whitespace, the same in both languages. */
function isSpace(unit: number): boolean {
  return unit === SPACE || unit === TAB || unit === LINE_FEED || unit === CARRIAGE_RETURN
}

/** What ends a value that isn't JSON: so `"password": correct horse battery` is masked whole. */
function endsBare(unit: number): boolean {
  return unit === LINE_FEED || unit === CARRIAGE_RETURN || unit === COMMA || unit === CLOSE_BRACE || unit === CLOSE_BRACKET
}
