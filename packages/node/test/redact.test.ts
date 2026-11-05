import { describe, expect, it } from 'vitest'
import { luhn, redactBody, resolvePatterns } from '../src/redact.js'
import type { Json } from '../src/types.js'

const all = resolvePatterns(undefined)

function redact(body: Record<string, unknown>, patterns = all) {
  return redactBody(body, patterns)
}

describe('patterns', () => {
  it.each([
    ['email', 'write to jane.doe@acme.io today', 'write to [EMAIL_1] today'],
    ['phone', 'call +44 20 7946 0958', 'call [PHONE_1]'],
    ['phone', 'or (555) 123-4567', 'or [PHONE_1]'],
    ['card', 'card 4111 1111 1111 1111', 'card [CARD_1]'],
    ['secret', 'key sk-abcdefghijklmnopqrstuvwx', 'key [SECRET_1]'],
    ['secret', `mine is lsq_${'Ab9_-'.repeat(8)}xyz, careful`, 'mine is [SECRET_1], careful'],
    ['secret', 'Authorization: Bearer abcdefghijklmnop1234', 'Authorization: [SECRET_1]'],
    ['secret', 'jwt eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dozjgNryP4J3jVmNHl0w5N_XgL0n3I9PlFUP0THsR8U', 'jwt [SECRET_1]'],
    ['secret', 'k -----BEGIN RSA PRIVATE KEY-----\nMIIEow\nabc\n-----END RSA PRIVATE KEY----- end', 'k [SECRET_1] end'],
    ['secret', `token ya29.a0AfH6SM${'b'.repeat(40)}`, 'token [SECRET_1]'],
    ['secret', `client GOCSPX-${'c'.repeat(28)}`, 'client [SECRET_1]'],
    ['secret', `oauth gho_${'d'.repeat(36)}`, 'oauth [SECRET_1]'],
    ['secret', `app ghs_${'e'.repeat(36)}`, 'app [SECRET_1]'],
    ['secret', 'sts ASIAABCDEFGHIJKLMNOP', 'sts [SECRET_1]'],
    ['secret', `aws_secret_access_key = ${'f'.repeat(40)}`, '[SECRET_1]'],
    ['secret', `stripe sk_test_${'g'.repeat(24)}`, 'stripe [SECRET_1]'],
    ['secret', `hook whsec_${'h'.repeat(32)}`, 'hook [SECRET_1]'],
    ['secret', `slack xapp-1-A0${'i'.repeat(20)}`, 'slack [SECRET_1]'],
    ['secret', `https://hooks.slack.com/services/T0AAAA/B0BBBB/${'j'.repeat(24)}`, 'https://[SECRET_1]'],
    ['secret', `npm npm_${'k'.repeat(36)}`, 'npm [SECRET_1]'],
    ['secret', `gitlab glpat-${'l'.repeat(20)}`, 'gitlab [SECRET_1]'],
    ['secret', 'authorization: bearer abcdefghijklmnop1234', 'authorization: [SECRET_1]'],
    ['secret', 'Authorization: Basic YWRtaW46aHVudGVyMi1zZWNyZXQ=', 'Authorization: [SECRET_1]'],
    ['secret', 'postgres://admin:hunter2@db.internal/app', 'postgres://[SECRET_1]@db.internal/app'],
    ['secret', 'REDIS_URL=redis://:s3cretPassw0rd@localhost:6379', 'REDIS_URL=redis://[SECRET_1]@localhost:6379'],
    ['email', `write to ${'a.'.repeat(40)}jane@acme.io`, 'write to [EMAIL_1]'],
    ['email', 'GET /users?email=jane.doe%40acme.io', 'GET /users?email=[EMAIL_1]'],
    ['ip', 'from 192.168.1.20', 'from [IP_1]'],
    ['ip', 'from 2001:db8:85a3::8a2e:370:7334 today', 'from [IP_1] today'],
    ['ip', 'gateway 2001:db8::1.', 'gateway [IP_1].'],
  ])('%s: %s', (pattern, text, expected) => {
    const { value, counts } = redact({ question: text })
    expect(value.question).toBe(expected)
    expect(counts).toEqual({ [pattern]: 1 })
  })

  it('leaves ordinary text alone', () => {
    const text =
      'ref Rk8QwE3rTu6IoP1aSd5F7h on 2026-03-02, 41208 tokens, 3 sessions, t=1187, ' +
      'stamp 1790000000123, total 0.2750399999999999, phone-like 5551234567, card-like 4111111111111112, ' +
      'at 10:30:45, ratio 3:2:1, Foo::Bar and std::vector, a :: b, basic statistics, Basic plan, ' +
      'the bearer of bad news, see https://example.com:8080/path and ws://host:9000/feed, 100%, ' +
      // Code and prose the newer rules must leave alone.
      'rows = data[::10], xs[0::2], A::B.new, Ab::Cd, BAD:: ok, loopback ::1 and fe80::1, ' +
      'Basic Authentication/Authorization guide, the bearer responsibilities are clear, ' +
      'https://example.com:8080?x=a@b'
    const { value, counts } = redact({ answer: text })
    expect(value.answer).toBe(text)
    expect(counts).toEqual({})
  })

  it('checks cards with Luhn', () => {
    expect(luhn('4111111111111111')).toBe(true)
    expect(luhn('4111111111111112')).toBe(false)
  })
})

