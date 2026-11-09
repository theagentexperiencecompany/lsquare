// The extras' rules that don't depend on a call: which of their inputs a tool's definition leaves
// to the wrapper, the listed copy that shows them, get_more_tools, and new session IDs.
// Parts adapted from AgentCat's TypeScript SDK (MIT, © 2025 AgentCat, Inc.; see THIRD_PARTY_NOTICES):
// adding the extras' inputs to a listed tool's schema.
import { randomBytes } from 'node:crypto'
import { types } from 'node:util'
import { hasOwn, put } from '../json.js'
import { copied, silence } from './read.js'

const { isProxy } = types

export const SESSION_ID = 'session_id'
export const INTENT = 'intent'
export const MISSING_TOOLS = 'get_more_tools'

/** A session ID an agent may send back: `ses_` and 22 letters, digits, `-` or `_`. */
export const SESSION_FORMAT = /^ses_[A-Za-z0-9_-]{22}$/
const ALPHABET = 'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_'

const DESCRIPTIONS: Record<string, string> = {
  [SESSION_ID]:
    'The ID of the task this call belongs to. Leave it out on your first call. On every later call for the same task, send the ID an earlier result gave you, and pass it to any subagent working on that task.',
  [INTENT]: "In one sentence, why you're calling this tool and what the user is trying to do. No personal data or secrets.",
}
const MISSING_TOOLS_DESCRIPTION =
  "Call this when none of the available tools fits what you need, or one fits only in part. It doesn't return new tools: it tells the server's developers what to add."
const NEED_DESCRIPTION = "What you needed to do and couldn't, in one or two sentences. No personal data or secrets."
const REPLY = "Noted. No other tools are available right now."
const NO_NEED = "Invalid arguments for tool get_more_tools: need is required, as a string saying what you needed to do and couldn't."

/** A new session ID: `ses_` and 22 random base64url characters. */
export function newSessionId(): string {
  let id = 'ses_'
  // 64 characters: each byte's low 6 bits pick one, evenly.
  for (const byte of randomBytes(22)) id += ALPHABET[byte & 63]
  return id
}

/** The text block that gives the agent its task's ID, at the end of a result. */
export function idBlock(id: string): { type: 'text'; text: string } {
  return { type: 'text', text: `Task ID: ${id}. Send it as session_id on later calls for this task, and pass it to any subagent working on it.` }
}

/** get_more_tools as listed: it takes `need`, and `session_id` when sessions are on. It changes nothing on the server, but recording `need` is what it's for, so it isn't read-only. */
export function missingToolsTool(sessions: boolean): Record<string, unknown> {
  const properties: Record<string, unknown> = { need: { type: 'string', description: NEED_DESCRIPTION } }
  if (sessions) put(properties, SESSION_ID, { type: 'string', description: DESCRIPTIONS[SESSION_ID] })
  return {
    name: MISSING_TOOLS,
    description: MISSING_TOOLS_DESCRIPTION,
    inputSchema: { type: 'object', properties, required: ['need'] },
    annotations: { readOnlyHint: false, destructiveHint: false, openWorldHint: true },
  }
}

/** The wrapper's reply to get_more_tools: an isError result, as the MCP SDKs give, without a string `need`. */
export function missingToolsReply(need: unknown): { content: Array<{ type: 'text'; text: string }>; isError?: true } {
  if (typeof need !== 'string') return { content: [{ type: 'text', text: NO_NEED }], isError: true }
  return { content: [{ type: 'text', text: REPLY }] }
}

/**
 * What a tool's input schema shows of the extras' inputs: the names it declares, when it can be
 * read. When it can't, both are assumed to be the tool's. Read as the schema is now, never cached.
 * Only zod schemas (3 and 4, full and mini) are read, and only two forms of them:
 * - an object that drops the keys it doesn't declare, refined or not: its checks never see them;
 * - one that refuses them, without refinements: a refinement could read them from the refusal.
 *   zod 4 runs the checks it found as it built the schema, a polluted Object.prototype's included,
 *   gone since or not: it runs them from the function it keeps as `_zod.run`, which is the schema's
 *   own `_zod.parse` only when it found none (or, under zod/compile, the run that wraps keeps).
 * Anything else, such as a preprocess, a transform, `lazy`, a union, a record or an object that
 * keeps unknown keys, can read or need any key. Without a schema the tool is never given arguments.
 */
