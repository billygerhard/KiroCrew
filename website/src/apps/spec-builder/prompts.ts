// Text sent TO the agent, not shown to the user. Deliberately English and NOT
// translated: it is an instruction the model reads, and a localized instruction would
// change the model's behaviour per user locale. This module is ignored by path in
// website/eslint.i18n.config.js for that reason.

/** Header for the batch of passage comments a review sends as one turn. */
export const REVIEW_FEEDBACK_HEADER =
  'Review feedback on the spec documents — address each item below, then update the'
  + ' affected file(s):\n'

/** One numbered comment: the quoted passage followed by the reviewer's note. */
export function reviewFeedbackItem(index: number, quote: string, feedback: string): string {
  return (
    index + '. Regarding this passage:\n> ' + quote.replace(/\n/g, '\n> ')
    + '\n\n   Feedback: ' + feedback
  )
}

/** Section header naming the document the following comments belong to. */
export function reviewFeedbackFileHeader(file: string): string {
  return '\n## ' + file + '\n'
}

/**
 * Sent as the next chat message once the ENGINE has recorded the approval and
 * authorised the transition, so the agent starts authoring the document the
 * engine named. Agent-directed instruction text, never rendered -- localising it
 * would change what the model is told per user locale.
 *
 * Keyed by the phase the engine says the spec is moving TO, not by a transition
 * this file decided. There used to be a map here keyed on the phase being left,
 * and the surface picked from it and sent "approved -- proceed" with nothing
 * having approved anything: the engine had no approval on record and would have
 * refused the move. A key absent from this table means the engine reported a
 * destination there is no authoring prompt for (``ready`` is the ordinary case),
 * and nothing is sent.
 */
export const AUTHOR_PHASE_PROMPT: Record<string, string> = {
  design: 'Requirements approved — proceed to Phase 2 (Design). Keep .spec-state.json updated.',
  tasks: 'Design approved — proceed to Phase 3 (Tasks). Keep .spec-state.json updated.',
}
