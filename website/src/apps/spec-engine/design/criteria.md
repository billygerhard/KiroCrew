# Selection criteria for the Operator_Surface mockup

Written **before** either mockup was judged and before any reviewer saw them.
It is a separate file from `selection.md` so that the ordering is checkable
rather than asserted: the reviewer agent was handed this file as its rubric,
and `selection.md` was written afterwards from what came back.

The criteria are ordered. When two options trade against each other, the
earlier criterion wins. C1 is first because it is the requirement the owner
rejected the prior interface over, and an option that loses C1 cannot be
rescued by winning everything below it.

## C1 — Reviewer, not driver (blocking)

On the default view, with configuration present, the primary affordances act on
work the engine already produced: render a verdict, release held feedback,
redispatch, resume, raise a ceiling, tear down. **Nothing on the default path
asks a human to compose or hand-edit spec prose.** An option that puts an
authoring surface — an editor for requirements/design/tasks, a chat composer
that drives authoring — on the default path fails outright.

Judged by: naming every interactive control reachable without navigation and
classifying each as *operate on engine output* or *produce content by hand*.

## C2 — Keystrokes and screens per verdict

For a backlog of five runs waiting on a person, count the interactions to
render a verdict on each in turn, including whatever is needed to see enough to
decide. Fewer is better; a model that requires opening and dismissing a
container per run is penalised against one that does not.

## C3 — Fidelity to the engine's waiting model

The surface distinguishes the three `WaitingOn` reasons (`review`, `budget`,
`stall`) rather than presenting one undifferentiated "waiting" list, and it
surfaces `revision_exhausted` and `feedback_needs_human` as distinct states
with distinct actions. A run parked on a budget ceiling and a run parked on a
verdict must not look like the same job.

## C4 — Untrusted text cannot move the controls

Submitter text is bounded when collapsed **and when expanded**. Expanding must
not displace the verdict controls or the blocks below it — an expanded form that
grows to its content's height fails this even if the collapsed form is clamped.
The text must be rendered as text; a mockup that demonstrates this with an
escaped markup payload scores above one that only claims it.

## C5 — Safety controls never behind navigation

The kill switch and the spend figure are visible on every view, at every scroll
position, without navigating. Engaging is at least two deliberate steps. The
engaged state is unmissable, not a small badge.

## C6 — First-run leads with the assistant

With no configuration, the page's primary content is the setup assistant flow,
not an empty form and not a board of zero runs. The flow shows the evidence
behind each inference, shows a refused-inference as a refusal naming the
ambiguity, treats an unanswered autonomy rung as unanswered rather than "no",
and demands an approver identity before apply.

## C7 — Config editing honesty

`config.json` is presented as the source of truth and the write path. Per-role
model and effort are visible per role. The segment-wise resolution is shown —
which dotted paths were consulted and which one won — rather than only the
answer. A per-role reset names the exact node it clears, so nobody clears a
profile believing they cleared a project override. No concrete model id appears
as a default.

## C8 — Holds at both ends of the population

Legible and unembarrassing at 3 runs; workable at 40 without the layout
changing kind (no switch from cards to a table as the population grows, no
horizontal scroll to see a run's state).

## C9 — Token fidelity and theme portability

Every colour, radius and shadow is a token from `website/src/index.css`. Both
polarities are readable without per-theme special-casing. Icons are inline SVG;
no emoji anywhere.

## C10 — Implementation cost

Cost to build against the dashboard's existing component vocabulary and the
already-designed backend routes. A tie-breaker only: it must not outrank C1–C7.