export type Ownership = { readable: true; declared: ReadonlySet<string> } | { readable: false; why: string }

/** What a registered tool's definition says of the extras' inputs: a definition that can't be read is the tool's. */
export function toolOwnership(tool: unknown): Ownership {
  try {
    return ownership(get(tool, 'inputSchema'))
  } catch (error) {
    return unreadable(error)
  }
}

export function ownership(schema: unknown): Ownership {
  try {
    // The MCP SDKs pass no arguments to a tool without one.
    if (!schema) return { readable: true, declared: new Set() }
    if (typeof schema !== 'object' && typeof schema !== 'function') return { readable: false, why: 'it is not a schema' }
    // zod 4 also has `_def`, as an alias: `_zod` comes first.
    const zod = get(schema, '_zod')
    if (zod !== undefined) return zod4(zod)
    const def = get(schema, '_def')
    if (def !== undefined) return zod3(schema, def, false, 0)
    return { readable: false, why: 'it is not a zod schema' }
  } catch (error) {
    return unreadable(error)
  }
}

/**
 * Why get() gave up on a reading, by what it threw: told by identity alone, so that a value the
 * app's code threw (a getter's, perhaps a Proxy) is never asked anything. `instanceof` would run
 * its getPrototypeOf trap, which may answer with a rejected promise nothing else would handle.
 */
const refusals = new WeakMap<object, string>()

/** Ends the reading: the schema is unreadable, for `why`. */
function refuse(why: string): never {
  const refusal = Object.freeze(Object.create(null) as object)
  refusals.set(refusal, why)
  throw refusal
}

function unreadable(error: unknown): Ownership {
  const why = (typeof error === 'object' || typeof error === 'function') && error !== null ? refusals.get(error) : undefined
  return { readable: false, why: why ?? 'it could not be read' }
}

/** How deep a prototype chain get() follows: a deeper one isn't read. */
const CHAIN = 32

/**
 * One property of a tool's definition or its zod schema, for reading ownership. Unlike read(), a
 * read that throws throws on, failing the whole reading: an absent value means the permissive case
 * (no catchall, no checks), so a failed one must never pass for it. The object's own, or its
 * class's (see declared()). One that only Object.prototype has ends the reading too (refuse()): it
 * is neither the schema's nor absent. zod 4 reads a def's `catchall` and `checks` through the
 * prototype as it builds the schema, then runs what it found (a polluted `catchall` keeps every
 * key the tool is sent), and a reading now can't tell that from a pollution made since; a
 * polluted `_zod` would make another schema of one. Whether Object.prototype has it is asked of
 * Object.prototype itself, never of the value (`in` would run a Proxy's `has` trap).
 *
 * A Proxy, the value or one of its prototypes, ends the reading before any of its traps runs:
 * JavaScript itself consumes what some of them answer (`getOwnPropertyDescriptor`, `has`), so a
 * promise one gives can't be silenced, and nothing but the wrapper asks those of a schema. zod's
 * own objects are never proxies. A value is silenced, as read() silences one: a getter the
 * listing's JSON never calls (one a class defines, or one that isn't enumerable) may give a
 * promise, which the wrapper is then the only one to see.
 */
function get(value: unknown, key: string): unknown {
  if ((typeof value !== 'object' && typeof value !== 'function') || value === null) return undefined
  let object: object | null = value
  for (let level = 0; object !== null && object !== Object.prototype; level++) {
    if (level >= CHAIN) refuse('its prototype chain is too deep to read')
    if (isProxy(object)) refuse('part of it is a Proxy, whose traps would run as it is read')
    if (Object.prototype.hasOwnProperty.call(object, key)) {
      const item = (value as Record<string, unknown>)[key]
      silence(item)
      return item
    }
    // An ordinary object's: no trap runs.
    object = Object.getPrototypeOf(object) as object | null
  }
  // Only Object.prototype's, or nobody's: a chain that ends without Object.prototype finds no pollution.
  if (object === Object.prototype && Object.prototype.hasOwnProperty.call(Object.prototype, key)) {
    refuse(`its ${key} comes from a polluted Object.prototype, which zod may run`)
  }
  return undefined
}

