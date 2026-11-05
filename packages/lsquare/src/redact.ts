import { createHash } from 'node:crypto'
import { hasOwn, put, readSettings, silence } from './json.js'
import { mayHoldKey, SCAN_WORK, type ScanWork, type SecretSpan, secretSpans } from './secret-spans.js'
import { advance, Binary, Cut, isMarked, plain, Secret, type Shaped, TooDeep } from './shape.js'
import type { Json, PatternName } from './types.js'

/**
 * JavaScript's `\s`, written out so Python can match the same characters (its own `\s` is a
 * different set). TypeScript's matches are the same as 0.1's.
 */
const WS = '\\t\\n\\v\\f\\r \\u00a0\\u1680\\u2000-\\u200a\\u2028\\u2029\\u202f\\u205f\\u3000\\ufeff'

/**
 * 0.1's patterns, with `\s` written out. Redaction runs on the host app's event loop, so every
 * pattern must match in linear time: emails, JWTs and IPv6 addresses may only start at a token
 * boundary, or straight after an escape written in a string (at most two attempts per run of token characters), a PEM body can't run past the next "-----",
 * a URL's credentials can't run past the next "/", "?", "#", '"' or 256 characters, and the Basic token check looks at most
 * 512 characters ahead. Secrets are not length-bounded, so long credentials are still caught.
 * The phone pattern and the IPv4 octet are 0.1's, rewritten so that adjacent digit repeats can't
 * split one run of digits in more than one way, and they match exactly what 0.1's did, which tests
 * check. 0.1's phone pattern tried every split of a run among a number's country code, area code
 * and groups before failing, hundreds for a `+` followed by a dozen digits and a letter, and its
 * octet read `11` two ways. scripts/phone-pattern.mjs builds the phone pattern from 0.1's pieces,
 * and says how.
 * `\d`, `\w` and `\b` are ASCII-only here, as in Python with `re.ASCII`.
 */
