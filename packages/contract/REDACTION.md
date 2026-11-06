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
a call's `agent`, and the fields the SDK adds. Numbers inside `toolCalls[n].input`, a call's `input`
and its `result.structuredContent` are also checked against the card rule. In those, and in `latency`,
`verification`, `metadata` and the same fields in a call's `agent`, a number under a secret key name is
replaced like any value. Other numbers (`usage`, timestamps, counts) are never changed.

| Pattern | Placeholder | Rule |
|---|---|---|
| `email` | `[EMAIL_n]` | Standard address format, including URL-encoded (`%40`) and local parts over 64 characters |
| `phone` | `[PHONE_n]` | International format with a leading `+`, or US format (`(555) 123-4567`, `555-123-4567`). Bare digit runs are not matched: they would catch counts, dates and timestamps |
| `card` | `[CARD_n]` | 15 or 16 digits starting with 3 to 6, 2200 to 2204 or 2221 to 2720, or 14 starting with 30, 36 or 38, passing the Luhn check, and not straight after a digit and a period, as a float's fraction is. One or two digits after a space or `-`, as in an expiry, aren't read as part of 15 or 16 digits. Diners' 14 digits can end in a group of two, so `3056 9309 0259 04` goes, as do 12 or 13 digits starting 30, 36 or 38 with one or two more, if they pass the check. A looser rule would match a millisecond timestamp. Not detected: Visa's old 13 digits, UnionPay's 19, and brands starting otherwise, such as RuPay's 81 and 82 |
| `secret` | `[SECRET_n]` | Known key formats only: LSquare's own `lsq_` keys, `sk-`, Stripe `sk_`/`rk_` live and test keys and `whsec_`, GitHub `ghp_`/`gho_`/`ghs_`/`ghu_`/`ghr_` and `github_pat_`, GitLab `glpat-`, npm `npm_`, Slack `xox`/`xapp-` tokens and the webhook and response URLs on `hooks.slack.com` and `hooks.slack-gov.com`, AWS `AKIA`/`ASIA` key IDs and `aws_secret_access_key=` values, Google `AIza`, `ya29.` and `GOCSPX-`, `phx_`, JWTs, `Bearer` (any of `Bearer`, `bearer`, `BEARER`) tokens of 16 characters or more, not counting `=` padding, containing a digit and `Basic` tokens of 16 characters or more, not counting padding, containing a digit, `+` or `=` (so prose such as "the bearer responsibilities" is left alone), credentials in URLs (`scheme://user:password@` or `scheme://:password@`, with no part of them replaced if the password holds a raw `/`, `?`, `#`, `"` or space or is over 256 characters, if the user holds any of those or `',`, or if the slashes are written `\u002F` or with more than two backslashes, as they can be on Slack's URLs and AWS values too), PEM private key blocks, whole or cut off by the string's end, even inside their END line |
| `ip` | `[IP_n]` | IPv4 addresses, and IPv6 addresses with at least three groups. `::1`, `fe80::1`, times, ratios and code such as `data[::10]` or `A::B` are left alone |

A value can start straight after an escape written in a string, as `\n`, `\u201c`, `\x00` or `\U0001f511`
are in JSON or a `repr`, `\0`, `\a`, `\e` or `\v` in other languages' printers, or `\_`, `\N`, `\L` or `\P` in
YAML: what ends the escape isn't taken for part of the value. So in `\nsk-...` the key is found, and
`\n@click.command` isn't an email. An email differs in two ways. A `\u`, `\x` or `\U` escape straight before
`@` or `%40` reads as the address, since one can end that way: `jos\u00e9@example.com` goes from the `u`, and
`\u201c@click.command` goes too. And the other letters above read as part of it, as in
`CORP\alice@example.com`.

The exact patterns:

```ts
const PATTERNS = {
  email: /(?:(?<![A-Za-z0-9._%+-])(?!(?<=\\)(?:[nrtfb]|(?:u[0-9A-Fa-f]{4}|x[0-9A-Fa-f]{2}|U[0-9A-Fa-f]{8})(?!%40)[A-Za-z0-9._%+-]))|(?<=\\[nrtfb])|(?<=\\u[0-9A-Fa-f]{4})|(?<=\\x[0-9A-Fa-f]{2})|(?<=\\U[0-9A-Fa-f]{8}))[A-Za-z0-9._%+-]+(?:@|%40)[A-Za-z0-9.-]{1,253}\.[A-Za-z]{2,24}/g,
  phone: /\+[1-9]\d{0,2}[ .-]?\(?\d{1,4}\)?(?:[ .-]?\d{2,4}){2,4}\b|\(\d{3}\) ?\d{3}-\d{4}\b|(?:\b|(?<=\\[nrtfb0aevNLP_])|(?<=\\u[0-9A-Fa-f]{4})|(?<=\\x[0-9A-Fa-f]{2})|(?<=\\U[0-9A-Fa-f]{8}))\d{3}-\d{3}-\d{4}\b/g,
  card: /(?<!\d\.)(?:\b|(?<=\\[nrtfb0aevNLP_])|(?<=\\u[0-9A-Fa-f]{4})|(?<=\\x[0-9A-Fa-f]{2})|(?<=\\U[0-9A-Fa-f]{8}))(?:(?:[3-6]\d{3}|2(?:20[0-4]|22[1-9]|2[3-9]\d|[3-6]\d\d|7[01]\d|720))(?:[ -]?\d){11,12}\b(?<![ -]\d)(?<![ -]\d\d)|3[068]\d\d(?:[ -]?\d){10}\b)/g, // and the digits must pass the Luhn check
  secret: /(?:\b|(?<=\\[nrtfb0aevNLP_])|(?<=\\u[0-9A-Fa-f]{4})|(?<=\\x[0-9A-Fa-f]{2})|(?<=\\U[0-9A-Fa-f]{8}))(?:lsq_[A-Za-z0-9_-]{40,}|sk-[A-Za-z0-9_-]{20,}|[rs]k_(?:live|test)_[A-Za-z0-9]{16,}|whsec_[A-Za-z0-9]{24,}|gh[opsur]_[A-Za-z0-9]{36,}|github_pat_[A-Za-z0-9_]{20,}|glpat-[A-Za-z0-9_-]{20,}|npm_[A-Za-z0-9]{36}|xox[a-z]-[A-Za-z0-9%+\/=-]{10,}|xapp-[A-Za-z0-9-]{10,}|hooks\.slack(?:-gov)?\.com\\{0,2}\/(?:services|triggers|workflows|commands|actions)\\{0,2}\/[A-Za-z0-9\/\\_-]{20,}|(?:AKIA|ASIA)[0-9A-Z]{16}|(?:aws_secret_access_key|AWS_SECRET_ACCESS_KEY)(?:\\{0,7}["'])?\s*[:=]\s*(?:\\{0,7}["'])?(?:[A-Za-z0-9\/+]|\\{1,2}\/){40}|AIza[0-9A-Za-z_-]{35}|ya29\.[A-Za-z0-9._-]{20,}|GOCSPX-[A-Za-z0-9_-]{20,}|phx_[A-Za-z0-9]{20,})|(?:(?<![A-Za-z0-9_-])|(?<=\\[nrtfb0aevNLP_])|(?<=\\u[0-9A-Fa-f]{4})|(?<=\\x[0-9A-Fa-f]{2})|(?<=\\U[0-9A-Fa-f]{8}))eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}|(?:[Bb]earer|BEARER)\s+(?=[A-Za-z0-9._~+/-]{0,512}\d)[A-Za-z0-9._~+/-]{16,}=*|(?:\b|(?<=\\[nrtfb0aevNLP_])|(?<=\\u[0-9A-Fa-f]{4})|(?<=\\x[0-9A-Fa-f]{2})|(?<=\\U[0-9A-Fa-f]{8}))(?:Basic|BASIC)\s+(?=[A-Za-z0-9+/]{0,512}[0-9+=])[A-Za-z0-9+/]{16,}={0,2}|(?:(?<=:\/\/)|(?<=:\\\/\\\/)|(?<=:\\\\\/\\\\\/))(?:(?!',|,')[^\s/:?#"]){0,256}:[^\s/?#"]{1,256}(?=@)|-----BEGIN [A-Z ]{0,32}PRIVATE KEY-----(?:[^-]|-(?!----))*?(?:-----END [A-Z ]{0,32}PRIVATE KEY-----|(?:-----[A-Z ]{0,48}-{0,4})?(?![\d\D]))/g,
  ip: /(?:\b|(?<=\\[nrtfb0aevNLP_])|(?<=\\u[0-9A-Fa-f]{4})|(?<=\\x[0-9A-Fa-f]{2})|(?<=\\U[0-9A-Fa-f]{8}))(?:25[0-5]|2[0-4]\d|1?\d?\d)(?:\.(?:25[0-5]|2[0-4]\d|1?\d?\d)){3}\b|(?:(?<![0-9A-Za-z:.])|(?<=\\[nrtfb0aevNLP_])|(?<=\\u[0-9A-Fa-f]{4})|(?<=\\x[0-9A-Fa-f]{2})|(?<=\\U[0-9A-Fa-f]{8}))(?:(?:[0-9A-Fa-f]{1,4}:){7}[0-9A-Fa-f]{1,4}|(?:[0-9A-Fa-f]{1,4}:){1,7}:|(?:[0-9A-Fa-f]{1,4}:){1,6}:[0-9A-Fa-f]{1,4}|(?:[0-9A-Fa-f]{1,4}:){1,5}(?::[0-9A-Fa-f]{1,4}){1,2}|(?:[0-9A-Fa-f]{1,4}:){1,4}(?::[0-9A-Fa-f]{1,4}){1,3}|(?:[0-9A-Fa-f]{1,4}:){1,3}(?::[0-9A-Fa-f]{1,4}){1,4}|(?:[0-9A-Fa-f]{1,4}:){1,2}(?::[0-9A-Fa-f]{1,4}){1,5}|[0-9A-Fa-f]{1,4}:(?::[0-9A-Fa-f]{1,4}){1,6}|:(?::[0-9A-Fa-f]{1,4}){1,7})(?![0-9A-Za-z:]|\.\d)/g, // and an IPv6 address needs at least three groups
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
  placeholder, with no escapes, counts as already replaced; it ends there, whatever follows. A string
  `beforeSend` returns as it got it isn't read for them again.
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
- Single-quoted keys are read too: Python's `{'password': 'x', 'note': "it's"}`, and `b'...'` keys and
  values, util.inspect's quoted keys, and `Map { 'password' => 'x' }`, as is a double-quoted key
  before `=>` or before a value in other quotes, as in `{"user's password": 'x'}`. Only a single-quoted key that opens an entry
  counts, straight after `{`, `(` or `,` or at the start of a line or the string, so a sentence's
  `the 'token': 'x'` and a SQL cast's `, 'token'::text` or `, "token"::text` stay, as do entries a printer
  marks at a line's start, as assert and difflib do with `+ 'password': 'x'` and an exception group's
  traceback with `| 'password': 'x'`. A value in single
  quotes or backticks ends at a matching quote followed
  by the string's or a line's end, `,`, `}`, `]`, `)`, or what ends a sentence, and strings that pprint
  writes side by side or util.inspect joins with `+` go as one. Without such a quote, or if that is over
  256 characters past its first quote, it ends at its first quote, or its line's end. A container goes whole, with what's written before its contents, as in
  `OrderedDict(a='b')`, `Foo { a: 'b' }`, `Set(2) { 'a' }`, `Foo(2) [Map] { 'a' => 'b' }`,
  `[Object: null prototype] { a: 'b' }`, `<ref *1> { a: 'b' }` or `Resp[int](a='b')`, keeping `None`, `True` and `False`.
  What's in angle brackets goes, up to 256 characters, as `<Buffer 68 69>`, and one word goes:
  `{'token': token}`. More than a word isn't a value: `, 'password': explains it.` A value that isn't
  a string, or pieces in different quotes, takes single quotes around its placeholder. A private key block
  that a value runs into goes whole, up to the string's end if nothing closes it.
- In pydantic's errors, `input_value=` straight after `[type=...`, for a type of up to 128 characters, always goes, whatever the field, up to
  `, input_type=` and the type's name, or if that isn't within 1,024 characters, its line's end however
  it's written. It can be cut anywhere, with its key on another line or gone, so it counts as a secret
  even where it isn't one. It's usually the call's `input`, but can be what the tool tried to return or
  anything else it validated, which the event then doesn't hold. A value holding `, input_type=`, a
  name and `]` keeps what follows those. `input_value=` anywhere else is just text, as it is when pprint
  splits it from its type.
- A URL's parameters under secret key names are replaced too, in a query or a fragment:
  `?token=abc&api_key=def`, `#access_token=abc`, `#/access_token=abc`, including `&amp;`, names that
  are percent-encoded, use `+` for a space or end in `[]`, and a URL held in a parameter, percent-encoded up to three times
  over. So do a signed URL's `sig`, `signature` and `hmac`, and a parameter named exactly `code` or
  `code_verifier`, as OAuth and Azure Functions use, including `?code=200`. A name needs `?`, `&`, `#`,
  `#/`, `#!` or `#!/` before it, not `&&`, and needn't be in a URL. A value stops at whitespace or one of
  `&`, `#`, `"`, `'`, `<`, `>`, `` ` ``, `)`, `]`, `}`, `\\`. A template stays if one of those follows
  it: `?token={token}`, `?token={{token}}`, `?token=$TOKEN`, `?token=${TOKEN}`, so a password spelled
  exactly like one stays too. A value that only starts like one goes. The event's own object keys aren't read this
  way. A parameter's value under any other name isn't replaced, nor a name holding `:`, split by `;` or over
  256 characters, or secrets in a URL's path.
- Other syntax isn't covered: `password=hunter2`, `password: hunter2`, an unquoted key
  (`{password: "x"}`, as Node's `util.inspect` writes one), a `repr` such as `User(password='x')`, or
  pairs such as `[('password', 'x')]`.
  Nor are a key holding both kinds of quote, or quotes written another way (CSV's `""`, HTML's `&quot;`, URL encoding, `\x22`),
  colours between a key and its colon, a key and its value split across two strings or lines with a
  border or prefix between them, a name and its value as separate strings
  (`{"name": "DB_PASSWORD", "value": "x"}`), or a copy of the value that isn't under a secret key
  name, as a low-level MCP server's schema refusal has: `'x' is not of type 'integer'`, kept only with
  both `inputs` and `results` on. `beforeSend` / `before_send` handles those, as do the wrapper's `inputs` / `results` options.

Placeholders are numbered per event:

- The first distinct email becomes `[EMAIL_1]`, the second `[EMAIL_2]`, and the same value gets the
  same number everywhere in that event. Different people therefore stay distinct within a turn.
- Numbering skips any placeholder that already appears in the event, so a replaced object key can't
  collide with an existing key or overwrite its value.
- Numbers aren't linked across events: `[EMAIL_1]` in two events can be two different people.
- In `@lsquare/node`, a placeholder written inside a longer value under a secret key name also holds its
  number back, outside `metadata`, so the numbers there can differ from the other SDKs'. The text doesn't.

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
- Also not detected: a value straight after a colour code, whose last letter reads as part of a word,
  or after a percent-encoded character or an octal escape such as Perl's `\13`; a `Bearer` or `Basic`
  token, an AWS value or a card that a printer splits across two strings, as `pprint` does to a long
  string; an email address with non-ASCII characters (`josé@example.com`); a card written with `+` or `%20` between its groups, as a URL encodes it; phone numbers without a `+` or US punctuation (`555 123 4567`, `07700 900123`),
  which bare-digit rules would confuse with counts; secrets in text without a known format, such as
  `password=hunter2` in a question (in structured data and JSON, their key name gives them away);
  IBANs and national ID numbers. An IBAN rule needs a new pattern name, which the ingestion must accept in
  `redactions` before an SDK can send it.

Redaction runs on the app's event loop, so every pattern matches in linear time, as does finding
values in JSON held in a string: emails, JWTs and IPv6 addresses may only start at a token boundary,
so there are at most two attempts per run of token characters; a PEM body can't run past the next `-----`, a
URL's credentials past the next `/`, `?`, `#`, `"`, whitespace or 256 characters, and the `Basic` check looks at most 512 characters ahead.
Secrets are not length-bounded, so long credentials (a 9 KB JWT, a 12 KB key) are still caught.
Placeholder numbers come from a running counter per pattern, so 10,000 distinct emails are numbered
in linear time. Finding values in JSON held in a string has a limit per event: 1,000,000 units, a unit
being roughly one quote or bracket read, one backslash or percent sign decoded, one JSON value checked,
one key looked at, or 16 characters decoded or of a parameter's name. Ordinary text stays under it: 256
KB of JSON printed once takes about 125,000, and of JSON in JSON in JSON about 420,000. Dozens of text
blocks of JSON or URLs printed two or three times over can reach it, as can crafted text in an event of
any size. Past it, a
string that could hold a value (a quote and a colon or `=>`, an `=` and one of `? & #`, a percent-encoded
`=`, or pydantic's `input_value=`) is replaced whole and counted as a secret, so nothing unread is sent.
That bounds only this step, not the patterns, so the slowest event found, on crafted text, takes about
1.9 s in Python and 190 ms in TypeScript, which runs on the app's event loop. The patterns alone take up
to about 900 ms and 60 ms. One string read to its 24,000-character window takes at most about 800 ms and 50 ms, and a whole event of 256 KB of
ordinary JSON about 120 to 300 ms and 12 to 35 ms, depending on how many times it was printed. In
`@lsquare/node`, a crafted 256 KB string takes about 120 ms, 0.1's phone pattern on crafted input (`+`
followed by long digit runs) about 50 ms, and ordinary text without JSON about 3 ms. These are from a
quiet machine: a busy one takes a few times as long.