/** A list's length, as get() reads it: a list whose length isn't a number ends the reading. */
function lengthOf(list: unknown): number {
  const length = get(list, 'length')
  if (typeof length !== 'number' || !(length <= 10_000)) refuse('a list in it could not be read')
  return length
}

/** A list's entries, as get() reads them: its length, then each index. */
function entries(list: unknown): unknown[] {
  const items: unknown[] = []
  for (let index = 0, length = lengthOf(list); index < length; index++) items.push(get(list, String(index)))
  return items
}

/**
 * One property of a listed schema, as get() reads one, but only the object's own, as it goes to
 * the client: JSON writes nothing else of it. A list's entries are read as JSON writes them, from
 * its prototype too. Its keys are listed with Object.keys(), never `for...in`, which would also
 * ask a Proxy for its prototype, as JSON doesn't (a trap's answer JavaScript consumes): the
 * listing's JSON lists the same keys, the same way, as it writes them.
 */
function listedField(value: unknown, key: string): unknown {
  if ((typeof value !== 'object' && typeof value !== 'function') || value === null) return undefined
  if (!Array.isArray(value) && !hasOwn(value, key)) return undefined
  const item = (value as Record<string, unknown>)[key]
  silence(item)
  return item
}

function zod4(zod: unknown): Ownership {
  const def = get(zod, 'def')
  const type = get(def, 'type')
  if (type !== 'object') return { readable: false, why: `it is a zod ${typeof type === 'string' ? type : 'schema'}, not a plain object` }
  const checks = get(def, 'checks')
  if (checks !== undefined && !Array.isArray(checks)) return { readable: false, why: 'its refinements could not be read' }
  const refined = checks !== undefined && lengthOf(checks) > 0
  const catchall = get(def, 'catchall')
  if (catchall !== undefined) {
    if (get(get(get(catchall, '_zod'), 'def'), 'type') !== 'never') return { readable: false, why: 'it keeps keys it does not declare' }
    if (refined) return { readable: false, why: 'it refuses keys it does not declare, and is refined' }
    // Checks it found as zod built it, which its def no longer shows (a polluted `checks`, gone since).
    if (!runsParseAlone(zod)) return { readable: false, why: 'it refuses keys it does not declare, and zod runs checks on it' }
  }
  // Reading the shape is what zod does itself the first time it lists or checks the schema.
  return declared(get(def, 'shape'))
}

/**
 * zod 4 runs a schema through `_zod.run`, which it makes the schema's own `_zod.parse` when it
 * finds no checks as it builds it, and otherwise a function that runs them too. zod/compile wraps
 * the run it finds, keeping that one as the wrapper's `__originalRun` (as zod's own compile()
 * unwraps it). A run that is neither, a schema z.compile() cloned included, may run checks.
 */
function runsParseAlone(zod: unknown): boolean {
  const parse = get(zod, 'parse')
  const run = get(zod, 'run')
  if (typeof parse !== 'function' || typeof run !== 'function') return false
  return (get(run, '__originalRun') ?? run) === parse
}