describe('numbering', () => {
  it('gives the same value the same number across fields, and distinct values distinct numbers', () => {
    const { value } = redact({
      question: 'Is jane@acme.io the same as bob@acme.io?',
      answer: 'JANE@acme.io is not bob@acme.io',
    })
    expect(value.question).toBe('Is [EMAIL_1] the same as [EMAIL_2]?')
    expect(value.answer).toBe('[EMAIL_1] is not [EMAIL_2]')
  })

  it('gives URL-encoded and plain forms of one email the same number', () => {
    const { value } = redact({ question: 'a%40b%40example.com is a%40b@example.com' })
    expect(value.question).toBe('[EMAIL_1] is [EMAIL_1]')
  })

  it('skips numbers of placeholders already in the event', () => {
    const { value } = redact({ question: 'Earlier I wrote [EMAIL_1], now jane@acme.io' })
    expect(value.question).toBe('Earlier I wrote [EMAIL_1], now [EMAIL_2]')
  })

  it('redacts object keys without collisions', () => {
    const { value } = redact({ verification: { 'jane@acme.io': 1, 'JANE@ACME.IO': 2, '[EMAIL_1]': 3 } })
    expect(value.verification).toEqual({ '[EMAIL_2]': 1, '[EMAIL_3]': 2, '[EMAIL_1]': 3 })
  })
})

describe('scope', () => {
  it('never changes identifiers', () => {
    const body = {
      conversationId: 'conv1',
      orgId: 'org_1',
      toolName: 'ask_docs',
      apiKeyId: 'key-jane@acme.io',
      client: { name: 'agent jane@acme.io' },
      question: 'jane@acme.io',
    }
    const { value } = redact(body)
    expect(value).toEqual({ ...body, question: '[EMAIL_1]' })
  })

  it('redacts tool inputs and error messages, but not tool names', () => {
    const { value } = redact({
      toolCalls: [{ name: 'lookup_jane@acme.io', input: { email: 'jane@acme.io', card: 4111111111111111 }, errorMessage: 'no jane@acme.io' }],
    })
    expect(value.toolCalls).toEqual([
      { name: 'lookup_jane@acme.io', input: { email: '[EMAIL_1]', card: '[CARD_1]' }, errorMessage: 'no [EMAIL_1]' },
    ])
  })

  it('never changes numbers outside tool inputs', () => {
    const { value } = redact({ usage: { inputTokens: 4111111111111111 }, latency: { ms: 4111111111111111 } })
    expect(value).toEqual({ usage: { inputTokens: 4111111111111111 }, latency: { ms: 4111111111111111 } })
  })

  it('can be turned off, entirely or per pattern', () => {
    const text = 'jane@acme.io from 10.0.0.1'
    expect(redact({ question: text }, resolvePatterns(false)).value.question).toBe(text)
    expect(redact({ question: text }, resolvePatterns({ ip: false })).value.question).toBe('[EMAIL_1] from 10.0.0.1')
  })
})

