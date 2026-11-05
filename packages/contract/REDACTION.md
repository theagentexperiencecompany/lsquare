# Redaction

What both SDKs redact before an event leaves the app, and the names the ingestion accepts in an
event's `redactions` counts. The SDKs' tests compare the two code blocks below with their own code,
and fail if they differ: `@lsquare/node` matches them exactly; `lsquare` 0.2 writes `\s` out in full
and rewrites the phone and ip patterns to match the same strings.

`capture()` processes each event in this order:

1. Turn the input into an event.
2. Run `beforeSend`, waiting for it if it returns a promise.
3. Check the hook's output: keep only the event's fields, check their types, restore the identity
   fields, and record anything removed in `dropped`.
4. Apply built-in redaction.
5. Enforce the size limit.

Running the checks and redaction after the hook means nothing the hook adds to a redacted field can
skip them.

Built-in redaction applies to every string value and every object key in the event, except at
these exact paths: `conversationId`, `turnId`, `orgId`, `userId`, `sessionId`, `apiKeyId`,
`toolName`, `surface`, `client`, `server`, `protocolVersion`, `toolCalls[n].name`, the same fields in
a call's `agent`, and the fields the SDK adds. Numbers inside `toolCalls[n].input`
are also checked against the card rule. Numbers anywhere else (`usage`, `latency`, timestamps,
counts) are never changed.

| Pattern | Placeholder | Rule |
|---|---|---|
| `email` | `[EMAIL_n]` | Standard address format, including URL-encoded (`%40`) and local parts over 64 characters |
| `phone` | `[PHONE_n]` | International format with a leading `+`, or US format (`(555) 123-4567`, `555-123-4567`). Bare digit runs are not matched: they would catch counts, dates and timestamps |
| `card` | `[CARD_n]` | 15 or 16 digits starting with 3 to 6, passing the Luhn check. A looser rule would match a millisecond timestamp |
| `secret` | `[SECRET_n]` | Known key formats only: LSquare's own `lsq_` keys, `sk-`, Stripe `sk_`/`rk_` live and test keys and `whsec_`, GitHub `ghp_`/`gho_`/`ghs_`/`ghu_`/`ghr_` and `github_pat_`, GitLab `glpat-`, npm `npm_`, Slack `xox`/`xapp-` tokens and webhook URLs, AWS `AKIA`/`ASIA` key IDs and `aws_secret_access_key=` values, Google `AIza`, `ya29.` and `GOCSPX-`, `phx_`, JWTs, `Bearer` (any of `Bearer`, `bearer`, `BEARER`) tokens containing a digit and `Basic` tokens containing a digit, `+` or `=` (so prose such as "the bearer responsibilities" is left alone), credentials in URLs (`scheme://user:password@` or `scheme://:password@`, ending at `?` or `#`), whole PEM private key blocks |
| `ip` | `[IP_n]` | IPv4 addresses, and IPv6 addresses with at least three groups. `::1`, `fe80::1`, times, ratios and code such as `data[::10]` or `A::B` are left alone |

The exact patterns:

