/**
 * The configuration document as an editor has to treat it: patches, segments,
 * and the one value that must never be written back.
 *
 * `config.json` IS the write path — the operator edits the document and saves, and
 * the backend validates on save — so these are the three rules that make editing a
 * document safe against a MERGE-patch write path.
 *
 * ## 1. A save sends a patch, so a deletion has to be spelled
 *
 * `ConfigStore.write` merges: nested objects merge key by key, and a `null` value
 * DELETES its key. That is JSON Merge Patch, and it is what lets one panel edit one
 * project without resending every other. But it also means sending the edited
 * document verbatim would silently ignore every line the operator deleted — the key
 * is simply absent from the patch, so the merge keeps the old value and the editor
 * shows a change that did not happen. {@link mergePatch} computes the patch from the
 * baseline instead, and a removed key becomes an explicit `null`.
 *
 * A form does not diff a baseline — it stages the values an operator touched — so the
 * same rule reaches it through {@link DELETE}: a staged removal is a sentinel that
 * {@link buildFormPatch} writes out as that `null`, and nothing else ever emits one.
 *
 * ## 2. An elided value must never be written back
 *
 * The read withholds credential-classified values, replacing each with a marker it
 * reports as `elided_marker`. They can be OVERWRITTEN — an operator may type a new
 * token — but a save that echoed the marker back would replace a live credential
 * with it, and nothing downstream would report that: the document stays valid, the
 * write is recorded as ordinary, and the capability that needed the token fails
 * later somewhere else. So the marker is dropped from every OBJECT position in a
 * patch — the only positions an accepted document can carry it; see
 * :func:`withoutElided` for why arrays are outside the guarantee and why that
 * costs nothing.
 *
 * The marker is a PARAMETER rather than a constant here, and that is the point: it
 * is the store's own value, relayed by the read. A copy of the string on this side
 * would be a second spelling of one constant, and the two drifting apart would
 * silently disable the protection above.
 *
 * The residual, stated: the marker itself cannot be written through this editor as a
 * literal value. Nobody means to configure that string, and the alternative —
 * trusting the read's `elided` path list to decide which occurrences are real —
 * fails on exactly the case that matters, a key the operator RENAMED while its
 * value was still withheld.
 *
 * ## 3. A path is a list of segments, never a dotted string
 *
 * Configuration names are user-chosen, and a profile may legitimately be called
 * `thrifty.roles`. Its review-role node then lives at
 * `cost_profiles` / `thrifty.roles` / `roles` / `review`, whose dotted rendering is
 * `cost_profiles.thrifty.roles.roles.review` — and no split of that string recovers
 * the four segments. So every function here takes segments, and dotted paths exist
 * for DISPLAY only. {@link isDescendant} compares segment for segment, which is why
 * `thrifty.roles.review` is not a descendant of `thrifty.roles`: as segments those
 * are two sibling names, not a path and its prefix.
 */

/** Section holding the cost profiles, whose role assignments live under it. */
export const COST_PROFILES = 'cost_profiles'

/** Section holding the per-project entries. */
export const PROJECTS = 'projects'

/** Key holding the role assignments inside a profile object. */
export const ROLES_KEY = 'roles'

/** Section holding the watch sources, whose autonomy grids live under it. */
export const SOURCES = 'sources'

/** Key holding the autonomy grid inside a source object. */
export const AUTONOMY_KEY = 'autonomy'

/** A JSON object as it arrives from the read. */
export type Document = Record<string, unknown>

/** Whether *value* is a plain JSON object, which is what merges key by key. */
export function isObject(value: unknown): value is Document {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
}

/**
 * The document node at *segments*, or `undefined` when the path does not exist.
 *
 * `undefined` for a missing node AND for a node whose stored value is `undefined`
 * is not a distinction JSON can carry — `undefined` is not a JSON value — so the
 * two cannot be confused here.
 */
export function nodeAt(document: unknown, segments: readonly string[]): unknown {
  let node: unknown = document
  for (const segment of segments) {
    if (!isObject(node)) return undefined
    node = node[segment]
  }
  return node
}

/** A patch that sets *value* at *segments*, nesting one object per segment. */
export function patchAt(segments: readonly string[], value: unknown): Document {
  if (segments.length === 0) throw new Error('a patch needs at least one segment to address')
  let patch: unknown = value
  for (let index = segments.length - 1; index >= 0; index -= 1) {
    patch = { [segments[index]]: patch }
  }
  return patch as Document
}