// Security review finding 1: a secret in structured data has no recognisable format, but its key
// name gives it away.
describe('secret key names', () => {
  const inInput = (input: Json) => redact({ toolCalls: [{ name: 'tool', input }] })
  const inputOf = (value: Record<string, unknown>) => (value.toolCalls as Array<{ input: Json }>)[0]?.input

  it("catches the security review's reproduction", () => {
    const secret = 'aB3/'.repeat(10)
    const { value } = redact({
      question: `AWS_SECRET_ACCESS_KEY=${secret}`,
      toolCalls: [{ name: 'test', input: { AWS_SECRET_ACCESS_KEY: secret } }],
    })
    expect(JSON.stringify(value)).not.toContain(secret)
  })

  it.each([
    'password', 'dbPassword', 'DB_PASSWORD', 'new-password', 'passwords', 'passwd', 'passphrase',
    'secret', 'client_secret', 'clientSecret', 'secrets', 'token', 'accessToken', 'refresh_token',
    'GITHUB_TOKEN', 'x-csrf-token', 'auth', 'basicAuth', 'oauth', 'Authorization', 'Proxy-Authorization',
    'apiKey', 'api_key', 'X-Api-Key', 'apiKeys', 'privateKey', 'private_key', 'secretKey',
    'AWS_SECRET_ACCESS_KEY', 'signingKey', 'encryptionKey', 'credential', 'credentials',
    'Cookie', 'Set-Cookie', 'cookies', 'connectionString',
  ])('replaces the value under %s', (key) => {
    const { value, counts } = inInput({ [key]: 'opaque-value-123', note: 'kept' })
    expect(inputOf(value)).toEqual({ [key]: '[SECRET_1]', note: 'kept' })
    expect(counts).toEqual({ secret: 1 })
  })

  it.each([
    'apiKeyId', 'keyId', 'key', 'sortKey', 'accessKeyId', 'maxTokens', 'tokenCount', 'inputTokens', 'token_type',
    'tokenizer', 'author', 'authorId', 'sessionId', 'secretQuestion', 'passwordMinLength', 'cookieConsent',
  ])('leaves the value under %s alone', (key) => {
    const { value, counts } = inInput({ [key]: 'opaque-value-123' })
    expect(inputOf(value)).toEqual({ [key]: 'opaque-value-123' })
    expect(counts).toEqual({})
  })

  it('replaces whole values of any shape, at any depth, keeping values that hold nothing', () => {
    const { value, counts } = inInput({
      db: { host: 'db.internal', password: 123456 },
      headers: [{ Authorization: 'opaque' }],
      credentials: { user: 'jane', pass: 'hunter2' },
      secrets: ['a', 'b'],
      token: null,
      apiKey: true,
      passwd: '',
      auth: {},
      cookies: [],
      secret: '[SECRET_7]',
    })
    expect(inputOf(value)).toEqual({
      db: { host: 'db.internal', password: '[SECRET_1]' },
      headers: [{ Authorization: '[SECRET_2]' }],
      credentials: '[SECRET_3]',
      secrets: '[SECRET_4]',
      token: null,
      apiKey: true,
      passwd: '',
      auth: {},
      cookies: [],
      secret: '[SECRET_7]',
    })
    expect(counts).toEqual({ secret: 4 })
  })

  it('gives a secret the same number under any key and in text', () => {
    const key = 'sk-abcdefghijklmnopqrstuvwx'
    const { value } = redact({
      question: `use ${key}`,
      toolCalls: [
        { name: 'a', input: { apiKey: key, token: 'other-opaque' } },
        { name: 'b', input: { openai_api_key: key } },
      ],
    })
    expect(value.question).toBe('use [SECRET_1]')
    expect(value.toolCalls).toEqual([
      { name: 'a', input: { apiKey: '[SECRET_1]', token: '[SECRET_2]' } },
      { name: 'b', input: { openai_api_key: '[SECRET_1]' } },
    ])
  })

  it('applies in latency, verification and metadata, never to the fields the SDK defines', () => {
    const body = {
      apiKeyId: 'key_1',
      usage: { inputTokens: 10 },
      error: { message: 'failed', type: 'AuthError' },
      latency: { token: 'a1' },
      verification: { checks: [{ secret: 'b2' }] },
      metadata: { sessionToken: 'c3', plan: 'pro' },
    }
    const { value, counts } = redact(body)
    expect(value).toEqual({
      ...body,
      latency: { token: '[SECRET_1]' },
      verification: { checks: [{ secret: '[SECRET_2]' }] },
      metadata: { sessionToken: '[SECRET_3]', plan: 'pro' },
    })
    expect(counts).toEqual({ secret: 3 })
  })

  it('is part of the secret pattern', () => {
    const body = { toolCalls: [{ name: 'tool', input: { password: 'hunter2' } }] }
    expect(redact(body, resolvePatterns({ secret: false })).value).toEqual(body)
    expect(redact(body, resolvePatterns(false)).value).toEqual(body)
    expect(inputOf(redact(body, resolvePatterns({ email: false })).value)).toEqual({ password: '[SECRET_1]' })
  })
})