```ts
const PATTERNS = {
  email: /(?<![A-Za-z0-9._%+-])[A-Za-z0-9._%+-]+(?:@|%40)[A-Za-z0-9.-]{1,253}\.[A-Za-z]{2,24}/g,
  phone: /\+[1-9]\d{0,2}[ .-]?\(?\d{1,4}\)?(?:[ .-]?\d{2,4}){2,4}\b|\(\d{3}\) ?\d{3}-\d{4}\b|\b\d{3}-\d{3}-\d{4}\b/g,
  card: /\b[3-6]\d{3}(?:[ -]?\d){11,12}\b/g, // and the digits must pass the Luhn check
  secret: /\b(?:lsq_[A-Za-z0-9_-]{40,}|sk-[A-Za-z0-9_-]{20,}|[rs]k_(?:live|test)_[A-Za-z0-9]{16,}|whsec_[A-Za-z0-9]{24,}|gh[opsur]_[A-Za-z0-9]{36,}|github_pat_[A-Za-z0-9_]{20,}|glpat-[A-Za-z0-9_-]{20,}|npm_[A-Za-z0-9]{36}|xox[abprs]-[A-Za-z0-9-]{10,}|xapp-[A-Za-z0-9-]{10,}|hooks\.slack\.com\/services\/[A-Za-z0-9\/_-]{20,}|(?:AKIA|ASIA)[0-9A-Z]{16}|(?:aws_secret_access_key|AWS_SECRET_ACCESS_KEY)["']?\s*[:=]\s*["']?[A-Za-z0-9\/+]{40}|AIza[0-9A-Za-z_-]{35}|ya29\.[A-Za-z0-9._-]{20,}|GOCSPX-[A-Za-z0-9_-]{20,}|phx_[A-Za-z0-9]{20,})|(?<![A-Za-z0-9_-])eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}|(?:[Bb]earer|BEARER)\s+(?=[A-Za-z0-9._~+/-]{0,512}\d)[A-Za-z0-9._~+/-]{16,}=*|\b(?:Basic|BASIC)\s+(?=[A-Za-z0-9+/]{0,512}[0-9+=])[A-Za-z0-9+/]{16,}={0,2}|(?<=:\/\/)[^\s/:@?#]{0,256}:[^\s/@?#]{1,256}(?=@)|-----BEGIN [A-Z ]{0,32}PRIVATE KEY-----(?:[^-]|-(?!----))*?-----END [A-Z ]{0,32}PRIVATE KEY-----/g,
  ip: /\b(?:25[0-5]|2[0-4]\d|1?\d?\d)(?:\.(?:25[0-5]|2[0-4]\d|1?\d?\d)){3}\b|(?<![0-9A-Za-z:.])(?:(?:[0-9A-Fa-f]{1,4}:){7}[0-9A-Fa-f]{1,4}|(?:[0-9A-Fa-f]{1,4}:){1,7}:|(?:[0-9A-Fa-f]{1,4}:){1,6}:[0-9A-Fa-f]{1,4}|(?:[0-9A-Fa-f]{1,4}:){1,5}(?::[0-9A-Fa-f]{1,4}){1,2}|(?:[0-9A-Fa-f]{1,4}:){1,4}(?::[0-9A-Fa-f]{1,4}){1,3}|(?:[0-9A-Fa-f]{1,4}:){1,3}(?::[0-9A-Fa-f]{1,4}){1,4}|(?:[0-9A-Fa-f]{1,4}:){1,2}(?::[0-9A-Fa-f]{1,4}){1,5}|[0-9A-Fa-f]{1,4}:(?::[0-9A-Fa-f]{1,4}){1,6}|:(?::[0-9A-Fa-f]{1,4}){1,7})(?![0-9A-Za-z:]|\.\d)/g, // and an IPv6 address needs at least three groups
}
```

Secret key names catch what the patterns can't: a secret in structured data has no recognisable
format, but its key gives it away. Inside `toolCalls[n].input`, `latency`, `verification` and
`metadata`, at any depth, a key whose name ends with one of these, once lowercased and stripped of
everything but letters and digits, has its whole value replaced by one `[SECRET_n]` placeholder,
counted as `secret`:

```ts
const SECRET_KEYS = [
  'password', 'passwords', 'passwd', 'passphrase', 'secret', 'secrets', 'token', 'auth', 'authorization',
  'apikey', 'apikeys', 'privatekey', 'secretkey', 'accesskey', 'signingkey', 'encryptionkey',
  'credential', 'credentials', 'cookie', 'cookies', 'connectionstring',
]
```

- `dbPassword`, `client_secret`, `X-Api-Key` and `AWS_SECRET_ACCESS_KEY` match. `apiKeyId`,
  `maxTokens`, `tokenCount` and `author` don't.