export const PATTERNS: Record<PatternName, RegExp> = {
  email: /(?:(?<![A-Za-z0-9._%+-])(?!(?<=\\)(?:[nrtfb]|(?:u[0-9A-Fa-f]{4}|x[0-9A-Fa-f]{2}|U[0-9A-Fa-f]{8})(?!%40)[A-Za-z0-9._%+-]))|(?<=\\[nrtfb])|(?<=\\u[0-9A-Fa-f]{4})|(?<=\\x[0-9A-Fa-f]{2})|(?<=\\U[0-9A-Fa-f]{8}))[A-Za-z0-9._%+-]+(?:@|%40)[A-Za-z0-9.-]{1,253}\.[A-Za-z]{2,24}/g,
  phone: /\+(?=[1-9](?:[ .()-]{0,2}\d){5})(?:\d{1,3}(?:[ .-]\(?|\()(?:\d{1,4}(?:\)[ .-]?|[ .-])(?:\d{2,4}[ .-](?:\d{2,4}(?:[ .-](?:\d{2,4}(?:[ .-]\d{2,4}\b|\b)|\d{4,8}\b)|\b)|\d{4,8}(?:[ .-]\d{2,4}\b|\b)|\d{6,12}\b)|\d{4,8}(?:[ .-](?:\d{2,4}(?:[ .-]\d{2,4}\b|\b)|\d{4,8}\b)|\b)|\d{6,12}(?:[ .-]\d{2,4}\b|\b)|\d{8,16}\b)|\d{3,8}[ .-](?:\d{2,4}(?:[ .-](?:\d{2,4}(?:[ .-]\d{2,4}\b|\b)|\d{4,8}\b)|\b)|\d{4,8}(?:[ .-]\d{2,4}\b|\b)|\d{6,12}\b)|\d{5,12}(?:[ .-](?:\d{2,4}(?:[ .-]\d{2,4}\b|\b)|\d{4,8}\b)|\b)|\d{7,16}(?:[ .-]\d{2,4}\b|\b)|\d{9,20}\b)|\d{2,7}(?:\)[ .-]?|[ .-])(?:\d{2,4}[ .-](?:\d{2,4}(?:[ .-](?:\d{2,4}(?:[ .-]\d{2,4}\b|\b)|\d{4,8}\b)|\b)|\d{4,8}(?:[ .-]\d{2,4}\b|\b)|\d{6,12}\b)|\d{4,8}(?:[ .-](?:\d{2,4}(?:[ .-]\d{2,4}\b|\b)|\d{4,8}\b)|\b)|\d{6,12}(?:[ .-]\d{2,4}\b|\b)|\d{8,16}\b)|\d{4,11}[ .-](?:\d{2,4}(?:[ .-](?:\d{2,4}(?:[ .-]\d{2,4}\b|\b)|\d{4,8}\b)|\b)|\d{4,8}(?:[ .-]\d{2,4}\b|\b)|\d{6,12}\b)|\d{6,15}(?:[ .-](?:\d{2,4}(?:[ .-]\d{2,4}\b|\b)|\d{4,8}\b)|\b)|\d{8,19}(?:[ .-]\d{2,4}\b|\b)|\d{10,23}\b)|\(\d{3}\) ?\d{3}-\d{4}\b|(?:\b|(?<=\\[nrtfb0aevNLP_])|(?<=\\u[0-9A-Fa-f]{4})|(?<=\\x[0-9A-Fa-f]{2})|(?<=\\U[0-9A-Fa-f]{8}))\d{3}-\d{3}-\d{4}\b/g,
  card: /(?<!\d\.)(?:\b|(?<=\\[nrtfb0aevNLP_])|(?<=\\u[0-9A-Fa-f]{4})|(?<=\\x[0-9A-Fa-f]{2})|(?<=\\U[0-9A-Fa-f]{8}))(?:(?:[3-6]\d{3}|2(?:20[0-4]|22[1-9]|2[3-9]\d|[3-6]\d\d|7[01]\d|720))(?:[ -]?\d){11,12}\b(?<![ -]\d)(?<![ -]\d\d)|3[068]\d\d(?:[ -]?\d){10}\b)/g,
  secret: new RegExp(
    String.raw`(?:\b|(?<=\\[nrtfb0aevNLP_])|(?<=\\u[0-9A-Fa-f]{4})|(?<=\\x[0-9A-Fa-f]{2})|(?<=\\U[0-9A-Fa-f]{8}))(?:lsq_[A-Za-z0-9_-]{40,}|sk-[A-Za-z0-9_-]{20,}|[rs]k_(?:live|test)_[A-Za-z0-9]{16,}|whsec_[A-Za-z0-9]{24,}|gh[opsur]_[A-Za-z0-9]{36,}|github_pat_[A-Za-z0-9_]{20,}|glpat-[A-Za-z0-9_-]{20,}|npm_[A-Za-z0-9]{36}|xox[a-z]-[A-Za-z0-9%+\/=-]{10,}|xapp-[A-Za-z0-9-]{10,}|hooks\.slack(?:-gov)?\.com\\{0,2}\/(?:services|triggers|workflows|commands|actions)\\{0,2}\/[A-Za-z0-9\/\\_-]{20,}|(?:AKIA|ASIA)[0-9A-Z]{16}|(?:aws_secret_access_key|AWS_SECRET_ACCESS_KEY)(?:\\{0,7}["'])?[${WS}]*[:=][${WS}]*(?:\\{0,7}["'])?(?:[A-Za-z0-9\/+]|\\{1,2}\/){40}|AIza[0-9A-Za-z_-]{35}|ya29\.[A-Za-z0-9._-]{20,}|GOCSPX-[A-Za-z0-9_-]{20,}|phx_[A-Za-z0-9]{20,})|(?:(?<![A-Za-z0-9_-])|(?<=\\[nrtfb0aevNLP_])|(?<=\\u[0-9A-Fa-f]{4})|(?<=\\x[0-9A-Fa-f]{2})|(?<=\\U[0-9A-Fa-f]{8}))eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}|(?:[Bb]earer|BEARER)[${WS}]+(?=[A-Za-z0-9._~+/-]{0,512}\d)[A-Za-z0-9._~+/-]{16,}=*|(?:\b|(?<=\\[nrtfb0aevNLP_])|(?<=\\u[0-9A-Fa-f]{4})|(?<=\\x[0-9A-Fa-f]{2})|(?<=\\U[0-9A-Fa-f]{8}))(?:Basic|BASIC)[${WS}]+(?=[A-Za-z0-9+/]{0,512}[0-9+=])[A-Za-z0-9+/]{16,}={0,2}|(?:(?<=:\/\/)|(?<=:\\\/\\\/)|(?<=:\\\\\/\\\\\/))(?:(?!',|,')[^${WS}/:?#"]){0,256}:[^${WS}/?#"]{1,256}(?=@)|-----BEGIN [A-Z ]{0,32}PRIVATE KEY-----(?:[^-]|-(?!----))*?(?:-----END [A-Z ]{0,32}PRIVATE KEY-----|(?:-----[A-Z ]{0,48}-{0,4})?(?![\d\D]))`,
    'g',
  ),
  ip: /(?:\b|(?<=\\[nrtfb0aevNLP_])|(?<=\\u[0-9A-Fa-f]{4})|(?<=\\x[0-9A-Fa-f]{2})|(?<=\\U[0-9A-Fa-f]{8}))(?:25[0-5]|2[0-4]\d|1\d\d|\d\d?)(?:\.(?:25[0-5]|2[0-4]\d|1\d\d|\d\d?)){3}\b|(?:(?<![0-9A-Za-z:.])|(?<=\\[nrtfb0aevNLP_])|(?<=\\u[0-9A-Fa-f]{4})|(?<=\\x[0-9A-Fa-f]{2})|(?<=\\U[0-9A-Fa-f]{8}))(?:(?:[0-9A-Fa-f]{1,4}:){7}[0-9A-Fa-f]{1,4}|(?:[0-9A-Fa-f]{1,4}:){1,7}:|(?:[0-9A-Fa-f]{1,4}:){1,6}:[0-9A-Fa-f]{1,4}|(?:[0-9A-Fa-f]{1,4}:){1,5}(?::[0-9A-Fa-f]{1,4}){1,2}|(?:[0-9A-Fa-f]{1,4}:){1,4}(?::[0-9A-Fa-f]{1,4}){1,3}|(?:[0-9A-Fa-f]{1,4}:){1,3}(?::[0-9A-Fa-f]{1,4}){1,4}|(?:[0-9A-Fa-f]{1,4}:){1,2}(?::[0-9A-Fa-f]{1,4}){1,5}|[0-9A-Fa-f]{1,4}:(?::[0-9A-Fa-f]{1,4}){1,6}|:(?::[0-9A-Fa-f]{1,4}){1,7})(?![0-9A-Za-z:]|\.\d)/g,
}

