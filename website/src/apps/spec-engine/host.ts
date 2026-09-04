/**
 * Host boundary for the Spec Engine app.
 *
 * ## Why this file exists
 *
 * The Spec Engine ships today compiled into the dashboard's builtin bundle, but
 * it is meant to become a loadable external-app later. Every symbol the app
 * consumes from the REST of the website (anything outside
 * `src/apps/spec-engine/`) is re-exported here, and every other file in the
 * directory imports those symbols from `./host` instead of reaching out with a
 * `../../` path of its own. When the app is repackaged, only this one file has
 * to be repointed at whatever the external-app runtime provides (an injected
 * host object, a shim, etc.) — the ~14 consuming files never change.
 *
 * The `SpecEngineHostBoundary` test (`website/src/test/`) enforces the rule:
 * no source file under `src/apps/spec-engine/` except this one may carry an
 * import specifier that resolves outside the directory.
 *
 * ## Scope
 *
 * Only cross-website edges are funneled here — relative `../` reaches into the
 * shared website tree. Bare npm-package imports (`react`,
 * `@tanstack/react-query`, `lucide-react`) are NOT host edges: they are ordinary
 * third-party dependencies an external app declares for itself, so they stay as
 * direct imports at each call site and the boundary test ignores them.
 *
 * ## Type-only exception
 *
 * None. `tsconfig.app.json` sets `isolatedModules: true`, so the one type this
 * boundary carries (`FormatUnit`) is re-exported with `export type` below, which
 * erases cleanly. No symbol had to stay a direct outside import.
 */

// i18n — translate function (see ../../i18n/t)
export { i18nT } from '../../i18n/t'

// i18n — locale-aware formatters (see ../../i18n/format)
export { fmtNumber, fmtDateTime, fmtDuration } from '../../i18n/format'
export type { FormatUnit } from '../../i18n/format'

// Shared UI — the project picker dialog (default export, re-exported named)
export { default as ProjectPicker } from '../../components/ProjectPicker'
