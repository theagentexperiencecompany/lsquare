import { hasOwn, readSettings } from './json.js'
import { mayHoldKey, SCAN_WORK, type ScanWork, secretSpans } from './secret-spans.js'
import type { Json, PatternName, ToolCall } from './types.js'

/**
 * The exact patterns from the spec. Redaction runs on the host app's event loop, so every pattern must match
 * in linear time: emails, JWTs and IPv6 addresses may only start at a token boundary, or straight after
 * an escape written in a string (at most two attempts per run of token characters), a PEM body can't run past the next "-----", a URL's credentials
 * can't run past the next "/", "?", "#", '"' or 256 characters, and the Basic token check looks at most 512 characters ahead.
 * Secrets are not length-bounded, so long credentials are still caught.
 */
export const PATTERNS: Record<PatternName, RegExp> = {
  email: /(?:(?<![A-Za-z0-9._%+-])(?!(?<=\\)(?:[nrtfb]|(?:u[0-9A-Fa-f]{4}|x[0-9A-Fa-f]{2}|U[0-9A-Fa-f]{8})(?!%40)[A-Za-z0-9._%+-]))|(?<=\\[nrtfb])|(?<=\\u[0-9A-Fa-f]{4})|(?<=\\x[0-9A-Fa-f]{2})|(?<=\\U[0-9A-Fa-f]{8}))[A-Za-z0-9._%+-]+(?:@|%40)[A-Za-z0-9.-]{1,253}\.[A-Za-z]{2,24}/g,
  phone: /\+[1-9]\d{0,2}[ .-]?\(?\d{1,4}\)?(?:[ .-]?\d{2,4}){2,4}\b|\(\d{3}\) ?\d{3}-\d{4}\b|(?:\b|(?<=\\[nrtfb0aevNLP_])|(?<=\\u[0-9A-Fa-f]{4})|(?<=\\x[0-9A-Fa-f]{2})|(?<=\\U[0-9A-Fa-f]{8}))\d{3}-\d{3}-\d{4}\b/g,
  card: /(?<!\d\.)(?:\b|(?<=\\[nrtfb0aevNLP_])|(?<=\\u[0-9A-Fa-f]{4})|(?<=\\x[0-9A-Fa-f]{2})|(?<=\\U[0-9A-Fa-f]{8}))(?:(?:[3-6]\d{3}|2(?:20[0-4]|22[1-9]|2[3-9]\d|[3-6]\d\d|7[01]\d|720))(?:[ -]?\d){11,12}\b(?<![ -]\d)(?<![ -]\d\d)|3[068]\d\d(?:[ -]?\d){10}\b)/g,
  secret:
    /(?:\b|(?<=\\[nrtfb0aevNLP_])|(?<=\\u[0-9A-Fa-f]{4})|(?<=\\x[0-9A-Fa-f]{2})|(?<=\\U[0-9A-Fa-f]{8}))(?:lsq_[A-Za-z0-9_-]{40,}|sk-[A-Za-z0-9_-]{20,}|[rs]k_(?:live|test)_[A-Za-z0-9]{16,}|whsec_[A-Za-z0-9]{24,}|gh[opsur]_[A-Za-z0-9]{36,}|github_pat_[A-Za-z0-9_]{20,}|glpat-[A-Za-z0-9_-]{20,}|npm_[A-Za-z0-9]{36}|xox[a-z]-[A-Za-z0-9%+\/=-]{10,}|xapp-[A-Za-z0-9-]{10,}|hooks\.slack(?:-gov)?\.com\\{0,2}\/(?:services|triggers|workflows|commands|actions)\\{0,2}\/[A-Za-z0-9\/\\_-]{20,}|(?:AKIA|ASIA)[0-9A-Z]{16}|(?:aws_secret_access_key|AWS_SECRET_ACCESS_KEY)(?:\\{0,7}["'])?\s*[:=]\s*(?:\\{0,7}["'])?(?:[A-Za-z0-9\/+]|\\{1,2}\/){40}|AIza[0-9A-Za-z_-]{35}|ya29\.[A-Za-z0-9._-]{20,}|GOCSPX-[A-Za-z0-9_-]{20,}|phx_[A-Za-z0-9]{20,})|(?:(?<![A-Za-z0-9_-])|(?<=\\[nrtfb0aevNLP_])|(?<=\\u[0-9A-Fa-f]{4})|(?<=\\x[0-9A-Fa-f]{2})|(?<=\\U[0-9A-Fa-f]{8}))eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}|(?:[Bb]earer|BEARER)\s+(?=[A-Za-z0-9._~+/-]{0,512}\d)[A-Za-z0-9._~+/-]{16,}=*|(?:\b|(?<=\\[nrtfb0aevNLP_])|(?<=\\u[0-9A-Fa-f]{4})|(?<=\\x[0-9A-Fa-f]{2})|(?<=\\U[0-9A-Fa-f]{8}))(?:Basic|BASIC)\s+(?=[A-Za-z0-9+/]{0,512}[0-9+=])[A-Za-z0-9+/]{16,}={0,2}|(?:(?<=:\/\/)|(?<=:\\\/\\\/)|(?<=:\\\\\/\\\\\/))[^\s/:?#"]{0,256}:[^\s/?#"]{1,256}(?=@)|-----BEGIN [A-Z ]{0,32}PRIVATE KEY-----(?:[^-]|-(?!----))*?(?:-----END [A-Z ]{0,32}PRIVATE KEY-----|(?:-----[A-Z ]{0,48}-{0,4})?(?![\d\D]))/g,
  ip: /(?:\b|(?<=\\[nrtfb0aevNLP_])|(?<=\\u[0-9A-Fa-f]{4})|(?<=\\x[0-9A-Fa-f]{2})|(?<=\\U[0-9A-Fa-f]{8}))(?:25[0-5]|2[0-4]\d|1?\d?\d)(?:\.(?:25[0-5]|2[0-4]\d|1?\d?\d)){3}\b|(?:(?<![0-9A-Za-z:.])|(?<=\\[nrtfb0aevNLP_])|(?<=\\u[0-9A-Fa-f]{4})|(?<=\\x[0-9A-Fa-f]{2})|(?<=\\U[0-9A-Fa-f]{8}))(?:(?:[0-9A-Fa-f]{1,4}:){7}[0-9A-Fa-f]{1,4}|(?:[0-9A-Fa-f]{1,4}:){1,7}:|(?:[0-9A-Fa-f]{1,4}:){1,6}:[0-9A-Fa-f]{1,4}|(?:[0-9A-Fa-f]{1,4}:){1,5}(?::[0-9A-Fa-f]{1,4}){1,2}|(?:[0-9A-Fa-f]{1,4}:){1,4}(?::[0-9A-Fa-f]{1,4}){1,3}|(?:[0-9A-Fa-f]{1,4}:){1,3}(?::[0-9A-Fa-f]{1,4}){1,4}|(?:[0-9A-Fa-f]{1,4}:){1,2}(?::[0-9A-Fa-f]{1,4}){1,5}|[0-9A-Fa-f]{1,4}:(?::[0-9A-Fa-f]{1,4}){1,6}|:(?::[0-9A-Fa-f]{1,4}){1,7})(?![0-9A-Za-z:]|\.\d)/g,
}

/**
 * The secret key names from the spec. In free-form fields, a key whose name ends with one of these,
 * once lowercased and stripped of everything but letters and digits, holds a secret whatever its
 * value looks like: `dbPassword`, `client_secret`, `X-Api-Key` and `AWS_SECRET_ACCESS_KEY` match,
 * `apiKeyId`, `maxTokens` and `author` don't. Part of the `secret` pattern.
 */
export const SECRET_KEYS: readonly string[] = [
  'password', 'passwords', 'passwd', 'passphrase', 'secret', 'secrets', 'token', 'auth', 'authorization',
  'apikey', 'apikeys', 'privatekey', 'secretkey', 'accesskey', 'signingkey', 'encryptionkey',
  'credential', 'credentials', 'cookie', 'cookies', 'connectionstring',
]

/** Secrets first, so a token's digits are never read as a card or phone number. */
const ORDER: PatternName[] = ['secret', 'email', 'card', 'phone', 'ip']

const LABELS: Record<PatternName, string> = { email: 'EMAIL', phone: 'PHONE', card: 'CARD', secret: 'SECRET', ip: 'IP' }
const PATTERN_BY_LABEL = new Map(Object.entries(LABELS).map(([pattern, label]) => [label, pattern as PatternName]))
const EXISTING_PLACEHOLDER = /\[(EMAIL|PHONE|CARD|SECRET|IP)_(\d{1,9})\]/g
const WHOLE_PLACEHOLDER = /^\[(?:EMAIL|PHONE|CARD|SECRET|IP)_\d{1,9}\]$/

/** Fields that identify the turn or are added by the SDK. Never redacted. */
const SKIPPED_FIELDS = new Set([
  'conversationId', 'turnId', 'orgId', 'apiKeyId', 'toolName', 'surface', 'client', 'status',
  'eventId', 'capturedAt', 'source', 'schemaVersion', 'sdk', 'redactions', 'dropped',
])

/** Fields whose keys come from the app's data, like tool inputs. Secret key names apply there, and to JSON held in any string. */
const FREE_FORM_FIELDS = new Set(['latency', 'verification', 'metadata'])

function isSecretKey(key: string): boolean {
  const name = key.toLowerCase().replace(/[^a-z0-9]/g, '')
  return SECRET_KEYS.some((suffix) => name.endsWith(suffix))
}

export function resolvePatterns(option: false | Partial<Record<PatternName, boolean>> | undefined): PatternName[] {
  if (option === false) return []
  // Never from Object.prototype, so a polluted prototype can't turn a pattern off.
  const settings = readSettings(option, ORDER)
  return ORDER.filter((pattern) => settings[pattern] !== false)
}

export function luhn(text: string): boolean {
  const digits = text.replace(/\D/g, '')
  let sum = 0
  for (let i = 0; i < digits.length; i++) {
    let digit = Number(digits[digits.length - 1 - i])
    if (i % 2 === 1) {
      digit *= 2
      if (digit > 9) digit -= 9
    }
    sum += digit
  }
  return digits.length > 0 && sum % 10 === 0
}

/**
 * Replaces private data in every string and object key of an event body, except the skipped
 * fields, and the whole value under a secret key name in tool inputs and free-form fields.
 * Placeholders are numbered per event: the same value gets the same number everywhere.
 */
export function redactBody<T extends object>(
  body: T,
  patterns: PatternName[],
): { value: T; counts: Partial<Record<PatternName, number>> } {
  if (patterns.length === 0) return { value: body, counts: {} }
  const redactor = new Redactor(patterns)
  redactor.reserve(body)
  const out: Record<string, unknown> = {}
  for (const [key, value] of Object.entries(body)) {
    if (SKIPPED_FIELDS.has(key)) out[key] = value
    else if (key === 'toolCalls' && Array.isArray(value)) out[key] = value.map((call: ToolCall) => redactor.toolCall(call))
    else out[key] = redactor.walk(value as Json, false, FREE_FORM_FIELDS.has(key))
  }
  return { value: out as T, counts: redactor.counts }
}

class Redactor {
  readonly counts: Partial<Record<PatternName, number>> = {}
  private readonly used = new Map<PatternName, Set<number>>()
  private readonly next = new Map<PatternName, number>()
  private readonly assigned = new Map<string, number>()
  private readonly secretKeys: boolean
  private readonly scanWork: ScanWork = { left: SCAN_WORK }

  constructor(private readonly patterns: PatternName[]) {
    this.secretKeys = patterns.includes('secret')
  }

  /** Records placeholders already in the event, so new ones never reuse their numbers. */
  reserve(value: unknown): void {
    if (typeof value === 'string') {
      for (const match of value.matchAll(EXISTING_PLACEHOLDER)) {
        const pattern = PATTERN_BY_LABEL.get(match[1] as string)
        if (pattern) this.usedNumbers(pattern).add(Number(match[2]))
      }
    } else if (Array.isArray(value)) {
      for (const item of value) this.reserve(item)
    } else if (typeof value === 'object' && value !== null) {
      for (const [key, item] of Object.entries(value)) {
        this.reserve(key)
        this.reserve(item)
      }
    }
  }

  toolCall(call: ToolCall): ToolCall {
    const out: ToolCall = { ...call }
    // Its own fields only: a polluted Object.prototype's would be added to every call without one.
    if (hasOwn(call, 'input') && call.input !== undefined) out.input = this.walk(call.input, true, true)
    if (hasOwn(call, 'errorMessage') && call.errorMessage !== undefined) out.errorMessage = this.string(call.errorMessage)
    return out
  }

  walk(value: Json, inToolInput: boolean, freeForm: boolean): Json {
    if (typeof value === 'string') return this.string(value)
    if (typeof value === 'number') return inToolInput ? this.number(value) : value
    if (value === null || typeof value === 'boolean') return value
    if (Array.isArray(value)) return value.map((item) => this.walk(item, inToolInput, freeForm))
    const out: { [key: string]: Json } = {}
    for (const [key, item] of Object.entries(value)) {
      let redactedKey = this.text(key)
      // Two keys that redact to the same placeholder must not overwrite each other.
      if (Object.prototype.hasOwnProperty.call(out, redactedKey)) redactedKey = this.text(key, true)
      out[redactedKey] =
        freeForm && this.secretKeys && isSecretKey(key) ? this.secretValue(item) : this.walk(item, inToolInput, freeForm)
    }
    return out
  }

  /**
   * The whole value under a secret key name, whatever its shape. Values that can't hold a secret
   * (null, booleans, empty values) and existing placeholders stay as they are.
   */
  private secretValue(value: Json): Json {
    if (value === null || typeof value === 'boolean' || value === '') return value
    if (typeof value === 'string' && WHOLE_PLACEHOLDER.test(value)) return value
    if (typeof value === 'object' && Object.keys(value).length === 0) return value
    return this.placeholder('secret', typeof value === 'string' ? value : JSON.stringify(value), false)
  }

  /** A string value: what's under secret key names in JSON it holds, then the patterns. */
  private string(input: string): string {
    return this.text(this.withoutSecretValues(input))
  }

  private withoutSecretValues(input: string): string {
    if (!this.secretKeys || !mayHoldKey(input)) return input
    let out = ''
    let from = 0
    for (const span of secretSpans(input, isSecretKey, this.scanWork)) {
      const placeholder = this.placeholder('secret', span.identity ?? '', span.identity === undefined)
      out += input.slice(from, span.start) + span.quote + placeholder + span.quote
      from = span.end
    }
    return from === 0 ? input : out + input.slice(from)
  }

  text(input: string, fresh = false): string {
    let out = input
    for (const pattern of this.patterns) {
      if (pattern === 'email' && !out.includes('@') && !out.includes('%40')) continue
      if (pattern === 'ip' && !out.includes('.') && !out.includes(':')) continue
      out = out.replace(PATTERNS[pattern], (match: string, offset: number, whole: string) => {
        if (pattern === 'card' && !luhn(match)) return match
        // IPv6 needs three groups: `::1`, `fe80::1` and code such as `data[::10]` or `A::B` stay.
        if (pattern === 'ip' && match.includes(':') && match.split(':').filter(Boolean).length < 3) return match
        if (pattern === 'secret' && leavesUrl(match, offset, whole)) return this.secretsIn(match, fresh)
        return this.placeholder(pattern, identity(pattern, match), fresh)
      })
    }
    return out
  }

  /** A match that holds no credentials: only the secrets in it go. */
  private secretsIn(match: string, fresh: boolean): string {
    return match.replace(PATTERNS.secret, (found) => this.placeholder('secret', identity('secret', found), fresh))
  }

  /** Card numbers stored as JSON numbers inside tool inputs. */
  private number(value: number): Json {
    if (!this.patterns.includes('card') || !Number.isInteger(value) || value <= 0) return value
    const digits = String(value)
    if (!/^(?:[3-6]\d{14,15}|2(?:20[0-4]|22[1-9]|2[3-9]\d|[3-6]\d\d|7[01]\d|720)\d{11,12}|3[068]\d{12})$/.test(digits) || !luhn(digits)) return value
    return this.placeholder('card', digits, false)
  }

  private placeholder(pattern: PatternName, id: string, fresh: boolean): string {
    const key = `${pattern}\u0000${id}`
    let n = fresh ? undefined : this.assigned.get(key)
    if (n === undefined) {
      n = this.nextFree(pattern)
      if (!fresh) this.assigned.set(key, n)
    }
    // Its own count: a polluted Object.prototype's would start it, inflating it or making it a
    // string the ingestion rejects.
    this.counts[pattern] = ((hasOwn(this.counts, pattern) ? this.counts[pattern] : undefined) ?? 0) + 1
    return `[${LABELS[pattern]}_${n}]`
  }

  /** A running counter per pattern, skipping numbers already in the event: linear overall. */
  private nextFree(pattern: PatternName): number {
    const used = this.usedNumbers(pattern)
    let n = this.next.get(pattern) ?? 1
    while (used.has(n)) n++
    used.add(n)
    this.next.set(pattern, n + 1)
    return n
  }

  private usedNumbers(pattern: PatternName): Set<number> {
    let set = this.used.get(pattern)
    if (!set) {
      set = new Set()
      this.used.set(pattern, set)
    }
    return set
  }
}

/** `://` as it's written in text, in JSON and in JSON in JSON. */
const URL_SLASHES = ['://', ':\\/\\/', ':\\\\/\\\\/']

/** Whether `form` at `at` is the whole of how a quote is written there: `o''''brien` and a shell's `'\''` hold neither `'` nor `''`. */
function isWrittenAs(text: string, at: number, form: string): boolean {
  const quote = form.charAt(form.length - 1)
  const before = text.slice(Math.max(at - 1, 0), at)
  const after = text.slice(at + form.length, at + form.length + 1)
  return before !== '\\' && before !== quote && after !== '\\' && after !== quote
}

/**
 * Whether a URL's credentials match ran out of its URL, and so holds none: `host:443','ok','ops` in
 * `['https://host:443','ok','ops@...']`. It takes all of this: the URL is straight after a `'`
 * or a backtick, alone, doubled or after a backslash, as an SQL or JavaScript string inside another
 * writes it. That quote closes before any `@` and with no word character after it, as `o'brien` has,
 * and it opens again before the match ends, written the same way each time. Anything else stays masked
 * whole, since `new URL` leaves a `'` in a password and `urlunsplit` a backtick too. Linear: it reads
 * the match and at most 42 characters before it, and matches don't overlap.
 */
function leavesUrl(match: string, offset: number, whole: string): boolean {
  if (whole[offset + match.length] !== '@') return false
  const slashes = URL_SLASHES.find((written) => offset >= written.length && whole.startsWith(written, offset - written.length))
  if (slashes === undefined) return false
  const colon = offset - slashes.length
  let scheme = colon
  while (scheme > 0 && colon - scheme < 32 && /[A-Za-z0-9+.-]/.test(whole.charAt(scheme - 1))) scheme--
  if (scheme === colon || scheme === 0) return false
  const quote = whole.charAt(scheme - 1)
  if (quote !== "'" && quote !== '`') return false
  const previous = whole.slice(Math.max(scheme - 2, 0), scheme - 1)
  const form = previous === quote || previous === '\\' ? previous + quote : quote
  if (!isWrittenAs(whole, scheme - form.length, form)) return false
  const closedBy = (at: number) => isWrittenAs(match, at, form) && !/\w/.test(match.slice(at + form.length, at + form.length + 1))
  let closes = match.indexOf(form)
  while (closes !== -1 && !closedBy(closes)) closes = match.indexOf(form, closes + 1)
  if (closes === -1 || match.slice(0, closes).includes('@')) return false
  for (let opens = match.indexOf(form, closes + form.length); opens !== -1; opens = match.indexOf(form, opens + 1)) {
    if (isWrittenAs(match, opens, form)) return true
  }
  return false
}

/** What makes two matches "the same value" for numbering. */
function identity(pattern: PatternName, match: string): string {
  if (pattern === 'email') return match.toLowerCase().replaceAll('%40', '@')
  if (pattern === 'ip') return match.toLowerCase()
  if (pattern === 'card' || pattern === 'phone') return match.replace(/\D/g, '')
  return match
}