/**
 * 0.1's secret key names. A key whose name ends with one of these, once lowercased and stripped of
 * everything but letters and digits, holds a secret whatever its value looks like: `dbPassword`,
 * `client_secret`, `X-Api-Key` and `AWS_SECRET_ACCESS_KEY` match, `apiKeyId`, `maxTokens` and
 * `author` don't. Part of the `secret` pattern. `redactKeys` adds more.
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

/**
 * What a secret looks like at its start, for one that runs into a cut and so can't be matched
 * whole: a known key prefix or a JWT's `eyJ`, at the start of the token the cut ends.
 */
const SECRET_START =
  /^(?:lsq_|sk-|[rs]k_(?:live|test)_|whsec_|gh[opsur]_|github_pat_|glpat-|npm_|xox[a-z]-|xapp-|hooks\.slack(?:-gov)?\.com\\{0,2}\/|AKIA|ASIA|AIza|ya29\.|GOCSPX-|phx_|eyJ)/
/** A Bearer or Basic token's scheme, just before the token the cut ends. */
const AUTH_SCHEME = new RegExp(`(?:[Bb]earer|BEARER|Basic|BASIC)[${WS}]+$`)
const PEM_BEGIN = /^-----BEGIN [A-Z ]{0,32}(?:PRIVATE KEY-----|$)/
/** A PEM key's closing line, whole: until one follows its BEGIN line, the block runs into the cut. */
const PEM_END = /-----END [A-Z ]{0,32}PRIVATE KEY-----/
const TOKEN_CHAR = /[A-Za-z0-9._~+/=-]/
/** What's after the backslash of an escape a value can start straight after. */
const WRITTEN_ESCAPE = /^(?:[nrtfb0aevNLP_]|u[0-9A-Fa-f]{4}|x[0-9A-Fa-f]{2}|U[0-9A-Fa-f]{8})/
/** Inside a run of token characters, a secret can start after one of these, as the patterns allow. */
const SECRET_BOUNDARY = /[.~+/=]/
/** Matched values longer than this are numbered by a hash, so a pending event doesn't keep them. */
const MAX_KEPT_IDENTITY = 64

