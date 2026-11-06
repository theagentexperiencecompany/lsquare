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
/** What containerEnd() gives for a container with a string in other quotes: it's not JSON. */
const NOT_JSON = -1
/** What text ending in isn't a key, unless it starts an entry of an object: it is code or a sentence before a string, as in `token == ":"` or `createToken(":", a)`. */
const NOT_A_KEY_END = ' \t=([+?,.<|&'
/** What a sentence or code can have after a closing quote, before a space or a line's end. */
const SENTENCE_ENDS = '.;)\'`'
/** A name is matched by its end, so only this much of a long key is read. */
const KEY_READ = 256
/** The escapes printed text and YAML have that JSON doesn't, besides those that take digits. */
const OTHER_ESCAPES: Record<string, string> = { "'": "'", ' ': ' ', '0': '\0', a: '\x07', e: '\x1b', v: '\v', N: '\x85', _: '\xa0', L: '\u2028', P: '\u2029' }
/** A key at the nesting limit: a quote, whitespace and a colon, each as it is or encoded any number of times. */
const ENCODED_KEY = /(?:"|\\u0022)(?:[ \t\r\n]|\\+[nrt]|\\+u00(?:20|09|0[aAdD]))*(?::|\\+u003[aA])/

/** Parameters that hold secrets only in a URL: a signed URL's, and an OAuth or Azure Functions `code`. */
const URL_ONLY_NAMES = /(?:^|[^A-Za-z0-9])(?:sig|signature|hmac)$|^(?:code|code_verifier)$/i
/** What a query value stops at: where a URL ends in text, or the next parameter. */
const QUERY_VALUE_END = ' \t\r\n\f\v&#"\'<>`)]}\\'
/** A query value that is a template, not a value, up to where a value ends: `{token}`, `{{token}}`, `$TOKEN`, `${TOKEN}`. */
const QUERY_TEMPLATE = /^(?:\{[A-Za-z0-9_.-]+\}|\{\{ ?[A-Za-z0-9_.-]+ ?\}\}|\$\{[A-Za-z0-9_.-]+\}|\$[A-Z][A-Z0-9_]*)/
const INPUT_VALUE = 'input_value='
/** Where pydantic's errors have it: straight after the error's type, which a custom error can spell any way. */
const PYDANTIC_VALUE = /\[type=[^\r\n]{1,128}?, input_value=/g
const INPUT_TYPE = ', input_type='
/** What's after `, input_type=` where it does end a value: the type's name and the bracket that closes the error's. */
const INPUT_TYPE_END = /[^ \t\r\n'"=]*\]/y
/** How far past its start a value printed by pydantic is looked for its end: it cuts long ones to well under this. */
const INPUT_READ = 1024
const WRITTEN_LINE_END = /(?<!\\)\\+[nr]/g
const PEM_START = '-----BEGIN '
/** A private key block, or one the string's end cuts. */
const PEM_BLOCK = /-----BEGIN [A-Z ]{0,32}PRIVATE KEY-----(?:[^-]|-(?!----))*?(?:-----END [A-Z ]{0,32}PRIVATE KEY-----|(?:-----[A-Z ]{0,48}-{0,4})?$)/g
/** How many times a URL held in a URL is read: a redirect chain encodes it again each time. */
const MAX_PERCENT = 3
const PERCENT_EQUALS = /%(?:25)*3[Dd]/
const PERCENT_BYTE = /%([0-9A-Fa-f]{2})/g
/** How far past a closing quote a run of what ends a sentence is read. */
const SENTENCE_READ = 5
/** How far past a Python string's first quote a quote that can end it is looked for: each string reads this at most, so the work stays linear. */
const QUOTE_READ = 256
const PYTHON_LITERALS = ['None', 'True', 'False', 'true', 'false', 'null']
/** util.inspect's mark on a container something inside it refers back to. */
const INSPECT_REF = /<ref \*[0-9]{1,9}> /y

const TAB = 9
const LINE_FEED = 10
const CARRIAGE_RETURN = 13
const SPACE = 32
const BANG = 33
const QUOTE = 34
const HASH = 35
const PERCENT = 37
const AMPERSAND = 38
const APOSTROPHE = 39
const OPEN_PAREN = 40
const CLOSE_PAREN = 41
const PLUS = 43
const COMMA = 44
const MINUS = 45
const PERIOD = 46
const SLASH = 47
const COLON = 58
const SEMICOLON = 59
const LESS = 60
const EQUALS = 61
const GREATER = 62
const QUESTION = 63
const OPEN_BRACKET = 91
const BACKSLASH = 92
const CLOSE_BRACKET = 93
const UNDERSCORE = 95
const BACKTICK = 96
const LOWER_B = 98
const LOWER_U = 117
const OPEN_BRACE = 123
const CLOSE_BRACE = 125
const TILDE = 126

/**
 * How much scanning one event may do. A unit is roughly one quote or bracket read, one backslash or
 * percent sign decoded, one JSON value checked, one key looked at, or 16 characters decoded or of a parameter's name. So a crafted string
 * can't hold up the host app's event loop for long, where ordinary text doesn't come near it. Past this, a
 * string `mayHoldKey` lets through is replaced whole, as nothing unread may be sent.
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
  /** A decoded view where a container didn't close with its line ends as escapes: the rest are read with them as line ends. */
  linesIn: View | undefined
}

/** A string to scan, and where each of its positions is in the string given. The string itself has no map. */
interface View {
  text: string
  map: number[] | undefined
}

/**
 * Whether a string could hold a secret value at all: the cheap check before scanning it. It needs a
 * quote and a colon or `=>`, as they are or encoded any number of times; or an `=`, as it is or percent-encoded,
 * and what can start a URL's parameter; or pydantic's `input_value=`.
 */
export function mayHoldKey(text: string): boolean {
  if ((text.includes('"') || text.includes("'") || text.includes('u0022')) && (text.includes(':') || text.includes('=>') || text.includes('u003a') || text.includes('u003A'))) return true
  if (text.includes('%') && PERCENT_EQUALS.test(text)) return true
  return text.includes('=') && (text.includes('?') || text.includes('&') || text.includes('#') || text.includes(INPUT_VALUE))
}

/** The values under secret key names in `text`, in order and not overlapping. */
export function secretSpans(text: string, isSecretKey: (key: string) => boolean, work: ScanWork): SecretSpan[] {
  const reading: Reading = { isSecretKey, spans: [], work, linesIn: undefined }
  try {
    read({ text, map: undefined }, 0, false, reading)
  } catch (error) {
    if (error !== OVER_WORK) throw error
    return [{ start: 0, end: text.length, identity: undefined, quote: '' }]
  }
  return withWholeKeys(text, merged(reading.spans))
}

/**
 * A value can stop at a private key block's first line, which would leave the block without the
 * BEGIN its pattern looks for. So a block that a value runs into goes whole.
 */
function withWholeKeys(text: string, spans: SecretSpan[]): SecretSpan[] {
  if (spans.length === 0 || !text.includes(PEM_START)) return spans
  const out = [...spans]
  let index = 0
  for (const block of text.matchAll(PEM_BLOCK)) {
    const end = block.index + block[0].length
    while (index < spans.length && (spans[index] as SecretSpan).end <= block.index) index++
    if (index < spans.length && (spans[index] as SecretSpan).start < end) out.push({ start: block.index, end, identity: undefined, quote: '' })
  }
  return out.length === spans.length ? spans : merged(out)
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
  scanSingleQuoted(view, reading)
  scanInputValues(view, reading)
  scanQueries(view, reading)
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
    const arrow = text.startsWith('=>', colon)
    if ((arrow || (text.charCodeAt(colon) === COLON && text.charCodeAt(colon + 1) !== COLON)) && isKey(view, start, end, colon, escaped, reading)) {
      const next = readValue(view, skipSpace(text, colon + (arrow ? 2 : 1)), printed ? depth - 1 : depth, startsEntry(text, start), reading)
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

/**
 * Python's repr of a dict, which is how pydantic's errors quote an input, and other text with
 * single-quoted keys: `{'password': 'x', 'note': "it's"}`, and `Map { 'password' => 'x' }`. Any two
 * quotes in a row can hold a key, whatever is between them, so an apostrophe can't put the scan out
 * of step. Only one that opens an entry is a key, so a sentence's `the 'token': 'x'` isn't, and a SQL
 * cast's `'token'::text` isn't either.
 */
function scanSingleQuoted(view: View, reading: Reading): void {
  const { text } = view
  let open = text.indexOf("'")
  while (open !== -1) {
    const close = text.indexOf("'", open + 1)
    if (close === -1) return
    const after = skipBlank(text, close + 1)
    const arrow = text.startsWith('=>', after)
    if ((arrow || (text.charCodeAt(after) === COLON && text.charCodeAt(after + 1) !== COLON)) && opensEntry(text, open)) {
      spend(reading.work)
      if (reading.isSecretKey(text.slice(Math.max(open + 1, close - KEY_READ), close))) {
        const next = readPythonValue(view, skipSpace(text, after + (arrow ? 2 : 1)), reading)
        if (next === undefined) return
        open = text.indexOf("'", next)
        continue
      }
    }
    open = close
  }
}

/** Whether the quote at `open`, or the `b` or `u` before it, comes straight after `{`, `(` or `,`, or at the start of a line or the text. */
function opensEntry(text: string, open: number): boolean {
  let index = open - 1
  if (text.charCodeAt(index) === LOWER_B || text.charCodeAt(index) === LOWER_U) index--
  while (index >= 0 && (text.charCodeAt(index) === SPACE || text.charCodeAt(index) === TAB)) index--
  if (index < 0) return true
  const unit = text.charCodeAt(index)
  return unit === OPEN_BRACE || unit === OPEN_PAREN || unit === COMMA || unit === LINE_FEED || unit === CARRIAGE_RETURN
}

/** Where a string in Python's quotes starts at or after a `b` or `u` at `at`, or undefined: there is no such string. */
function pythonQuoteAt(text: string, at: number): number | undefined {
  const quoteAt = text.charCodeAt(at) === LOWER_B || text.charCodeAt(at) === LOWER_U ? at + 1 : at
  const unit = text.charCodeAt(quoteAt)
  return unit === APOSTROPHE || unit === QUOTE || unit === BACKTICK ? quoteAt : undefined
}

/** Adds the span of the value at `at`, as Python or Node's util.inspect writes one. Where scanning goes on; undefined: it ran to the end of the text. */
function readPythonValue(view: View, at: number, reading: Reading): number | undefined {
  const { text } = view
  const { spans, work } = reading
  if (at >= text.length) return at
  const quoteAt = pythonQuoteAt(text, at)
  if (quoteAt !== undefined) return readPythonString(view, quoteAt, reading)
  // An escaped quote is read decoded, where it is a string's: `\\"it's\\"`, in JSON holding a repr.
  if (text.charCodeAt(at) === BACKSLASH) return at
  const bracket = pythonBracketAt(text, at)
  if (bracket !== undefined) {
    const close = pythonContainersEnd(view, bracket, reading)
    if (close === undefined) {
      spans.push({ start: position(view, at), end: position(view, text.length), identity: undefined, quote: "'" })
      return undefined
    }
    if ((bracket === at && skipSpace(text, at + 1) === close - 1) || isPlaceholder(view, at, close)) return close
    if (!endsToken(text, close)) return bareValue(view, at, close, "'", spans)
    spans.push({ start: position(view, at), end: position(view, close), identity: text.slice(at, close), quote: "'" })
    return close
  }
  // What util.inspect and repr write in angle brackets: `<Buffer 68 69>` holds a value's bytes.
  if (text.charCodeAt(at) === LESS) {
    const close = text.slice(at, at + KEY_READ).indexOf('>')
    if (close !== -1 && endsToken(text, at + close + 1)) {
      spans.push({ start: position(view, at), end: position(view, at + close + 1), identity: text.slice(at, at + close + 1), quote: "'" })
      return at + close + 1
    }
  }
  for (const literal of PYTHON_LITERALS) {
    if (text.startsWith(literal, at) && endsToken(text, at + literal.length)) return at + literal.length
  }
  const number = jsonNumberEnd(text, at)
  if (number > at && endsToken(text, number)) {
    spans.push({ start: position(view, at), end: position(view, number), identity: text.slice(at, number), quote: "'" })
    return number
  }
  // One word, as `{'token': token}` has. More than that is a sentence: `, 'password': explains it.`
  const limit = Math.min(at + KEY_READ, text.length)
  let end = at
  while (end < limit && !endsBare(text.charCodeAt(end)) && !isSpace(text.charCodeAt(end))) end++
  if (end < limit && !endsToken(text, end)) return at
  return end > at ? bareValue(view, at, at, "'", spans) : at
}

/**
 * Adds the span of the string whose opening quote is at `quoteAt`, and of what continues it: pprint
 * writes a long value as adjacent strings, and util.inspect joins them with `+`. Where scanning goes on.
 */
function readPythonString(view: View, quoteAt: number, reading: Reading): number {
  const { text } = view
  const { spans, work } = reading
  const first = text.charCodeAt(quoteAt)
  const { end, escaped } = pythonStringEnd(view, quoteAt + 1, first, false, work)
  let last = end
  let lastQuote = first
  for (;;) {
    if (text.charCodeAt(last) !== lastQuote) break
    let next = skipSpace(text, last + 1)
    if (text.charCodeAt(next) === PLUS) next = skipSpace(text, next + 1)
    const piece = pythonQuoteAt(text, next)
    if (piece === undefined) break
    const quote = text.charCodeAt(piece)
    const ended = pythonStringEnd(view, piece + 1, quote, true, work).end
    if (text.charCodeAt(ended) === quote && text.charCodeAt(skipBlank(text, ended + 1)) === COLON) break
    last = ended
    lastQuote = quote
  }
  const closed = text.charCodeAt(last) === lastQuote
  if (last === end && isPlaceholder(view, quoteAt + 1, end)) return closed ? end + 1 : end
  // Pieces with different quotes: their placeholder takes one kind, so that they stay balanced.
  if (closed && lastQuote !== first) {
    spans.push({ start: position(view, quoteAt), end: position(view, last + 1), identity: undefined, quote: "'" })
    return last + 1
  }
  if (last > quoteAt + 1) {
    spans.push({
      start: position(view, quoteAt + 1),
      end: position(view, last),
      identity: closed && last === end ? body(view, quoteAt + 1, end, escaped, work) : undefined,
      quote: '',
    })
  }
  return closed ? last + 1 : last
}

/**
 * Where a string in Python's quotes ends: a quote that something able to follow it follows. Without
 * one soon after its first quote, on its line, that first quote, so that a sentence after it stays;
 * without a quote, its line's end, which repr never has in a string. A line end that was an escape, in
 * text read decoded, is one only if no quote follows, or with `lines`.
 */
function pythonStringEnd(view: View, from: number, quote: number, inContainer: boolean, work: ScanWork, lines = false): { end: number; escaped: boolean } {
  const { text } = view
  let escaped = false
  let firstQuote: number | undefined
  let firstLine: number | undefined
  let index = from
  while (index < text.length) {
    if (firstQuote !== undefined && index > firstQuote + QUOTE_READ) return { end: firstQuote, escaped }
    const unit = text.charCodeAt(index)
    if (unit === BACKSLASH) {
      escaped = true
      index += 2
    } else if ((unit === LINE_FEED || unit === CARRIAGE_RETURN) && (lines || !fromEscape(view, index))) {
      return { end: firstQuote ?? index, escaped }
    } else {
      if (unit === quote) {
        spend(work)
        if (endsPythonString(text, index + 1, inContainer)) return { end: index, escaped }
        firstQuote ??= index
      } else if (unit === LINE_FEED || unit === CARRIAGE_RETURN) {
        firstLine ??= index
      }
      index++
    }
  }
  return { end: firstQuote ?? firstLine ?? text.length, escaped }
}

/** Whether what's at `at` in a decoded view was written as an escape. */
function fromEscape(view: View, at: number): boolean {
  return view.map !== undefined && position(view, at + 1) - position(view, at) !== 1
}

/** Not a colon outside a container, which is the next key's quote in a repr pydantic cut short: `{'token': 'abc...i_key': b'x'}`. */
function endsPythonString(text: string, at: number, inContainer: boolean): boolean {
  const index = skipBlank(text, at)
  if (index >= text.length || isLineEnd(text, index) || isWrittenLineEnd(text, index)) return true
  const unit = text.charCodeAt(index)
  if (unit === COMMA || unit === CLOSE_BRACE || unit === CLOSE_BRACKET || unit === CLOSE_PAREN) return true
  if (unit === PLUS) return pythonQuoteAt(text, skipSpace(text, index + 1)) !== undefined
  // A key's end, and `<class 'list'>`.
  if (inContainer && (unit === COLON || unit === GREATER || text.startsWith('=>', index))) return true
  // One run of what ends a sentence, read at most a few characters on: a long run isn't one.
  for (let end = index; end < index + SENTENCE_READ; end++) {
    if (end >= text.length || isSpace(text.charCodeAt(end))) return end > index
    if (!SENTENCE_ENDS.includes(text.charAt(end))) return false
  }
  return false
}

/** The bracket of the container at `at`, which may have any name: `[1, 2]`, `OrderedDict(a='b')`, `Foo { a: 'b' }`, `$e { a: 'b' }`, `f.<locals>.Foo(a='b')`. */
function pythonBracketAt(text: string, at: number): number | undefined {
  INSPECT_REF.lastIndex = at
  const from = INSPECT_REF.test(text) ? INSPECT_REF.lastIndex : at
  const limit = Math.min(from + KEY_READ, text.length)
  let index = from
  while (index < limit && !endsContainerName(text.charCodeAt(index))) index++
  if (index > from && text.charCodeAt(index) === SPACE) index++
  return opensContainer(text.charCodeAt(index)) ? index : undefined
}

function endsContainerName(unit: number): boolean {
  return isSpace(unit) || unit === QUOTE || unit === APOSTROPHE || unit === BACKTICK || unit === COMMA || opensContainer(unit) || unit === CLOSE_BRACE || unit === CLOSE_BRACKET || unit === CLOSE_PAREN
}

function opensContainer(unit: number): boolean {
  return unit === OPEN_BRACE || unit === OPEN_BRACKET || unit === OPEN_PAREN
}

/**
 * Where the container whose first bracket is at `at` ends, past what's written before its contents:
 * `Set(2) { 'a' }`, `Foo(2) [Map] { 'a' => 'b' }`, `[Object: null prototype] { a: 'b' }`, `Resp[int](a=1)`.
 */
function pythonContainersEnd(view: View, at: number, reading: Reading): number | undefined {
  const { text } = view
  let close = pythonContainerRead(view, at, reading)
  while (close !== undefined) {
    const next = text.charCodeAt(close) === SPACE ? close + 1 : close
    if (!opensContainer(text.charCodeAt(next))) break
    close = pythonContainerRead(view, next, reading)
  }
  return close
}

/**
 * A decoded view's line ends were escapes. A container that doesn't close is read again with them as
 * line ends, as in text that has them as they are, and so is every later one in that view, so that
 * many of them don't each read to the far end first.
 */
function pythonContainerRead(view: View, at: number, reading: Reading): number | undefined {
  const { work } = reading
  if (view.map === undefined) return pythonContainerEnd(view, at, false, work)
  if (reading.linesIn === view) return pythonContainerEnd(view, at, true, work)
  const close = pythonContainerEnd(view, at, false, work)
  if (close !== undefined) return close
  reading.linesIn = view
  return pythonContainerEnd(view, at, true, work)
}

/** Where a container in Python's syntax ends, past its closing bracket. Undefined: its brackets don't match, or it never closes. */
function pythonContainerEnd(view: View, at: number, lines: boolean, work: ScanWork): number | undefined {
  const { text } = view
  const open: number[] = []
  let index = at
  while (index < text.length) {
    const unit = text.charCodeAt(index)
    if (unit === APOSTROPHE || unit === QUOTE || unit === BACKTICK) {
      spend(work)
      index = unit !== BACKTICK && text.charCodeAt(index - 1) === BACKSLASH ? escapedStringEnd(text, index, unit) : pythonStringEnd(view, index + 1, unit, true, work, lines).end + 1
      continue
    }
    if (unit === OPEN_BRACE || unit === OPEN_BRACKET || unit === OPEN_PAREN) {
      spend(work)
      open.push(unit)
    } else if (unit === CLOSE_BRACE || unit === CLOSE_BRACKET || unit === CLOSE_PAREN) {
      spend(work)
      if (open.pop() !== (unit === CLOSE_PAREN ? OPEN_PAREN : unit - 2)) return undefined
      if (open.length === 0) return index + 1
    }
    index++
  }
  return undefined
}

/** Past a string whose opening quote has a backslash before it, as JSON holding a repr has: its closing quote has one too. */
function escapedStringEnd(text: string, at: number, quote: number): number {
  const found = text.indexOf(`\\${String.fromCharCode(quote)}`, at + 1)
  return found === -1 ? text.length : found + 2
}

/**
 * Pydantic's errors quote what they refused: `Input should be a valid string [type=string_type,
 * input_value=123, input_type=int]`. It can be cut anywhere, list its key on another line or not at
 * all, and the event's input has it anyway, so it always goes, up to `, input_type=` and the type's
 * name. Without that soon after it, it was cut: up to its line's end, however that's written.
 */
function scanInputValues(view: View, reading: Reading): void {
  const { text } = view
  // Each looked for only once it's behind: a search per value would read to a far one every time.
  let type = -1
  let line = -1
  let written = -1
  PYDANTIC_VALUE.lastIndex = 0
  for (let found = PYDANTIC_VALUE.exec(text); found !== null; found = PYDANTIC_VALUE.exec(text)) {
    spend(reading.work)
    const start = found.index + found[0].length
    if (type < start) type = inputTypeAt(text, start)
    if (line < start) line = lineEnd(text, start, text.length) ?? text.length
    let end = type
    if (type > line || type - start > INPUT_READ) {
      if (written < start) written = writtenLineEnd(text, start)
      end = Math.min(line, written)
    }
    if (end > start && !isPlaceholder(view, start, end)) {
      reading.spans.push({ start: position(view, start), end: position(view, end), identity: reprText(text.slice(start, end)), quote: '' })
    }
    PYDANTIC_VALUE.lastIndex = end
  }
}

/** The next `, input_type=` that ends a value; a value can hold those words. Past the text's end if none does. */
function inputTypeAt(text: string, from: number): number {
  for (let at = text.indexOf(INPUT_TYPE, from); at !== -1; at = text.indexOf(INPUT_TYPE, at + 1)) {
    INPUT_TYPE_END.lastIndex = at + INPUT_TYPE.length
    if (INPUT_TYPE_END.test(text)) return at
  }
  return text.length + 1
}

function writtenLineEnd(text: string, from: number): number {
  WRITTEN_LINE_END.lastIndex = from
  const found = WRITTEN_LINE_END.exec(text)
  return found === null ? text.length : found.index
}

/** What a value printed by repr holds, so a string shares its number with the same value elsewhere. Undefined: pydantic cut it. */
function reprText(value: string): string | undefined {
  if (value.includes('...')) return undefined
  const first = value.charAt(0)
  return value.length > 1 && (first === "'" || first === '"') && value.endsWith(first) ? value.slice(1, -1) : value
}

/**
 * A URL's parameters: `?token=abc&api_key=def`, a fragment's `#access_token=abc`, and a URL held in a
 * parameter, percent-encoded once or more. Their names follow the same rule as keys', and a few go
 * only here: a signed URL's `sig`, an OAuth `code`.
 */
function scanQueries(view: View, reading: Reading): void {
  let current = view
  for (let level = 0; ; level++) {
    scanParameters(current, reading)
    if (level === MAX_PERCENT || !PERCENT_EQUALS.test(current.text)) return
    current = percentDecoded(current, reading.work)
  }
}

function scanParameters(view: View, reading: Reading): void {
  const { text } = view
  let equals = text.indexOf('=')
  while (equals !== -1) {
    const limit = Math.max(0, equals - KEY_READ)
    let start = equals
    while (start > limit && isQueryName(text.charCodeAt(start - 1))) start--
    let next = equals + 1
    if (start < equals && text.charCodeAt(start) !== CLOSE_BRACKET && startsParameter(text, start)) {
      spend(reading.work, 1 + ((equals - start) >> 4))
      const name = parameterName(text.slice(start, equals))
      if (reading.isSecretKey(name) || URL_ONLY_NAMES.test(name)) next = readQueryValue(view, next, reading)
    }
    equals = text.indexOf('=', next)
  }
}

/** A name as it was before encoding, without PHP's and Rails' `[]` at its end. */
function parameterName(name: string): string {
  const decoded = name.includes('%') ? name.replace(PERCENT_BYTE, (_, hex: string) => String.fromCharCode(Number.parseInt(hex, 16))) : name
  let end = decoded.length
  while (decoded.charCodeAt(end - 1) === CLOSE_BRACKET) {
    let open = end - 2
    while (open >= 0 && isDigit(decoded.charCodeAt(open))) open--
    if (open < 0 || decoded.charCodeAt(open) !== OPEN_BRACKET) break
    end = open
  }
  return decoded.slice(0, end)
}

function isQueryName(unit: number): boolean {
  return isWord(unit) || unit === UNDERSCORE || unit === MINUS || unit === PERIOD || unit === TILDE || unit === PERCENT || unit === PLUS || unit === OPEN_BRACKET || unit === CLOSE_BRACKET
}

/** After `?`, `&`, `#`, a hash route's `#/`, `#!` or `#!/`, or `&` as HTML writes it. Not code's `&&`. */
function startsParameter(text: string, start: number): boolean {
  const unit = text.charCodeAt(start - 1)
  if (unit === QUESTION || unit === HASH) return true
  if (unit === AMPERSAND) return text.charCodeAt(start - 2) !== AMPERSAND
  if (unit === SEMICOLON) return text.startsWith('&amp', start - 5)
  if (unit === SLASH || unit === BANG) return text.charCodeAt(start - 2) === HASH || (unit === SLASH && text.startsWith('#!', start - 3))
  return false
}

/** Adds the span of the parameter's value at `at`. Where scanning goes on. */
function readQueryValue(view: View, at: number, reading: Reading): number {
  const { text } = view
  let end = at
  while (end < text.length && !QUERY_VALUE_END.includes(text.charAt(end))) end++
  // A placeholder's bracket ends a value, so it is looked for by its own shape.
  if (text.charCodeAt(at) === OPEN_BRACKET) {
    const close = text.slice(at, at + LONGEST_PLACEHOLDER).indexOf(']')
    if (close !== -1 && isPlaceholder(view, at, at + close + 1)) return at + close + 1
  }
  const template = QUERY_TEMPLATE.exec(text.slice(at, at + KEY_READ))
  if (template !== null) {
    const after = at + template[0].length
    if (after >= text.length || QUERY_VALUE_END.includes(text.charAt(after))) return after
  }
  if (end > at) reading.spans.push({ start: position(view, at), end: position(view, end), identity: text.slice(at, end), quote: '' })
  return end
}

/** `%XX` for an ASCII character decoded, keeping where each character came from. Anything else stays as it is. */
function percentDecoded(view: View, work: ScanWork): View {
  const { text } = view
  const out: string[] = []
  const map: number[] = []
  spend(work, 1 + (text.length >> 4))
  let index = 0
  for (;;) {
    const found = text.indexOf('%', index)
    const stop = found === -1 ? text.length : found
    if (stop > index) {
      out.push(text.slice(index, stop))
      for (let at = index; at < stop; at++) map.push(position(view, at))
    }
    if (found === -1) break
    spend(work)
    const byte = hasHex(text, found + 1, 2, text.length) ? Number.parseInt(text.slice(found + 1, found + 3), 16) : undefined
    if (byte !== undefined && byte < 0x80) {
      out.push(String.fromCharCode(byte))
      index = found + 3
    } else {
      out.push('%')
      index = found + 1
    }
    map.push(position(view, found))
  }
  map.push(position(view, text.length))
  return { text: out.join(''), map }
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
    // Replaced already, whatever follows it: a string a hook changed is read a second time.
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
  // Quoted any other way: `"password": 'x'`, as a repr has for a key with an apostrophe.
  if (pythonQuoteAt(text, at) !== undefined) return readPythonValue(view, at, reading)
  // Only a quoted string is a value wherever its key is: not `Post "https://x/token": timeout`, or `case "token": return x`.
  if (!entry) return at
  const quote = '\\'.repeat(2 ** level - 1) + '"'
  if (first === OPEN_BRACE || first === OPEN_BRACKET) {
    const close = containerEnd(text, at, work)
    if (close === NOT_JSON) return readPythonValue(view, at, reading)
    if (close === undefined) {
      spans.push({ start: position(view, at), end: position(view, text.length), identity: undefined, quote })
      return undefined
    }
    if (isPlaceholder(view, at, close)) return close
    if (endsToken(text, close)) {
      if (skipSpace(text, at + 1) === close - 1) return close
      spans.push({ start: position(view, at), end: position(view, close), identity: text.slice(at, close), quote })
      return close
    }
    // What's written before a container's contents: `[Object: null prototype] { a: 'b' }`.
    if (opensContainer(text.charCodeAt(text.charCodeAt(close) === SPACE ? close + 1 : close))) return readPythonValue(view, at, reading)
    return bareValue(view, at, close, quote, spans)
  }
  // A container with a name: `Foo(a='b')`, `Set(2) { 'a' }`.
  if (pythonBracketAt(text, at) !== undefined) return readPythonValue(view, at, reading)
  for (const literal of PYTHON_LITERALS) {
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

/** A line's end, as text printed more than once has it: any number of backslashes, then `n` or `r`. */
function isWrittenLineEnd(text: string, at: number): boolean {
  let index = at
  while (text.charCodeAt(index) === BACKSLASH) index++
  return index > at && (text.charAt(index) === 'n' || text.charAt(index) === 'r')
}

/** Whether what's at `at`, after spaces and tabs, can follow a number, a literal or a container. */
function endsToken(text: string, at: number): boolean {
  const index = skipBlank(text, at)
  return index >= text.length || endsBare(text.charCodeAt(index)) || isLineEnd(text, index)
}

/**
 * Whether what's at `at`, after spaces and tabs, can follow a string: the text's or a line's end, a
 * comma that a value or a line's end follows, a closing bracket with no word straight after it, what
 * ends a sentence, or inside a container, a key's colon or arrow.
 */
function endsString(text: string, at: number, inContainer: boolean): boolean {
  const index = skipBlank(text, at)
  if (index >= text.length || isLineEnd(text, index)) return true
  const unit = text.charCodeAt(index)
  if (unit === COMMA) return followsComma(text, skipBlank(text, index + 1))
  if (unit === CLOSE_BRACE || unit === CLOSE_BRACKET) return !isWord(text.charCodeAt(index + 1))
  return (inContainer && (unit === COLON || text.startsWith('=>', index))) || endsSentence(text, index)
}

function endsSentence(text: string, at: number): boolean {
  let index = at
  while (index < text.length && SENTENCE_ENDS.includes(text.charAt(index))) index++
  return index > at && (index >= text.length || isSpace(text.charCodeAt(index)) || isLineEnd(text, index))
}

function followsComma(text: string, at: number): boolean {
  if (at >= text.length || isLineEnd(text, at)) return true
  const unit = text.charCodeAt(at)
  if (unit === QUOTE || unit === APOSTROPHE || unit === BACKTICK || unit === OPEN_BRACE || unit === OPEN_BRACKET || unit === CLOSE_BRACE || unit === CLOSE_BRACKET || unit === MINUS) return true
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
    if (unit === APOSTROPHE || unit === BACKTICK) return NOT_JSON
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