- Strings, numbers, objects and arrays are replaced whole. `null`, booleans, empty values and a value
  that is already exactly a placeholder are kept.
- The key stays, so LSquare still sees which field held a secret: `{ dbPassword: "…" }` becomes
  `{ dbPassword: "[SECRET_1]" }`.
- A secret found both under a key and by a pattern gets one number, like any repeated value.
- It is part of the `secret` pattern: `redact: { secret: false }` turns it off too. In `lsquare` and
  the Python SDK, `redactKeys` / `redact_keys` add names specific to an app. In `@lsquare/node` those
  are left to `beforeSend`.

The same rule applies to JSON held in a string, in every string value of an event: a text block, an
input, an error message, a question or an answer. `{"password":"hunter2"}` in a text block becomes
`{"password":"[SECRET_1]"}`, and the rest of the string stays exactly as it was. It is part of the
`secret` pattern as well. In `lsquare` and the Python SDK, `redactKeys` / `redact_keys` names apply
too, and `beforeSend` / `before_send` never sees these values. `@lsquare/node` has no such option,
and runs its hook first.

- Values are found by scanning the string, which is never parsed and rewritten: a key is text before
  a quote that a colon follows. So this also works in JSON inside prose or a code fence, in most
  malformed JSON, in JSON printed once by Python's `repr` or Node's `util.inspect`, in YAML's
  double-quoted strings, and in a string cut at its end. JSON printed twice, or printed inside four
  levels of encoding, can keep part of a value. A string cut before its key's end keeps what shows of
  the value.
- JSON encoded inside a string is read too, up to four times over. Past that, a string still holding
  a key (a quote, then a colon) is replaced whole.
- The same values are replaced and kept as in structured data. Only a value spelled exactly as a
  placeholder, with no escapes, counts as already replaced; it ends there, whatever follows.
- A quoted value ends at a quote followed by the string's end, a line's end, a comma that a value or
  a line's end follows, a closing bracket with no letter or digit straight after it, or one or more of
  `. ; ) ' `` ` `` and then a space or a line's end. Any other quote is read with it, and so is text
  after it, up to a line that starts with a quote or a closing bracket: `"password": "abc" and more`,
  `"password": "pa",ss"`. So a quote in a value followed by one of those ends it early, and what's
  after that stays: `"password": "pa"} ss"`. In text that isn't valid JSON, a value ending in a
  backslash can take what follows it.
- Any other value is replaced unless its key comes straight after a word (two ASCII letters), `=`,
  `->`, `=>`, or a period after a letter or a quote, where it is code or a sentence quoting something.
  So `"password": correct horse` is replaced, up to the end of its line or the next `,`, `}` or `]`,
  but `Post "https://x/token": timeout`, `case "token": return x` and `x == "token": y` stay. A string
  or container that never closes, or whose brackets don't match, is replaced up to the end of the
  string.
- A string is numbered by its decoded value, so a text block and its `structuredContent` copy share
  a number when both were read whole. A number or container in a string may get a number of its own,
  as may a printed value holding any escape.
- Keys in JSON held in a string are read as text, as parsing would read them, and only the last 256
  characters of a long one. So JSON used as a key has its values replaced, which can make two keys the
  same. A printed key is read through its escapes (`'{"password\\n": "x"}'`). Text that ends in a
  space, a tab or one of `= ( [ + ? , . < | &` is taken for code or a sentence and isn't a key, unless
  it opens with a quote straight after `{` or `,` or at the start of a line or the string, as an
  object's keys do: `token == ":"` and `createToken(":", a)` stay. Nor is text after a `?` touching
  its quote, or with a space before its colon: `cond ? "token" : "x"`. Other text that ends in a secret
  key name and comes before a quote and a colon can still be read as a key, and what follows it
  replaced. A `redactKeys` name that a number, `true`, `false` or `null` ends with can also match the
  text between two strings. The event's own object keys get the patterns only, as do `error.type` and
  `error.code` in `lsquare` and the Python SDK.