export function normalizeKey(key: string): string {
  return key.toLowerCase().replace(/[^a-z0-9]/g, '')
}

export function resolvePatterns(option: false | Partial<Record<PatternName, boolean>> | undefined): PatternName[] {
  if (option === false) return []
  // Never from Object.prototype, so a polluted prototype can't turn a pattern off.
  const settings = readSettings(option, ORDER)
  return ORDER.filter((pattern) => settings[pattern] !== false)
}

/**
 * `redactKeys`, as the names are matched (lowercased, stripped to `a-z0-9`), and whether any entry
 * wasn't a key name; undefined when the value isn't a list of names at all. An array, or any other
 * iterable of strings (a Set, a Map's keys, a generator), of which the first 1,000 entries are
 * read; one string is one name. An object isn't read. An iterable that throws throws. Each value it
 * reads is read once and silenced, as an option is, before anything else is done with it: an
 * array's length and entries; the iterator method, the iterator, its `next` and `return` methods,
 * what they return, and each result's `done` and `value`. A getter's rejected promise, or one an
 * iterator method made async by mistake returns, would otherwise crash the app.
 */
export function readKeys(option: unknown): { keys: string[]; skipped: boolean } | undefined {
  const read = { keys: [] as string[], skipped: false }
  const add = (key: unknown) => {
    silence(key)
    const name = typeof key === 'string' ? normalizeKey(key) : ''
    if (name === '') read.skipped = true
    else if (!read.keys.includes(name)) read.keys.push(name)
  }
  if (option === undefined || option === null) return read
  if (typeof option === 'string') {
    add(option)
  } else if (Array.isArray(option)) {
    // Read once: an Array Proxy's length is the app's code too.
    const length: unknown = option.length
    silence(length)
    const end = typeof length === 'number' ? Math.min(length, 1_000) : 0
    for (let i = 0; i < end; i++) add(option[i])
  } else if (typeof option === 'object') {
    // As JavaScript's own iteration does: the method read once, then the iterator's `next`.
    const iterate: unknown = (option as Partial<Iterable<unknown>>)[Symbol.iterator]
    silence(iterate)
    if (typeof iterate !== 'function') return undefined
    const iterator: unknown = iterate.call(option)
    silence(iterator)
    if ((typeof iterator !== 'object' && typeof iterator !== 'function') || iterator === null) throw new TypeError('An iterator must be an object.')
    const next: unknown = (iterator as Partial<Iterator<unknown>>).next
    silence(next)
    if (typeof next !== 'function') throw new TypeError("An iterator's next must be a function.")
    let done = false
    try {
      for (let i = 0; i < 1_000; i++) {
        const step: unknown = next.call(iterator)
        silence(step)
        // As JavaScript's own iteration does: a result that isn't an object is an error.
        if (typeof step !== 'object' || step === null) throw new TypeError('An iterator result must be an object.')
        const ended: unknown = (step as IteratorResult<unknown>).done
        silence(ended)
        done = ended === true
        if (done) break
        add((step as IteratorResult<unknown>).value)
      }
    } finally {
      // An endless one, read up to the limit, is let go.
      if (!done) {
        const close: unknown = (iterator as Partial<Iterator<unknown>>).return
        silence(close)
        if (typeof close === 'function') silence(close.call(iterator))
      }
    }
  } else {
    return undefined
  }
  return read
}

