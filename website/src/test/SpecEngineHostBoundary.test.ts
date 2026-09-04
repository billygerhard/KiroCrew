/**
 * The Spec Engine host boundary is enforced here.
 *
 * The app is compiled into the dashboard's builtin bundle today but is meant to
 * become a loadable external app later. `src/apps/spec-engine/host.ts` is the
 * single seam through which every symbol the app borrows from the rest of the
 * website is re-exported, so a repackaging touches one file. That guarantee only
 * holds if no OTHER file in the directory quietly reaches across the boundary on
 * its own — which is exactly what a human eye stops catching once the directory
 * has two dozen files. So the rule is machine-checked.
 *
 * ## What counts as crossing the boundary
 *
 * An import specifier that RESOLVES outside `src/apps/spec-engine/`:
 *   - a relative path that climbs out (`../`, `../../…`), or
 *   - a project alias into the shared tree (`@/…`, `src/…`).
 *
 * What does NOT cross it, and is deliberately allowed:
 *   - an intra-directory relative import (`./api`, `./host`, `./stages`), and
 *   - a bare npm-package specifier (`react`, `@tanstack/react-query`,
 *     `lucide-react`). Those are ordinary third-party dependencies an external
 *     app declares for itself; they are not a website edge and the boundary file
 *     does not funnel them.
 *
 * `host.ts` is the ONE file exempt from the rule, because being the boundary is
 * its whole job. If a future symbol legitimately cannot be funneled (a type-only
 * import that breaks erasability under `isolatedModules`), it is documented in
 * host.ts's header and would be the only other permitted exception — none exists
 * today.
 */
import { describe, it, expect } from 'vitest'
import { readdirSync, readFileSync } from 'node:fs'
import { resolve } from 'node:path'

const APP_DIR = resolve(process.cwd(), 'src/apps/spec-engine')

/** The boundary file itself is allowed to reach outside — that is its purpose. */
const EXEMPT_FILES = new Set(['host.ts'])

/**
 * Every `.ts`/`.tsx` source file directly under the app directory.
 *
 * Not recursive: the app has no source subdirectories (only `design/`, which
 * holds markdown and HTML mockups, never compiled code). A `.d.ts` would be a
 * type declaration, not an import site, so it is excluded too.
 */
function appSourceFiles(): string[] {
  return readdirSync(APP_DIR, { withFileTypes: true })
    .filter((entry) => entry.isFile())
    .map((entry) => entry.name)
    .filter((name) => /\.tsx?$/.test(name) && !name.endsWith('.d.ts'))
    .filter((name) => !EXEMPT_FILES.has(name))
}

/**
 * Pull every import/re-export specifier out of a source file.
 *
 * Matches the module specifier in `import … from '…'`, side-effect `import '…'`,
 * and `export … from '…'`. A dynamic `import('…')` is caught by the same string
 * shape via the `from`-less arm. This is a lexical scan, not a parse — good
 * enough because the only thing under test is the LITERAL specifier string, and
 * a specifier is always a plain string literal.
 */
function importSpecifiers(source: string): string[] {
  const specifiers: string[] = []
  const re = /(?:import|export)\b[^'"`]*?from\s*['"]([^'"]+)['"]|import\s*['"]([^'"]+)['"]|import\s*\(\s*['"]([^'"]+)['"]\s*\)/g
  let m: RegExpExecArray | null
  while ((m = re.exec(source)) !== null) {
    specifiers.push(m[1] ?? m[2] ?? m[3])
  }
  return specifiers
}

/**
 * True when a specifier resolves OUTSIDE the app directory.
 *
 * `./…`  — intra-directory, allowed.
 * `../…` — climbs out, forbidden.
 * `@/…` or `src/…` — alias into the shared tree, forbidden.
 * anything else (`react`, `@tanstack/react-query`) — bare package, allowed.
 */
function reachesOutside(specifier: string): boolean {
  if (specifier.startsWith('./')) return false
  if (specifier.startsWith('../')) return true
  if (specifier.startsWith('@/') || specifier.startsWith('src/')) return true
  return false
}

describe('Spec Engine host boundary', () => {
  const files = appSourceFiles()

  it('finds spec-engine source files to check', () => {
    // A silently-empty file list would make every assertion below vacuous.
    expect(files.length).toBeGreaterThan(0)
  })

  it('routes every cross-website import through ./host', () => {
    const offenders: string[] = []
    for (const name of files) {
      const source = readFileSync(resolve(APP_DIR, name), 'utf8')
      for (const specifier of importSpecifiers(source)) {
        if (reachesOutside(specifier)) offenders.push(`${name}: ${specifier}`)
      }
    }
    expect(offenders).toEqual([])
  })
})