- A placeholder can be longer than what it replaces, so an event dense with short values can grow
  past its size limit, and then loses parts or is dropped, as any event over it does.
- Other syntax isn't covered: `password=hunter2`, `password: hunter2`, an unquoted key
  (`{password: "x"}`) or Python's `{'password': 'x'}`, which is how pydantic's validation errors quote
  an input. Nor are quotes written another way (CSV's `""`, HTML's `&quot;`, URL encoding, `\x22`),
  colours between a key and its colon, a key and its value split across two strings or lines with a
  border or prefix between them, a name and its value as separate strings
  (`{"name": "DB_PASSWORD", "value": "x"}`), or a copy of the value that isn't under a secret key
  name. `beforeSend` / `before_send` handles those, as do the wrapper's `inputs` / `results` options.

Placeholders are numbered per event:

- The first distinct email becomes `[EMAIL_1]`, the second `[EMAIL_2]`, and the same value gets the
  same number everywhere in that event. Different people therefore stay distinct within a turn.
- Numbering skips any placeholder that already appears in the event, so a replaced object key can't
  collide with an existing key or overwrite its value.
- Numbers aren't linked across events: `[EMAIL_1]` in two events can be two different people.

Other rules:

- Every pattern is on by default. An app can turn one off (`redact: { ip: false }`) or all of them
  (`redact: false`).
- The SDK records how many replacements it made in `redactions`, so LSquare knows the text was
  changed.
- Redaction fails closed: if it throws, the event is dropped and reported to `onError` without the
  event, never sent unredacted. That covers errors, not missed values: the patterns only catch the
  formats above.
- There is no generic detection of random-looking strings, which would erase ordinary IDs.
- Names, company names, usernames and domains are not detected: patterns can't find them
  reliably, and a model is too slow for an SDK. An app handles them in `beforeSend`.
- Also not detected: phone numbers without a `+` or US punctuation (`555 123 4567`, `07700 900123`),
  which bare-digit rules would confuse with counts; secrets in text without a known format, such as
  `password=hunter2` in a question (in structured data and JSON, their key name gives them away);
  IBANs and national ID numbers. An IBAN rule needs a new pattern name, which the ingestion must accept in
  `redactions` before an SDK can send it.

Redaction runs on the app's event loop, so every pattern matches in linear time, as does finding
values in JSON held in a string: emails, JWTs and IPv6 addresses may only start at a token boundary,
so there is one attempt per run of token characters; a PEM body can't run past the next `-----`, a
URL's credentials past the next `/` or `@`, and the `Basic` check looks at most 512 characters ahead.
Secrets are not length-bounded, so long credentials (a 9 KB JWT, a 12 KB key) are still caught.
Placeholder numbers come from a running counter per pattern, so 10,000 distinct emails are numbered
in linear time. Finding values in JSON held in a string has a limit per event, covering both passes
when there is a hook: 1,000,000, counted as one for each quote, backslash or bracket read, each JSON
value checked, and each 16 characters decoded. Ordinary text in an event that fits the size limit
doesn't reach it (256 KB of JSON in JSON in JSON takes about 550,000), though about 1 MB of it can, as
can crafted text in an event of any size. Past it, a string holding a quote and a colon is replaced
whole and counted as a secret, so nothing unread is sent. That bounds the slowest event found, on
crafted text, to about 800 ms in Python and 140 ms in TypeScript, of which about 400 ms and 30 ms is
everything but this step. One string read to its 24,000-character window takes at most about 110 ms
and 20 ms, and 256 KB of ordinary JSON about 20 to 75 ms and 2 to 7 ms, depending on how many escapes
it holds.
In `@lsquare/node`, 0.1's phone pattern on crafted input (`+` followed by long digit runs) takes
about 50 ms for a 256 KB event, and ordinary text without JSON under 1 ms.