/**
 * Whether *path* lies strictly inside *ancestor*, compared segment for segment.
 *
 * Strictly: a node is not its own ancestor, because the two license different
 * actions — clearing a node and clearing something under it are different edits.
 */
export function isDescendant(path: readonly string[], ancestor: readonly string[]): boolean {
  if (path.length <= ancestor.length) return false
  return ancestor.every((segment, index) => path[index] === segment)
}

/** Whether two paths address the same node, compared segment for segment. */
export function samePath(one: readonly string[], other: readonly string[]): boolean {
  return one.length === other.length && one.every((segment, index) => segment === other[index])
}

/**
 * Whether either path lies at or inside the other.
 *
 * The relation {@link buildFormPatch} cannot carry twice: it is last-edit-wins,
 * so an ancestor and a descendant staged together leave only one of the two in
 * the patch — and a review card built from the staged list would then describe a
 * change the write does not make. Callers reconcile on this rather than trusting
 * that a form only ever stages leaves.
 */
export function pathsOverlap(one: readonly string[], other: readonly string[]): boolean {
  return samePath(one, other) || isDescendant(one, other) || isDescendant(other, one)
}

/** Scope a setting is written at, spelled as the registry projects it. */
export const SCOPE_APP = 'app'

/** Scope a setting is written at, spelled as the registry projects it. */
export const SCOPE_PROJECT = 'project'

/** Scope a setting is written at, spelled as the registry projects it. */
export const SCOPE_SOURCE = 'source'

/**
 * The segments addressing one registry setting's stored value at one scope, or
 * `null` when no path can be composed.
 *
 * The dotted registry key splits at its FIRST dot only, which is the engine's own
 * split: `group` is the leading segment and `leaf` is everything after it, as one
 * segment. So a key of `a.b.c` addresses `a` / `b.c` — two segments, not three —
 * and the composed path matches what `stored_value` reads.
 *
 * `null` rather than a partial path for the three ways a scope has no address: an
 * unknown scope name (a scope the engine gains has no composition here until one
 * is written), a project- or source-scoped write with no target named, and a key
 * with no group at all. A caller offers the scope only when a path exists,
 * because `projects..limits.x` would write a project literally named the empty
 * string.
 */
export function settingSegments(key: string, scope: string, target: string): string[] | null {
  const dot = key.indexOf('.')
  if (dot <= 0 || dot === key.length - 1) return null
  const leaf = [key.slice(0, dot), key.slice(dot + 1)]
  if (scope === SCOPE_APP) return leaf
  if (target === '') return null
  if (scope === SCOPE_PROJECT) return [PROJECTS, target, ...leaf]
  if (scope === SCOPE_SOURCE) return [SOURCES, target, ...leaf]
  return null
}

/**
 * The segments addressing one role's assignment inside one profile.
 *
 * Both names come from the resolved read's own fields, never from splitting the
 * declaring path it reports beside them.
 */
export function roleSegments(profile: string, role: string): string[] {
  return [COST_PROFILES, profile, ROLES_KEY, role]
}

/**
 * One autonomy-grid cell's address: the three names that identify it.
 *
 * Separate from the level so a lookup can ask "is there a choice for this cell"
 * without inventing a level to ask with.
 */
export interface GridCellRef {
  /** The watch source whose grid holds the cell. */
  source: string
  /** The submitter class, in the engine's vocabulary. */
  klass: string
  /** The spec type, in the engine's vocabulary. */
  specType: string
}

/**
 * One autonomy-grid cell an operator has chosen a level for and not yet written.
 *
 * The stored cell it replaces is deliberately NOT carried here: the level and the
 * origin in force are read from the current answer at render time, so a change
 * landing from another surface while a choice sits unwritten cannot leave the
 * review describing a level nobody holds any more.
 */
export interface PendingEdit extends GridCellRef {
  /** The level to store at the cell, in the engine's vocabulary. */
  level: string
}

/** The segments addressing one grid cell's own stored level. */
export function gridCellSegments(cell: GridCellRef): string[] {
  return [SOURCES, cell.source, AUTONOMY_KEY, cell.klass, cell.specType]
}

/** Whether two addresses name the same cell. */
export function sameCell(one: GridCellRef, other: GridCellRef): boolean {
  return (
    one.source === other.source &&
    one.klass === other.klass &&
    one.specType === other.specType
  )
}

