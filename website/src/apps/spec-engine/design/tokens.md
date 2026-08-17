# Where the mockups' tokens come from

Both mockups inline a token block copied from `website/src/index.css` — the
dark block (`:root,[data-theme="dark"],[data-theme="amber-dark"]`), the light
block (`[data-theme="light"],...`), the shared radii from `:root`, and the
`--font-body` / `--mono` stacks. They are copied rather than imported because a
mockup has to open from the filesystem with no build step, and a mockup that
pulled the real stylesheet would also pull the whole dashboard's layout rules
and stop being a proposal about layout.

Two deliberate edits to the copies:

- The `KC * Fallback` alias families are dropped from the font stacks. Those
  names resolve through `@font-face` rules in `index.css` that the mockups do
  not carry, so keeping them would be dead text. `'Space Grotesk'` and
  `'JetBrains Mono'` are kept ahead of the system stack: if the host has them,
  the preview matches the dashboard; if not, it falls through the same way.
- `color-mix()` ramps (`--ctx-*`) are omitted. Neither mockup shows a context
  breakdown.

Nothing else is invented. A colour, radius, or shadow that is not a token in
`index.css` should not appear in either file, and a reviewer checking token
fidelity can diff the inlined block against the source block by name.

Consequence worth stating: because the block is a copy, it goes stale. It is a
design artifact, not a shipped stylesheet — the implementation under task 6.x
consumes the real tokens by cascade and must not copy this block forward.