function zod3(schema: unknown, def: unknown, refined: boolean, depth: number): Ownership {
  const typeName = get(def, 'typeName')
  if (typeName === 'ZodEffects') {
    const effect = get(get(def, 'effect'), 'type')
    if (effect !== 'refinement') return { readable: false, why: `it is a zod 3 ${typeof effect === 'string' ? effect : 'effect'}` }
    if (depth >= 100) return { readable: false, why: 'it could not be read' }
    const inner = get(def, 'schema')
    return zod3(inner, get(inner, '_def'), true, depth + 1)
  }
  if (typeName !== 'ZodObject') return { readable: false, why: `it is a zod 3 ${typeof typeName === 'string' ? typeName : 'schema'}, not a plain object` }
  if (get(get(get(def, 'catchall'), '_def'), 'typeName') !== 'ZodNever') return { readable: false, why: 'it keeps keys it does not declare' }
  const unknownKeys = get(def, 'unknownKeys')
  if (unknownKeys === 'strict' ? refined : unknownKeys !== 'strip') {
    return { readable: false, why: unknownKeys === 'strict' ? 'it refuses keys it does not declare, and is refined' : 'it keeps keys it does not declare' }
  }
  const shape = get(def, 'shape')
  if (typeof shape !== 'function') return { readable: false, why: 'its shape could not be read' }
  const made: unknown = Reflect.apply(shape, undefined, [])
  silence(made)
  const now = declared(made)
  if (!now.readable) return now
  // zod 3 keeps the keys of its first check, which a shape changed later doesn't update.
  const cached = get(schema, '_cached')
  if (cached !== null && cached !== undefined) {
    const keys = get(cached, 'keys')
    const listed = Array.isArray(keys) ? entries(keys) : undefined
    if (!listed || listed.length !== now.declared.size || listed.some((key) => typeof key !== 'string' || !now.declared.has(key))) {
      return { readable: false, why: 'its shape changed after it was first used' }
    }
  }
  return now
}

/** The keys a shape declares: its own enumerable ones, as zod lists them. A Proxy's aren't read (see get()). */
function declared(shape: unknown): Ownership {
  if (typeof shape !== 'object' || shape === null || isProxy(shape)) return { readable: false, why: 'its shape could not be read' }
  return { readable: true, declared: new Set(Object.keys(shape)) }
}

/**
 * What a listed JSON Schema says of the extras' inputs, on a low-level server, where the listing
 * speaks for the tool: readable only when its root holds only `type: "object"`, `properties`, a
 * `required` of names, `additionalProperties` absent or false, and the keys that only describe.
 * `declared` holds only the extras' inputs it names, in `properties` or `required`. A read that
 * throws, or a field of the wrong kind, makes it unreadable.
 */
export function listedOwnership(schema: unknown): Ownership {
  const unreadable: Ownership = { readable: false, why: 'its listed input schema is more than an object of named properties' }
  try {
    if (typeof schema !== 'object' || schema === null || Array.isArray(schema)) return unreadable
    for (const key of Object.keys(schema)) if (!ROOT_KEYS.has(key)) return unreadable
    if (listedField(schema, 'type') !== 'object') return unreadable
    const properties = listedField(schema, 'properties')
    if (properties !== undefined && (typeof properties !== 'object' || properties === null || Array.isArray(properties))) return unreadable
    const required = requiredNames(listedField(schema, 'required'))
    if (required === 'unreadable') return unreadable
    const additional = listedField(schema, 'additionalProperties')
    if (additional !== undefined && additional !== false) return unreadable
    const names = new Set<string>()
    for (const name of required) if (name === SESSION_ID || name === INTENT) names.add(name)
    for (const name of [SESSION_ID, INTENT]) if (properties !== undefined && hasOwn(properties as object, name)) names.add(name)
    return { readable: true, declared: names }
  } catch {
    return unreadable
  }
}

/** Two readings agree on the extras' inputs: both unreadable, or both declaring the same of them. */
export function sameOwnership(a: Ownership, b: Ownership): boolean {
  if (!a.readable || !b.readable) return a.readable === b.readable
  return [SESSION_ID, INTENT].every((name) => a.declared.has(name) === b.declared.has(name))
}

/** Keys a listed schema's root may hold for inputs to be added to it: the object's own, and those that only describe. */
const ROOT_KEYS = new Set(['type', 'properties', 'required', 'additionalProperties', '$schema', '$id', '$comment', '$defs', 'definitions', 'title', 'description', 'default', 'examples', 'deprecated'])
/** How much of a listed schema is read to find its references, at most. */
const WALK_NODES = 10_000
const WALK_DEPTH = 64

/**
 * A copy of a tool's listed input schema with `names` added as optional string properties, each
 * only where the schema takes it as one more without changing what it accepts; undefined when
 * none can be added. The root may hold only `type: "object"`, `properties`, a `required` without
 * the name, `additionalProperties` absent or false, and keys that only describe. Every reference
 * in it must point into its `$defs`, `definitions` or one of its own properties: one to the root,
 * as a recursive schema has, would show the inputs at nested levels too, where they aren't removed.
 */