/**
 * The staged value that REMOVES the key at its path instead of storing something.
 *
 * A symbol rather than `null`, because `null` is a value a document may legitimately
 * hold: a sentinel spelled `null` could not tell "store null here" from "delete this
 * key", and the two are opposite writes. A symbol also cannot survive
 * `JSON.stringify`, so it cannot reach the wire by accident —
 * {@link buildFormPatch} is the only thing that translates it, and the `null` it
 * emits is the store's own deletion spelling.
 */
export const DELETE = Symbol('remove the key at this path')

/**
 * One value an operator has changed on a form and not yet written.
 *
 * Segments rather than a dotted key, for rule 3 above: a profile or source name may
 * hold a dot, and no split of the rendered path recovers the names.
 *
 * The stored value being replaced is deliberately NOT carried here, for
 * {@link PendingEdit}'s reason: the review reads what is in force from the current
 * answer, so a change landing from another surface while an edit sits unwritten
 * cannot leave a sentence describing a value nobody holds any more.
 */
export interface StagedEdit {
  /** The document path this edit addresses, one segment per name. */
  readonly segments: readonly string[]
  /** The value to store, or {@link DELETE} to remove the key. */
  readonly value: unknown
}

/**
 * The minimal merge patch that stores *edits* and touches nothing else.
 *
 * Every leaf is one staged edit's own path, so the store's merge — nested objects
 * merged key by key — leaves every other path in the document exactly as it was.
 * That is what makes a form write provably isolated from every value the operator
 * did not touch: the isolation is a property of the patch's SHAPE rather than of
 * care taken at the call site, which is why this is a pure function with a property
 * test on it.
 *
 * A {@link DELETE} becomes an explicit `null`, which is how the store deletes. Note
 * what that means for a path whose parent is not stored: the merge creates the
 * parent and then removes nothing from it, leaving an empty container behind. So a
 * caller stages a deletion for a value that exists, which is what a form does — it
 * removes something it is displaying.
 *
 * Two edits sharing a prefix share that container rather than the second replacing
 * the first, and a second edit to one path wins over the first: an operator's last
 * choice is the one they are about to read in the review. Paths are expected to be
 * pairwise non-overlapping — a form stages leaves — and when one edit's path lies
 * INSIDE another's the later edit wins whole, dropping the earlier. A caller that
 * staged both would be describing two changes the write cannot both carry.
 *
 * Containers are created prototype-less, and only a container this function made is
 * descended into. Both halves matter and for different reasons. A name of
 * `__proto__` would otherwise hit `Object.prototype`'s setter — the assignment would
 * set a prototype instead of creating a key, the patch would serialize without that
 * edit, and the review card would show a change the write then did not carry. And
 * descending into a value a CALLER staged would mutate the caller's own object while
 * building a patch of it. Silent loss of an edit is the one failure this surface
 * must not have.
 */
export function buildFormPatch(edits: readonly StagedEdit[]): Document {
  const patch = emptyContainer()
  // Only these are safe to descend into: see the prototype note above.
  const built = new WeakSet<Document>([patch])
  for (const edit of edits) {
    if (edit.segments.length === 0) {
      throw new Error('a staged edit needs at least one segment to address')
    }
    let node = patch
    for (const segment of edit.segments.slice(0, -1)) {
      const child = node[segment]
      if (isObject(child) && built.has(child)) {
        node = child
        continue
      }
      const fresh = emptyContainer()
      built.add(fresh)
      node[segment] = fresh
      node = fresh
    }
    node[edit.segments[edit.segments.length - 1]] = edit.value === DELETE ? null : edit.value
  }
  return patch
}

/**
 * The minimal merge patch that stores a set of grid choices.
 *
 * One cell's own level at `sources.<name>.autonomy.<class>.<type>`, through
 * {@link buildFormPatch} — the same mechanism every form write uses, so the grid's
 * isolation and a form's are one property with one proof rather than two builders
 * that can drift.
 *
 * A wildcard cell is never a target. An edit on a pair a broader rule answered
 * writes the pair's OWN cell, leaving the broader rule in place for the pairs it
 * still answers — modifying the wildcard would change cells nobody was looking at.
 */
export function buildGridPatch(edits: readonly PendingEdit[]): Document {
  return buildFormPatch(
    edits.map((edit) => ({ segments: gridCellSegments(edit), value: edit.level })),
  )
}