describe('long secrets', () => {
  it('redacts a JWT longer than 8 KB', () => {
    const jwt = `eyJhbGciOiJIUzI1NiJ9.eyJ${'a'.repeat(9400)}.${'b'.repeat(43)}`
    expect(jwt.length).toBe(9468)
    const { value, counts } = redact({ question: `token ${jwt} end` })
    expect(value.question).toBe('token [SECRET_1] end')
    expect(counts).toEqual({ secret: 1 })
  })

  it('redacts a PEM block longer than 8 KB', () => {
    const pem = `-----BEGIN PRIVATE KEY-----\n${'A'.repeat(12000)}\n-----END PRIVATE KEY-----`
    expect(redact({ question: pem }).value.question).toBe('[SECRET_1]')
  })
})

/** Smallest of three timings, to reduce noise. */
function time(text: string): number {
  let best = Number.POSITIVE_INFINITY
  for (let i = 0; i < 3; i++) {
    const started = performance.now()
    redact({ answer: text })
    best = Math.min(best, performance.now() - started)
  }
  return best
}

describe('performance', () => {
  it('numbers 10,000 distinct emails in linear time', () => {
    const text = Array.from({ length: 10_000 }, (_, i) => `u${i}@acme.io`).join(' ')
    const started = performance.now()
    const { value, counts } = redact({ answer: text })
    expect(performance.now() - started).toBeLessThan(100)
    expect(counts).toEqual({ email: 10_000 })
    expect((value.answer as string).endsWith('[EMAIL_10000]')).toBe(true)
  })

  it('scales linearly: 4x the input takes well under 16x the time', () => {
    const generators: Record<string, (n: number) => string> = {
      distinctEmails: (n) => Array.from({ length: n / 12 }, (_, i) => `u${i}@acme.io`).join(' '),
      emailNoDomain: (n) => 'a'.repeat(n - 1) + '@',
      jwtStarts: (n) => 'eyJ'.repeat(n / 3),
      jwtMissingSegments: (n) => '.eyJaaaaaaaaaaaa'.repeat(n / 16),
      pemNoEnd: (n) => '-----BEGIN PRIVATE KEY-----'.repeat(n / 27),
      pemLongBodyNoEnd: (n) => '-----BEGIN PRIVATE KEY-----' + 'A'.repeat(n),
      cardStarts: (n) => '4111 '.repeat(n / 5),
      ipv6Compressed: (n) => ' 1::'.repeat(n / 4),
      ipv6GroupsBadEnd: (n) => (' ' + 'aaaa:'.repeat(7) + 'g').repeat(n / 37),
      urlCredentialsNoAt: (n) => '://a:'.repeat(n / 5),
      basicLetters: (n) => ('Basic ' + 'a'.repeat(600) + ' ').repeat(n / 607),
      encodedEmailNoDomain: (n) => ' a' + '%40a'.repeat(n / 4),
    }
    for (const [name, generate] of Object.entries(generators)) {
      const small = time(generate(64 * 1024))
      const large = time(generate(256 * 1024))
      expect(large / Math.max(small, 0.5), name).toBeLessThan(10)
    }
  })

  it('stays fast on long adversarial strings', () => {
    const size = 256 * 1024
    const inputs = [
      'a'.repeat(size - 1) + '@',
      'eyJ'.repeat(size / 3),
      'sk-'.repeat(size / 3),
      '-----END PRIVATE KEY-----' + '-----BEGIN PRIVATE KEY-----'.repeat(size / 27),
      '4'.repeat(size),
      '1.'.repeat(size / 2),
      '+1 '.repeat(size / 3),
      'Bearer '.repeat(size / 7),
    ]
    for (const text of inputs) {
      const started = performance.now()
      redact({ answer: text })
      expect(performance.now() - started).toBeLessThan(100)
    }
  })
})