/** The names `readKeys` finds; none for a value that isn't a list of names. */
export function resolveKeys(option: unknown): string[] {
  return readKeys(option)?.keys ?? []
}

/** Whether a `userId` looks like an email address or phone number, which the SDK warns about. */
export function looksLikeContact(text: string): boolean {
  for (const pattern of [PATTERNS.email, PATTERNS.phone]) {
    pattern.lastIndex = 0
    const found = pattern.test(text)
    pattern.lastIndex = 0
    if (found) return true
  }
  return false
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

/** How a field is finalised. */
export interface Scope {
  /** Redact every string and key. Without it, only the strings that were cut are, and what's under secret key names in JSON a string holds. */
  all: boolean
  /** Keys come from the app's data: secret key names apply. */
  freeForm: boolean
  /** Numbers that look like card numbers are redacted (tool inputs). */
  cards: boolean
  /** The field must be a string: a file becomes `[binary, N bytes]`. */
  text: boolean
}

export const JSON_FIELD: Scope = { all: true, freeForm: true, cards: true, text: false }
export const TEXT_FIELD: Scope = { all: true, freeForm: false, cards: false, text: true }

/**
 * Redaction of one event. Placeholders are numbered per event: the same value gets the same number
 * everywhere, also across `beforeSend` when the same Redactor finishes the event.
 */
export class Redactor {
  /** No prototype: a polluted Object.prototype must not start a count. */
  readonly counts: Partial<Record<PatternName, number>> = Object.create(null)
  /**
   * What this SDK made of strings before `beforeSend`, as the hook got them, each with the tightest
   * limit it keeps its form under after the hook: a cut, the limit it was cut at; a string whose
   * secret values were replaced, its length before that, since placeholders may have made it longer
   * than the limit it fitted. Only what is still in the event, after `prune`.
   */
  readonly cuts = new Map<string, number>()
  private readonly used = new Map<PatternName, Set<number>>()
  private readonly next = new Map<PatternName, number>()
  private readonly assigned = new Map<string, number>()
  private readonly secretKeys: boolean
  private readonly keyNames: readonly string[]

  private readonly scanWork: ScanWork = { left: SCAN_WORK }
  /** What the scan made of strings before `beforeSend`, which isn't read again: a placeholder could be taken for part of what's around it. */
  private readonly scanned = new Set<string>()

  constructor(
    private readonly patterns: PatternName[],
    extraKeys: readonly string[] = [],
  ) {
    this.secretKeys = patterns.includes('secret')
    this.keyNames = [...SECRET_KEYS, ...extraKeys]
  }

  get enabled(): boolean {
    return this.patterns.length > 0
  }

  /** Records placeholders already in the event, so new ones never reuse their numbers. */
  reserve(value: Shaped | Json | undefined): void {
    if (typeof value === 'string') {
      this.reserveText(value)
    } else if (value instanceof Cut) {
      this.reserveText(value.window)
    } else if (value instanceof Secret) {
      // Its identity may hold placeholders, but it is sent as a new one: nothing to reserve.
    } else if (Array.isArray(value)) {
      for (const item of value) this.reserve(item)
    } else if (typeof value === 'object' && value !== null && !(value instanceof Binary) && !(value instanceof TooDeep) && !(value instanceof Secret)) {
      for (const [key, item] of Object.entries(value)) {
        this.reserveText(key)
        this.reserve(item)
      }
    }
  }

  private reserveText(text: string): void {
    if (!text.includes('[')) return
    for (const match of text.matchAll(EXISTING_PLACEHOLDER)) {
      const pattern = PATTERN_BY_LABEL.get(match[1] as string)
      if (pattern) this.usedNumbers(pattern).add(Number(match[2]))
    }
  }

  /**
   * Shaped JSON to JSON: cuts redacted and closed, secrets replaced, files and deep containers as
   * markers. Secret key names apply in free-form fields and in JSON held in a string even before the hook.
   */
  finalize(value: Shaped, scope: Scope): Json {
    if (value instanceof Cut) {
      const text = this.cutText(value.window, value.cut, value.scanned) + value.mark
      this.cuts.set(text, Math.min(this.cuts.get(text) ?? value.limit, value.limit))
      return text
    }
    // Couldn't be read whole: numbered on its own, never equated with another secret.
    if (value instanceof Secret) return this.placeholder('secret', value.identity ?? '', value.identity === undefined)
    if (value instanceof Binary) return scope.text ? `[binary, ${value.bytes} bytes]` : { omitted: 'binary', bytes: value.bytes }
    if (value instanceof TooDeep) return { omitted: 'depth' }
    if (typeof value === 'string') return scope.all ? this.string(value) : this.beforeHook(value)
    if (typeof value === 'number') return scope.all && scope.cards ? this.number(value) : value
    if (value === null || typeof value === 'boolean') return value
    if (Array.isArray(value)) return value.map((item) => this.finalize(item, scope))
    // An ordinary object, as `beforeSend` gets it, each key added as a literal adds it (see put()).
    const out: { [key: string]: Json } = {}
    for (const [key, item] of Object.entries(value)) {
      let name = scope.all ? this.text(key) : key
      // Two keys that redact to the same placeholder must not overwrite each other.
      if (scope.all && hasOwn(out, name)) name = this.text(key, true)
      put(
        out,
        name,
        scope.freeForm && this.secretKeys && this.isSecretKey(key) && !(item instanceof Secret)
          ? this.secretValue(plain(item))
          : this.finalize(item, scope),
      )
    }
    return out
  }

  /**
   * Keeps only what the event still holds once it has been fitted: the cuts in it, and the numbers
   * of the placeholders in it with what they stand for. A pending event then holds no more of what
   * redaction matched than what it still shows, and `retainedBytes` counts that.
   */
  prune(event: unknown): void {
    const strings = new Set<string>()
    const present = new Map<PatternName, Set<number>>()
    const note = (text: string) => {
      strings.add(text)
      if (!text.includes('[')) return
      for (const match of text.matchAll(EXISTING_PLACEHOLDER)) {
        const pattern = PATTERN_BY_LABEL.get(match[1] as string) as PatternName
        let numbers = present.get(pattern)
        if (!numbers) present.set(pattern, (numbers = new Set()))
        numbers.add(Number(match[2]))
      }
    }
    const walk = (value: unknown): void => {
      if (typeof value === 'string') note(value)
      else if (Array.isArray(value)) for (const item of value) walk(item)
      else if (typeof value === 'object' && value !== null) {
        for (const [key, item] of Object.entries(value)) {
          note(key)
          walk(item)
        }
      }
    }
    walk(event)
    for (const text of this.cuts.keys()) if (!strings.has(text)) this.cuts.delete(text)
    for (const text of this.scanned) if (!strings.has(text)) this.scanned.delete(text)
    for (const [key, n] of this.assigned) {
      const pattern = key.slice(0, key.indexOf('\u0000')) as PatternName
      if (!present.get(pattern)?.has(n)) this.assigned.delete(key)
    }
    this.used.clear()
    for (const [pattern, numbers] of present) this.used.set(pattern, numbers)
  }

  /** About what this Redactor keeps in memory beyond the event, for the queue's limits. */
  retainedBytes(): number {
    let bytes = 0
    for (const key of this.assigned.keys()) bytes += key.length * 2 + 64
    bytes += (this.cuts.size + this.scanned.size) * 64
    for (const numbers of this.used.values()) bytes += numbers.size * 16
    return bytes
  }

  /** For shaping: undefined when the `secret` pattern is off. */
  secretKeyMatcher(): ((key: string) => boolean) | undefined {
    return this.secretKeys ? (key) => this.isSecretKey(key) : undefined
  }

  isSecretKey(key: string): boolean {
    const name = normalizeKey(key)
    return this.keyNames.some((suffix) => name.endsWith(suffix))
  }

  /**
   * The whole value under a secret key name, whatever its shape. Values that can't hold a secret
   * (null, booleans, empty values) and existing placeholders stay as they are.
   */
  secretValue(value: Json): Json {
    if (value === null || typeof value === 'boolean' || value === '') return value
    if (typeof value === 'string' && WHOLE_PLACEHOLDER.test(value)) return value
    if (typeof value === 'object' && Object.keys(value).length === 0) return value
    return this.placeholder('secret', typeof value === 'string' ? value : JSON.stringify(value), false)
  }

  /** A string value: what's under secret key names in JSON it holds, then the patterns. */
  string(input: string): string {
    return this.text(this.scanned.has(input) ? input : this.withoutSecretValues(input))
  }

  /** A string value before the hook: only what's under secret key names, which the hook never sees. */
  private beforeHook(input: string): string {
    const out = this.withoutSecretValues(input)
    if (mayHoldKey(out)) this.scanned.add(out)
    // One ending with a marker is read as any other marked string after the hook.
    if (out !== input && !isMarked(out)) {
      const [, length] = advance(input, 0, input.length)
      this.cuts.set(out, Math.min(this.cuts.get(out) ?? length, length))
    }
    return out
  }

  private withoutSecretValues(input: string): string {
    return this.windowWithoutSecretValues(input, input.length)[0]
  }

  /**
   * Replaces what's under secret key names in JSON held in `window`, which reads past the cut at
   * `cut`: the rewritten window and where the cut now is. A value starting past the cut is neither
   * replaced nor counted. One running past it is replaced whole, and its placeholder ends what's kept.
   */
  private windowWithoutSecretValues(window: string, cut: number): [string, number] {
    if (!this.secretKeys || !mayHoldKey(window)) return [window, cut]
    const spans = secretSpans(window, (key) => this.isSecretKey(key), this.scanWork)
    if (spans.length === 0) return [window, cut]
    let out = ''
    let from = 0
    let at = cut
    for (const span of spans) {
      if (span.start >= cut) break
      const placeholder = this.spanPlaceholder(span)
      out += window.slice(from, span.start) + placeholder
      from = span.end
      if (span.end > cut) return [out + window.slice(from), out.length]
      at += placeholder.length - (span.end - span.start)
    }
    return [out + window.slice(from), at]
  }

  private spanPlaceholder(span: SecretSpan): string {
    return span.quote + this.placeholder('secret', span.identity ?? '', span.identity === undefined) + span.quote
  }

  text(input: string, fresh = false): string {
    let out = input
    for (const pattern of this.patterns) {
      if (skip(pattern, out)) continue
      out = out.replace(PATTERNS[pattern], (match) => {
        if (rejected(pattern, match)) return match
        return this.placeholder(pattern, identity(pattern, match), fresh)
      })
    }
    return out
  }

  /**
   * Redacts what's kept of a cut string, reading `window` past the cut at `cut`: values under secret
   * key names in JSON it holds, then the patterns. A match that runs
   * past the cut is masked whole: its placeholder ends what's kept. Matches entirely past the cut
   * are neither replaced nor counted. Then a secret that starts like one and runs into the cut, or
   * past the window, is masked too.
   */
  cutText(window: string, cut: number, scanned = false): string {
    let [out, at] = scanned ? [window, cut] : this.windowWithoutSecretValues(window, cut)
    for (const pattern of this.patterns) {
      if (skip(pattern, out)) continue
      let shift = 0
      let straddled: number | undefined
      out = out.replace(PATTERNS[pattern], (match: string, offset: number) => {
        if (offset >= at || straddled !== undefined || rejected(pattern, match)) return match
        const placeholder = this.placeholder(pattern, identity(pattern, match), false)
        if (offset + match.length <= at) shift += placeholder.length - match.length
        else straddled = offset + shift + placeholder.length
        return placeholder
      })
      at = straddled ?? at + shift
    }
    return this.maskAtCut(out.slice(0, at))
  }

  /** A secret whose end was cut off: its kept part becomes the placeholder. */
  private maskAtCut(kept: string): string {
    if (!this.secretKeys) return kept
    const begin = kept.lastIndexOf('-----BEGIN ')
    if (begin !== -1 && PEM_BEGIN.test(kept.slice(begin, begin + 64)) && !PEM_END.test(kept.slice(begin))) {
      return kept.slice(0, begin) + this.placeholder('secret', kept.slice(begin), false)
    }
    let run = kept.length
    while (run > 0 && TOKEN_CHAR.test(kept[run - 1] as string)) run--
    // What ends an escape written in JSON, such as the `n` of `\\n`, isn't part of the token.
    if (kept[run - 1] === '\\') run += WRITTEN_ESCAPE.exec(kept.slice(run, run + 9))?.[0].length ?? 0
    // The earliest place in the run where a secret could start: its beginning, or just after a
    // character that ends one in the patterns, as `=` does in `token=eyJ…`.
    for (let start = run; start < kept.length; start++) {
      if (start > run && !SECRET_BOUNDARY.test(kept[start - 1] as string)) continue
      const token = kept.slice(start)
      const scheme = start === run && AUTH_SCHEME.test(kept.slice(Math.max(0, start - 64), start))
      if (SECRET_START.test(token) || scheme) return kept.slice(0, start) + this.placeholder('secret', token, false)
    }
    return kept
  }

  /** Card numbers stored as JSON numbers inside tool inputs. */
  private number(value: number): Json {
    if (!this.patterns.includes('card') || !Number.isInteger(value) || value <= 0) return value
    const digits = String(value)
    if (!/^(?:[3-6]\d{14,15}|2(?:20[0-4]|22[1-9]|2[3-9]\d|[3-6]\d\d|7[01]\d|720)\d{11,12}|3[068]\d{12})$/.test(digits) || !luhn(digits)) return value
    return this.placeholder('card', digits, false)
  }

  private placeholder(pattern: PatternName, id: string, fresh: boolean): string {
    // JSON escapes lone surrogates, which UTF-8 would replace: two values never share a hash for that.
    const identity = id.length > MAX_KEPT_IDENTITY ? `#${createHash('sha256').update(JSON.stringify(id), 'utf8').digest('hex')}` : id
    const key = `${pattern}\u0000${identity}`
    let n = fresh ? undefined : this.assigned.get(key)
    if (n === undefined) {
      n = this.nextFree(pattern)
      if (!fresh) this.assigned.set(key, n)
    }
    this.counts[pattern] = (this.counts[pattern] ?? 0) + 1
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

export { plain }

/** Cheap checks that let a pattern skip text it can't match. */
function skip(pattern: PatternName, text: string): boolean {
  if (pattern === 'email') return !text.includes('@') && !text.includes('%40')
  if (pattern === 'ip') return !text.includes('.') && !text.includes(':')
  return false
}

function rejected(pattern: PatternName, match: string): boolean {
  if (pattern === 'card') return !luhn(match)
  // IPv6 needs three groups: `::1`, `fe80::1` and code such as `data[::10]` or `A::B` stay.
  if (pattern === 'ip') return match.includes(':') && match.split(':').filter(Boolean).length < 3
  return false
}

/** What makes two matches "the same value" for numbering. */
function identity(pattern: PatternName, match: string): string {
  if (pattern === 'email') return match.toLowerCase().replaceAll('%40', '@')
  if (pattern === 'ip') return match.toLowerCase()
  if (pattern === 'card' || pattern === 'phone') return match.replace(/\D/g, '')
  return match
}