/** A container for patch nesting, with no prototype to shadow a key. */
function emptyContainer(): Document {
  return Object.create(null) as Document
}

/** A dotted rendering of *segments*, for display. Never parsed back. */
export function dotted(segments: readonly string[]): string {
  return segments.join('.')
}

/**
 * The JSON Merge Patch that turns *base* into *edited*.
 *
 * Recursive for objects, whole-value for everything else — an array is replaced,
 * never merged, which matches what the store's own merge does. A key present in
 * *base* and absent from *edited* becomes `null`, which is how the store deletes.
 *
 * Two values are compared by their canonical JSON rather than by identity, so a
 * reformatted document that changed nothing produces an EMPTY patch. That matters
 * more than it looks: an empty patch still writes (the store records every write),
 * so a panel can decide to send nothing at all.
 *
 * *elidedMarker* is dropped wherever it appears as a value, which is also why an
 * unchanged withheld value produces no patch entry at all.
 */
export function mergePatch(base: unknown, edited: unknown, elidedMarker: string): Document {
  const from = isObject(base) ? base : {}
  const to = isObject(edited) ? edited : {}
  const patch: Document = {}
  for (const key of Object.keys(to)) {
    const next = to[key]
    if (next === elidedMarker) continue
    const previous = from[key]
    if (isObject(next) && isObject(previous)) {
      const nested = mergePatch(previous, next, elidedMarker)
      if (Object.keys(nested).length > 0) patch[key] = nested
      continue
    }
    if (isObject(next)) {
      // New object, or one replacing a scalar. Sent whole, minus any marker inside
      // it, so a credential nested in a newly added section is not written back as
      // the marker either.
      patch[key] = withoutElided(next, elidedMarker)
      continue
    }
    if (canonical(next) !== canonical(previous)) patch[key] = next
  }
  for (const key of Object.keys(from)) {
    // Deleted in the editor. Spelled as null, because a merge patch that omits the
    // key keeps the old value and the editor would show a change that never landed.
    if (!(key in to)) patch[key] = null
  }
  return patch
}

/** *value* with the elision marker removed from every OBJECT position.
 *
 * Arrays are passed through whole: dropping an element would silently
 * renumber its siblings, and no schema-valid document can hold a
 * secret-classified key inside an array (the free-form maps — variables,
 * field_map, capability env — are all objects), so a marker inside an array
 * cannot arrive through an accepted write. If one is hand-edited into an
 * invalid document, the store refuses the merged document rather than
 * losing a credential silently.
 */
function withoutElided(value: Document, elidedMarker: string): Document {
  const kept: Document = {}
  for (const [key, inner] of Object.entries(value)) {
    if (inner === elidedMarker) continue
    kept[key] = isObject(inner) ? withoutElided(inner, elidedMarker) : inner
  }
  return kept
}

/**
 * Key-sorted JSON for *value*, so two structurally equal values compare equal.
 *
 * Sorted because a document round-tripped through an editor can legitimately
 * reorder keys, and a patch entry for a key nobody changed would record a write
 * that changed nothing.
 */
function canonical(value: unknown): string {
  return JSON.stringify(sortKeys(value))
}

function sortKeys(value: unknown): unknown {
  if (Array.isArray(value)) return value.map(sortKeys)
  if (!isObject(value)) return value
  const sorted: Document = {}
  for (const key of Object.keys(value).sort()) sorted[key] = sortKeys(value[key])
  return sorted
}

/** What parsing an edited document produced: a value, or the parser's own message. */
export type ParsedDocument =
  | { ok: true; document: Document }
  | { ok: false; error: string }

/**
 * Parse edited text as a configuration document.
 *
 * A non-object is refused here rather than sent: the store refuses it too, but its
 * refusal arrives after a round trip, and the operator is holding the text.
 */
export function parseDocument(text: string, notObjectMessage: string): ParsedDocument {
  let parsed: unknown
  try {
    parsed = JSON.parse(text)
  } catch (cause) {
    return { ok: false, error: cause instanceof Error ? cause.message : String(cause) }
  }
  if (!isObject(parsed)) return { ok: false, error: notObjectMessage }
  return { ok: true, document: parsed }
}

/** The document as the editor shows it: two-space JSON, key order preserved. */
export function documentText(document: unknown): string {
  return `${JSON.stringify(document ?? {}, null, 2)}\n`
}