describe('JSON in a string', () => {
  it('numbers a value by what it decodes to, and a value under a key is a secret before it is anything else', () => {
    const { value, counts } = redact({ question: '{"token":"a\\u0062c","auth":"abc"}', answer: '{"token": "bob@example.com"}' })
    expect(value).toEqual({ question: '{"token":"[SECRET_1]","auth":"[SECRET_1]"}', answer: '{"token": "[SECRET_2]"}' })
    expect(counts).toEqual({ secret: 3 })
  })

  it('replaces a string whole once the event has used its work', () => {
    const crafted = `":"{\\{${'\\'.repeat(7)}`.repeat(20_000)
    const { value, counts } = redact({ question: crafted, answer: '{"note":"not a secret"}', intent: 'no quote and colon here' })
    expect(value).toEqual({ question: '[SECRET_1]', answer: '[SECRET_2]', intent: 'no quote and colon here' })
    expect(counts).toEqual({ secret: 2 })
  })

  it('reads JSON printed by repr or util.inspect again, decoded', () => {
    const { value } = redact({ question: '{"password": "pa\\\\",ss"}', answer: '{"password"\\n: "hunter2"}' })
    expect(value).toEqual({ question: '{"password": "[SECRET_1]"}', answer: '{"password"\\n: "[SECRET_2]"}' })
  })

  it('replaces what is under a secret key name, in every string but the fields it skips', () => {
    const { value, counts } = redact({
      question: '{"password":"a"}',
      answer: 'Result: {"token": "b", "plan": "pro"}',
      toolCalls: [{ name: 't', input: { body: '{"apiKey":"c"}' }, errorMessage: '{"auth":"d"}' }],
      metadata: { body: '{"cookie":"e"}' },
      toolName: '{"password":"f"}',
    })
    expect(value).toEqual({
      question: '{"password":"[SECRET_1]"}',
      answer: 'Result: {"token": "[SECRET_2]", "plan": "pro"}',
      toolCalls: [{ name: 't', input: { body: '{"apiKey":"[SECRET_3]"}' }, errorMessage: '{"auth":"[SECRET_4]"}' }],
      metadata: { body: '{"cookie":"[SECRET_5]"}' },
      toolName: '{"password":"f"}',
    })
    expect(counts).toEqual({ secret: 5 })
  })

  it('reads JSON encoded in a string', () => {
    expect(redact({ answer: '{"result":"{\\"token\\":\\"abc\\"}"}' }).value).toEqual({ answer: '{"result":"{\\"token\\":\\"[SECRET_1]\\"}"}' })
  })

  it('reads JSON whose colon is encoded', () => {
    expect(redact({ answer: '"{\\"password\\"\\u003a\\"hunter2\\"}"' }).value).toEqual({ answer: '"{\\"password\\"\\u003a\\"[SECRET_1]\\"}"' })
  })

  it('gives a value one number, also when a pattern finds it', () => {
    const key = 'sk-abcdefghijklmnopqrstuvwxyz'
    expect(redact({ answer: `{"password":"${key}"} ${key}` }).value).toEqual({ answer: '{"password":"[SECRET_1]"} [SECRET_1]' })
  })

  it('leaves object keys to the patterns', () => {
    expect(redact({ metadata: { '{"token":"a"}': 1 } }).value).toEqual({ metadata: { '{"token":"a"}': 1 } })
  })

  it('gives a number its quotes, so the JSON stays valid', () => {
    expect(redact({ answer: '{"token":123}' }).value).toEqual({ answer: '{"token":"[SECRET_1]"}' })
  })

  it('gives two values that never close a number each', () => {
    expect(redact({ question: '{"token":"same', answer: '{"token":"same' }).value).toEqual({ question: '{"token":"[SECRET_1]', answer: '{"token":"[SECRET_2]' })
  })

  it('is off with the secret pattern', () => {
    const text = '{"password":"a"}'
    expect(redact({ answer: text }, resolvePatterns({ secret: false })).value).toEqual({ answer: text })
  })
})