export function advertise(schema: unknown, names: readonly string[]): Record<string, unknown> | undefined {
  try {
    if (typeof schema !== 'object' || schema === null || Array.isArray(schema) || names.length === 0) return undefined
    const root = schema as Record<string, unknown>
    for (const key of Object.keys(root)) if (!ROOT_KEYS.has(key)) return undefined
    if (listedField(root, 'type') !== 'object') return undefined
    const properties = listedField(root, 'properties')
    if (properties !== undefined && (typeof properties !== 'object' || properties === null || Array.isArray(properties))) return undefined
    const required = requiredNames(listedField(root, 'required'), true)
    if (required === 'unreadable') return undefined
    const additional = listedField(root, 'additionalProperties')
    if (additional !== undefined && additional !== false) return undefined
    const named = (name: string) => properties !== undefined && hasOwn(properties as object, name)
    const adding = names.filter((name) => !named(name) && !required.includes(name))
    if (adding.length === 0 || !localReferences(root, adding)) return undefined
    // Copies as JSON writes them, each value read once (see copied()), what was read already not again.
    const added = properties === undefined ? {} : copied(properties as object)
    // Added as a literal adds them (see put()), never through a polluted Object.prototype.
    for (const name of adding) put(added, name, { type: 'string', description: DESCRIPTIONS[name] })
    return copied(root, { properties: added })
  } catch {
    return undefined
  }
}

/**
 * Every `$ref` in the schema points into its `$defs`, `definitions` or a property it already has,
 * and it has no dynamic references. Each entry read counts against the budget, before its value
 * is read, so a wide object or list costs no more than a deep one. Each value is read through
 * listedField(), as an object's own.
 */
function localReferences(root: Record<string, unknown>, adding: readonly string[]): boolean {
  const stack: Array<{ node: object; depth: number }> = [{ node: root, depth: 0 }]
  let budget = WALK_NODES
  while (stack.length > 0) {
    const { node, depth } = stack.pop()!
    if (depth > WALK_DEPTH) return false
    const object = node as Record<string, unknown>
    const has = (key: string) => hasOwn(object, key)
    if (!Array.isArray(node)) {
      if (has('$dynamicRef') || has('$recursiveRef')) return false
      if (has('$ref') && !localReference(listedField(object, '$ref'), adding)) return false
    }
    // Listed as JSON lists them (see listedField()); the budget stops the walk before it reads them all.
    for (const key of Object.keys(object)) {
      if (--budget < 0) return false
      const value = listedField(object, key)
      if (typeof value === 'object' && value !== null) stack.push({ node: value, depth: depth + 1 })
    }
  }
  return true
}

/**
 * The names a listed schema's `required` holds, each read as JSON writes a list (see listedField()),
 * its length once: 'unreadable' unless a list of at most 10,000 strings, or, with `any`, of
 * anything (advertise() needs only to know which names are there).
 */
function requiredNames(required: unknown, any = false): unknown[] | 'unreadable' {
  if (required === undefined) return []
  if (!Array.isArray(required)) return 'unreadable'
  const length = listedField(required, 'length')
  if (typeof length !== 'number' || !(length <= 10_000)) return 'unreadable'
  const names: unknown[] = []
  for (let index = 0; index < length; index++) {
    const name = listedField(required, String(index))
    if (!any && typeof name !== 'string') return 'unreadable'
    names.push(name)
  }
  return names
}

function localReference(ref: unknown, adding: readonly string[]): boolean {
  // Percent-encoding could spell any name: such a reference counts as unknown.
  if (typeof ref !== 'string' || ref.includes('%')) return false
  if (ref.startsWith('#/$defs/') || ref.startsWith('#/definitions/')) return true
  if (!ref.startsWith('#/properties/')) return false
  const name = ref.slice('#/properties/'.length).split('/')[0]!.replace(/~1/g, '/').replace(/~0/g, '~')
  return !adding.includes(name)
}
