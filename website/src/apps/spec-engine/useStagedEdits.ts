/**
 * Staged form edits: the one place a configuration form holds what it has not
 * written yet.
 *
 * Every form on the configuration pane stages `(path segments, value)` pairs,
 * shows them as unwritten, builds one patch from them and sends it through the
 * review card's confirm. That is one mechanism rather than one per form, because
 * three copies of it would be three chances for the staged list and the patch to
 * disagree — and the whole guarantee of the review card is that they cannot.
 *
 * ## The reconciliation, which is the reason this is not a `useState`
 *
 * `buildFormPatch` is last-edit-wins over overlapping paths: an edit at
 * `sources.gh` and an edit at `sources.gh.enabled` cannot both survive in one
 * patch, because the later one replaces the container the earlier one built. A
 * form that simply appended both would then show TWO sentences in the review card
 * while the patch carried one, and the operator would confirm a change that does
 * not happen. So staging reconciles: an edit drops every staged edit its own path
 * overlaps, and re-staging one path replaces it where it already sits.
 *
 * Replaced in PLACE rather than moved to the end, so the review reads the changes
 * in the order they were first made and correcting one does not reorder the
 * account of the others.
 *
 * The two invariants that follow, and are property-tested:
 *
 * 1. **The staged list is pairwise non-overlapping.** No staged path lies at or
 *    inside another's.
 * 2. **Every staged edit survives into the patch.** `buildFormPatch` carries each
 *    one at its own path with its own value, so the review card cannot describe
 *    an edit the write drops.
 *
 * The reducers below are pure and exported for that reason: a property over a
 * hook is a property over React's scheduler, and what is being claimed here is a
 * property of the reconciliation itself.
 */
import { useCallback, useMemo, useState } from 'react'

import { pathsOverlap, samePath, type StagedEdit } from './configDocument'

/**
 * *edits* with *segments* staged at *value*, overlaps reconciled.
 *
 * The new edit always survives: it is the operator's latest act, and it is what
 * the review is about to read out. Everything its path overlaps is dropped rather
 * than kept, because the patch would drop it anyway.
 */
export function stageEdit(
  edits: readonly StagedEdit[],
  segments: readonly string[],
  value: unknown,
): StagedEdit[] {
  if (segments.length === 0) {
    // The same refusal `buildFormPatch` makes, made where the caller still is: a
    // patch with no segments is the whole document, which is the one write no
    // form ever means to make.
    throw new Error('a staged edit needs at least one segment to address')
  }
  // Copied, so a caller's array cannot become the staged path and change under it.
  const edit: StagedEdit = { segments: [...segments], value }
  const at = edits.findIndex((other) => samePath(other.segments, segments))
  if (at >= 0) {
    // Same path: a mind changed, replaced where it sits. Overlaps are still
    // dropped, because an edit at this exact path can coexist with neither an
    // ancestor nor a descendant.
    return edits
      .map((other, index) => (index === at ? edit : other))
      .filter((other) => other === edit || !pathsOverlap(other.segments, segments))
  }
  return [...edits.filter((other) => !pathsOverlap(other.segments, segments)), edit]
}

/** *edits* with the edit at *segments* withdrawn, if one is staged there. */
export function unstageEdit(
  edits: readonly StagedEdit[],
  segments: readonly string[],
): StagedEdit[] {
  return edits.filter((other) => !samePath(other.segments, segments))
}

/** The staged-edit state a form holds, and the four ways it changes. */
export interface StagedEdits {
  /** The staged edits, in the order they were first made. */
  readonly edits: readonly StagedEdit[]
  /** The edit staged at *segments*, or `undefined` when none is. */
  stagedAt: (segments: readonly string[]) => StagedEdit | undefined
  /** Stage *value* at *segments*, reconciling every path it overlaps. */
  stage: (segments: readonly string[], value: unknown) => void
  /** Withdraw the edit at *segments*, if one is staged there. */
  unstage: (segments: readonly string[]) => void
  /** Drop every staged edit *accounts* does not vouch for. */
  reconcile: (accounts: (edit: StagedEdit) => boolean) => void
  /** Drop every staged edit. */
  clear: () => void
}

/**
 * Hold a form's unwritten edits.
 *
 * `reconcile` exists because a staged edit outlives the answer it was made
 * against: a project entry can be removed, a source can leave the document, a
 * selection can move — from this pane, from another surface, or on any refetch.
 * An edit the form can no longer show is an edit no confirm could clear and no
 * sentence could describe, so a form drops it against the CURRENT answer rather
 * than trusting the document to hold still between a choice and its confirm.
 */
export function useStagedEdits(): StagedEdits {
  const [edits, setEdits] = useState<readonly StagedEdit[]>([])

  const stagedAt = useCallback(
    (segments: readonly string[]) => edits.find((edit) => samePath(edit.segments, segments)),
    [edits],
  )
  const stage = useCallback((segments: readonly string[], value: unknown) => {
    setEdits((current) => stageEdit(current, segments, value))
  }, [])
  const unstage = useCallback((segments: readonly string[]) => {
    setEdits((current) => unstageEdit(current, segments))
  }, [])
  const reconcile = useCallback((accounts: (edit: StagedEdit) => boolean) => {
    setEdits((current) => {
      const kept = current.filter(accounts)
      // The same array back when nothing was dropped, so a reconcile inside an
      // effect cannot re-render forever on a state that did not change.
      return kept.length === current.length ? current : kept
    })
  }, [])
  const clear = useCallback(() => setEdits([]), [])

  return useMemo(
    () => ({ edits, stagedAt, stage, unstage, reconcile, clear }),
    [edits, stagedAt, stage, unstage, reconcile, clear],
  )
}
