/**
 * Create Folders From Project — scan a project tree, tick what you want, create it.
 *
 * The page is a thin surface over two host endpoints and holds no scanning or
 * creation logic of its own. Three properties are worth stating because they are
 * what make the flow safe rather than merely convenient:
 *
 *  - **A scan is read-only, so the preview is the confirmation step.** Nothing is
 *    created until the user presses the create button, and what is sent is
 *    exactly the set ticked at that moment.
 *  - **Server prose is rendered verbatim.** A refused root produces the same
 *    sentence that creating a folder by hand would have produced. Re-wording it
 *    here would make two surfaces disagree about one refusal, so the server's
 *    `error` text is displayed as-is and only the surrounding chrome is
 *    localized.
 *  - **A stale preview is recoverable, not fatal.** The server refuses a
 *    selection it no longer offers; that is the user's tree having changed under
 *    an open page, so it resolves to a re-scan prompt rather than an error.
 *
 *  - **The root is chosen with the same picker as every other project
 *    directory.** A scan root is a project directory, so it is picked from the
 *    shared `ProjectPicker` (recent + browse) rather than pasted. Reusing it
 *    rather than reimplementing means a directory reachable in the sidebar's
 *    folder settings is reachable here too, spelled the same way. Free typing
 *    stays available for a path that is faster to say than to browse to.
 *
 * Everything interactive is a native control (`input type=checkbox`, `button`,
 * `input type=text`), which is what makes the whole preview keyboard-operable
 * without any key handling of its own.
 */
import { useCallback, useMemo, useRef, useState } from 'react'
import { FolderPlus, FolderCheck, FolderOpen, AlertTriangle, RefreshCw } from 'lucide-react'
import { Card, CardTitle, Btn, SendBtn, Input, Badge, EmptyState, PageHeader } from '../../components/ui'
import ProjectPicker from '../../components/ProjectPicker'
import { i18nT } from '../../i18n/t'
import {
  scanProject,
  scaffoldProject,
  ScaffoldApiError,
  STATUS_EMPTY,
  CODE_SELECTION_STALE,
  type Candidate,
  type ScanResult,
  type ScaffoldResult,
} from './api'

/** Rows of one preview group, resolved from the server's path buckets. */
interface Group {
  parentPath: string | null
  rows: Candidate[]
}

/** Sort rank per tier: confident rows lead, offered rows follow. */
const TIER_RANK: Record<Candidate['tier'], number> = { auto: 0, offered: 1 }

/**
 * Order rows by confidence, keeping the server's path order inside each tier.
 *
 * The two orderings are complementary rather than competing: confidence picks
 * which block a row sits in, and the server's path order picks its position
 * within that block. A stable sort is what makes the second half true, so this
 * relies on `Array#sort` being stable rather than comparing paths as a
 * tiebreak — comparing them would impose a *string* order where the server
 * already supplied a tree order.
 *
 * Presentation only: the tier a row carries and the set that is ticked are both
 * the server's, and neither is touched here.
 */
function byConfidence(rows: Candidate[]): Candidate[] {
  return [...rows].sort((a, b) => TIER_RANK[a.tier] - TIER_RANK[b.tier])
}

/** Whether a group holds anything the server was confident about. */
function hasConfident(group: Group): boolean {
  return group.rows.some((row) => row.tier === 'auto')
}

/**
 * Resolve the server's `groups` (parent -> paths) into rows.
 *
 * The buckets and their order come from the server so every surface renders the
 * same grouping — a per-group select-all control is only coherent if the groups
 * themselves are agreed on. Any candidate the buckets somehow omit is appended
 * as its own trailing group rather than dropped, so a row can never become
 * invisible-but-selectable.
 *
 * Rows and groups are then ordered by confidence, because the preview is read
 * top-down and the ticked rows are the ones the user is deciding about: a group
 * of purely offered candidates is an opt-in, and burying the ticked ones below
 * it makes the default selection look like it is somewhere else. The
 * partitioning is stable, so within each class the server's path order — and
 * therefore parent-before-child — survives.
 */
function toGroups(scan: ScanResult): Group[] {
  const byPath = new Map(scan.candidates.map((c) => [c.path, c]))
  const grouped: Group[] = []
  const placed = new Set<string>()
  for (const bucket of scan.groups) {
    const rows: Candidate[] = []
    for (const path of bucket.paths) {
      const row = byPath.get(path)
      if (!row) continue
      rows.push(row)
      placed.add(path)
    }
    if (rows.length) grouped.push({ parentPath: bucket.parent_path, rows: byConfidence(rows) })
  }
  const orphans = scan.candidates.filter((c) => !placed.has(c.path))
  if (orphans.length) grouped.push({ parentPath: null, rows: byConfidence(orphans) })
  return [...grouped.filter(hasConfident), ...grouped.filter((g) => !hasConfident(g))]
}

/** The server's own default tick state, which already excludes existing folders. */
function defaultSelection(scan: ScanResult): Set<string> {
  return new Set(scan.candidates.filter((c) => c.selected).map((c) => c.path))
}

function TierBadge({ tier }: { tier: Candidate['tier'] }) {
  return tier === 'auto'
    ? <Badge variant="ok">{i18nT('apps.projectScaffolder.projectScaffolderPage.tier_confident')}</Badge>
    : <Badge variant="muted">{i18nT('apps.projectScaffolder.projectScaffolderPage.tier_offered')}</Badge>
}

function CandidateRow({ row, checked, onToggle }: {
  row: Candidate
  checked: boolean
  onToggle: (path: string, next: boolean) => void
}) {
  const alreadySetUp = i18nT('apps.projectScaffolder.projectScaffolderPage.already_set_up')
  return (
    <li className="flex items-start gap-2.5 py-1.5" data-testid="candidate-row">
      <input
        type="checkbox"
        className="mt-[3px] shrink-0 accent-[var(--accent)] cursor-pointer disabled:cursor-not-allowed"
        // An existing folder is reported but never re-created, so its row is
        // informational: disabling the box is what makes that unmissable rather
        // than leaving a tick the create step would silently ignore.
        disabled={row.existing}
        checked={checked}
        onChange={(e) => onToggle(row.path, e.target.checked)}
        // The visible name repeats across sibling directories, so the path is
        // what disambiguates one checkbox from another in a screen-reader list.
        aria-label={row.existing ? `${row.path} (${alreadySetUp})` : row.path}
      />
      <div className="min-w-0 flex-1">
        <div className="flex items-center gap-2 flex-wrap">
          <span className="text-[13px] font-medium text-text-strong">{row.name}</span>
          <TierBadge tier={row.tier} />
          {row.existing && (
            <span className="inline-flex items-center gap-1 text-[11.5px] text-muted" data-testid="already-set-up">
              <FolderCheck size={12} className="lucide-inline" />
              {alreadySetUp}
            </span>
          )}
        </div>
        <div className="text-[11.5px] text-muted font-mono break-all mt-0.5">{row.path}</div>
        {row.signals.length > 0 && (
          <div className="text-[11.5px] text-muted mt-0.5">
            <span className="font-semibold">
              {i18nT('apps.projectScaffolder.projectScaffolderPage.signals')}
            </span>{' '}
            <span className="font-mono">{row.signals.join(', ')}</span>
          </div>
        )}
      </div>
    </li>
  )
}

/** Last segment of an absolute directory path, on either separator. */
function baseName(path: string): string {
  const segments = path.split(/[/\\]+/).filter(Boolean)
  return segments[segments.length - 1] ?? path
}

/**
 * Say what a group IS, not just where it is.
 *
 * A bare absolute path is ambiguous here: the same directory appears both as a
 * ticked row near the top of the preview and as the heading of the group holding
 * the sub-packages found inside it, so a heading that is only a path reads as if
 * that package had been moved out of the list rather than as a container for
 * what sits under it. Naming the relationship is what disambiguates the two
 * roles; the full path stays as secondary text because it is what tells apart
 * two directories sharing a basename.
 *
 * The scan root keeps its own wording: nothing is nested inside it in the sense
 * this label means, and its rows are the top level of what was found.
 */
function GroupHeading({ parentPath, root }: { parentPath: string | null; root: string }) {
  if (parentPath === null || parentPath === root) {
    return (
      <span className="text-[11.5px] font-semibold text-muted">
        {i18nT('apps.projectScaffolder.projectScaffolderPage.directly_under_the_root')}
      </span>
    )
  }
  return (
    <span className="flex flex-col min-w-0" data-testid="group-heading">
      <span className="text-[11.5px] font-semibold text-muted">
        {i18nT('apps.projectScaffolder.projectScaffolderPage.nested_inside_name', {
          name: baseName(parentPath),
        })}
      </span>
      <span className="text-[11px] text-muted font-mono break-all">{parentPath}</span>
    </span>
  )
}

function PreviewGroup({ group, root, selected, onToggle, onBulk }: {
  group: Group
  root: string
  selected: Set<string>
  onToggle: (path: string, next: boolean) => void
  onBulk: (paths: string[], next: boolean) => void
}) {
  // Only rows that can actually change state are worth bulk-toggling; an
  // existing folder is never created, so including it would make "select all"
  // claim more than it did.
  const togglePaths = group.rows.filter((r) => !r.existing).map((r) => r.path)
  return (
    <fieldset className="border-0 p-0 m-0 mb-4" data-testid="preview-group">
      <legend className="flex items-start gap-2 flex-wrap w-full mb-1">
        <GroupHeading parentPath={group.parentPath} root={root} />
        {togglePaths.length > 0 && (
          <span className="flex items-center gap-1.5">
            <Btn type="button" onClick={() => onBulk(togglePaths, true)}>
              {i18nT('apps.projectScaffolder.projectScaffolderPage.select_all')}
            </Btn>
            <Btn type="button" onClick={() => onBulk(togglePaths, false)}>
              {i18nT('apps.projectScaffolder.projectScaffolderPage.select_none')}
            </Btn>
          </span>
        )}
      </legend>
      <ul className="list-none p-0 m-0 divide-y divide-border">
        {group.rows.map((row) => (
          <CandidateRow
            key={row.path}
            row={row}
            checked={selected.has(row.path)}
            onToggle={onToggle}
          />
        ))}
      </ul>
    </fieldset>
  )
}

function WarningList({ warnings }: { warnings: string[] }) {
  if (!warnings.length) return null
  return (
    <div className="bg-warn-subtle text-warn rounded-md px-3 py-2 mb-3" data-testid="scan-warnings">
      <div className="flex items-center gap-1.5 text-[12px] font-semibold">
        <AlertTriangle size={13} className="lucide-inline" />
        {i18nT('apps.projectScaffolder.projectScaffolderPage.warnings')}
      </div>
      <ul className="list-disc pl-5 m-0 mt-1 text-[11.5px]">
        {/* Server prose, rendered verbatim. */}
        {warnings.map((w) => <li key={w}>{w}</li>)}
      </ul>
    </div>
  )
}

function ResultsCard({ result }: { result: ScaffoldResult }) {
  return (
    <Card data-testid="scaffold-results">
      <CardTitle>{i18nT('apps.projectScaffolder.projectScaffolderPage.results')}</CardTitle>
      <div className="flex items-center gap-2 flex-wrap mb-3">
        <Badge variant="ok" data-testid="result-created">
          {i18nT('apps.projectScaffolder.projectScaffolderPage.n_created', { n: result.created.length })}
        </Badge>
        <Badge variant="muted" data-testid="result-skipped">
          {i18nT('apps.projectScaffolder.projectScaffolderPage.n_already_existed', { n: result.skipped_existing.length })}
        </Badge>
        {result.failed.length > 0 && (
          <Badge variant="err" data-testid="result-failed">
            {i18nT('apps.projectScaffolder.projectScaffolderPage.n_failed', { n: result.failed.length })}
          </Badge>
        )}
      </div>
      {result.created.length > 0 && (
        <ul className="list-none p-0 m-0 text-[11.5px] text-muted font-mono">
          {result.created.map((c) => <li key={c.path} className="break-all py-0.5">{c.path}</li>)}
        </ul>
      )}
      {result.failed.length > 0 && (
        <ul className="list-none p-0 m-0 mt-3" data-testid="failed-rows">
          {result.failed.map((f) => (
            <li key={f.path} className="py-1 border-t border-border first:border-t-0">
              <div className="text-[11.5px] font-mono text-text break-all">{f.path}</div>
              {/* The server's own refusal prose and its machine-readable id. */}
              <div className="text-[11.5px] text-danger">{f.error}</div>
              <div className="text-[11px] text-muted font-mono">{f.code}</div>
            </li>
          ))}
        </ul>
      )}
      <WarningList warnings={result.warnings} />
    </Card>
  )
}

export default function ProjectScaffolderPage() {
  const [root, setRoot] = useState('')
  const [scan, setScan] = useState<ScanResult | null>(null)
  const [selected, setSelected] = useState<Set<string>>(new Set())
  const [result, setResult] = useState<ScaffoldResult | null>(null)
  const [rootError, setRootError] = useState('')
  const [stale, setStale] = useState<string[] | null>(null)
  const [busy, setBusy] = useState<'' | 'scan' | 'create'>('')
  const [pickerOpen, setPickerOpen] = useState(false)
  const rootInputRef = useRef<HTMLInputElement>(null)
  const browseRef = useRef<HTMLButtonElement>(null)

  const groups = useMemo(() => (scan ? toGroups(scan) : []), [scan])
  const selectedCount = selected.size

  const runScan = useCallback(async (path: string) => {
    const trimmed = path.trim()
    if (!trimmed || busy) return
    setBusy('scan')
    // A new scan invalidates the previous preview, its selection, and any prior
    // outcome, so all three are cleared before the request rather than after:
    // leaving a stale preview on screen during the scan invites confirming it.
    setRootError('')
    setStale(null)
    setResult(null)
    setScan(null)
    setSelected(new Set())
    try {
      const next = await scanProject(trimmed)
      setScan(next)
      setSelected(defaultSelection(next))
    } catch (err) {
      // Every scan refusal is about the root field, so it renders inline against
      // that field rather than as a page-level banner.
      setRootError(err instanceof Error ? err.message : String(err))
      rootInputRef.current?.focus()
    } finally {
      setBusy('')
    }
  }, [busy])

  const toggle = useCallback((path: string, next: boolean) => {
    setSelected((prev) => {
      const copy = new Set(prev)
      if (next) copy.add(path)
      else copy.delete(path)
      return copy
    })
  }, [])

  const bulk = useCallback((paths: string[], next: boolean) => {
    setSelected((prev) => {
      const copy = new Set(prev)
      for (const path of paths) {
        if (next) copy.add(path)
        else copy.delete(path)
      }
      return copy
    })
  }, [])

  const create = useCallback(async () => {
    if (!scan || busy) return
    setBusy('create')
    setStale(null)
    try {
      // Exactly the paths ticked right now. Order is the preview's own, and the
      // server re-derives the set from a fresh scan regardless.
      const picked = scan.candidates.filter((c) => selected.has(c.path)).map((c) => c.path)
      setResult(await scaffoldProject(scan.root, picked))
    } catch (err) {
      if (err instanceof ScaffoldApiError && err.code === CODE_SELECTION_STALE) {
        // Not a failure of the request so much as of the preview: the tree moved
        // under an open page. Offer the one action that fixes it.
        setStale(err.unknown)
      } else {
        setRootError(err instanceof Error ? err.message : String(err))
      }
    } finally {
      setBusy('')
    }
  }, [scan, selected, busy])

  const isEmpty = scan !== null && scan.status === STATUS_EMPTY

  return (
    <div className="flex flex-col h-full min-h-0">
      <PageHeader
        title={i18nT('apps.projectScaffolder.projectScaffolderPage.create_folders_from_project')}
        subtitle={i18nT('apps.projectScaffolder.projectScaffolderPage.scan_a_project_directory_then_create_a_sidebar')}
      />
      <div className="flex-1 overflow-y-auto px-6 pb-6">
        <Card>
          <CardTitle>
            <FolderPlus size={14} className="lucide-inline" />
            {i18nT('apps.projectScaffolder.projectScaffolderPage.project_directory')}
          </CardTitle>
          {/* A form so Enter in the field submits, which is the shortest keyboard
              path from a chosen path to seeing the preview. */}
          <form
            className="flex items-center gap-2"
            onSubmit={(e) => { e.preventDefault(); void runScan(root) }}
          >
            <Input
              ref={rootInputRef}
              value={root}
              onChange={(e) => setRoot(e.target.value)}
              placeholder={i18nT('apps.projectScaffolder.projectScaffolderPage.absolute_path_to_a_project_directory')}
              aria-label={i18nT('apps.projectScaffolder.projectScaffolderPage.project_directory')}
              aria-invalid={rootError ? true : undefined}
              aria-describedby={rootError ? 'scaffolder-root-error' : undefined}
              spellCheck={false}
              autoComplete="off"
            />
            {/* type=button so it never submits the form it sits inside. */}
            <Btn
              type="button"
              ref={browseRef}
              data-testid="scaffolder-browse"
              onClick={() => setPickerOpen(true)}
            >
              <FolderOpen size={13} className="lucide-inline" />
              {i18nT('apps.projectScaffolder.projectScaffolderPage.browse')}
            </Btn>
            <SendBtn type="submit" disabled={!root.trim() || busy !== ''}>
              {busy === 'scan'
                ? i18nT('apps.projectScaffolder.projectScaffolderPage.scanning')
                : i18nT('apps.projectScaffolder.projectScaffolderPage.scan')}
            </SendBtn>
          </form>
          {rootError && (
            // Verbatim server prose: the same sentence manual folder creation gives.
            <div
              id="scaffolder-root-error"
              role="alert"
              className="text-[12px] text-danger mt-2"
              data-testid="root-error"
            >
              {rootError}
            </div>
          )}
          {/* Portals to the body and anchors to the Browse button. Reused rather
           *  than reimplemented so picking a scan root stays identical to picking
           *  any other project directory.
           *
           *  A pick fills the field and stops there — it does not scan. That
           *  mirrors the folder-settings picker, where a selection stages into the
           *  draft and a separate action commits it. The scan is read-only, so
           *  auto-running it would be harmless but would make one picker apply
           *  immediately and the other not; instead focus returns to the field, so
           *  the path is visible and editable and Enter scans it. */}
          {pickerOpen && (
            <ProjectPicker
              open={true}
              onOpenChange={(o) => { if (!o) setPickerOpen(false) }}
              anchorRef={browseRef}
              onSelect={(path) => {
                setRoot(path)
                setPickerOpen(false)
                rootInputRef.current?.focus()
              }}
            />
          )}
        </Card>

        {isEmpty && scan && (
          <Card>
            <WarningList warnings={scan.warnings} />
            <EmptyState
              icon={<FolderPlus />}
              title={i18nT('apps.projectScaffolder.projectScaffolderPage.no_sub_projects_found')}
              subtitle={i18nT('apps.projectScaffolder.projectScaffolderPage.nothing_under_this_directory_looked_like_a_proje')}
              testId="scan-empty"
              action={
                <SendBtn onClick={() => void create()} disabled={busy !== ''}>
                  {busy === 'create'
                    ? i18nT('apps.projectScaffolder.projectScaffolderPage.creating')
                    : i18nT('apps.projectScaffolder.projectScaffolderPage.create_the_root_folder_only')}
                </SendBtn>
              }
            />
          </Card>
        )}

        {scan && !isEmpty && (
          <Card>
            <CardTitle>
              {i18nT('apps.projectScaffolder.projectScaffolderPage.preview')}
            </CardTitle>
            <WarningList warnings={scan.warnings} />
            <div className="flex items-center gap-2 flex-wrap mb-3 text-[12px] text-muted">
              <span className="font-mono break-all text-text">{scan.root}</span>
              {scan.root_existing && (
                <Badge variant="muted" data-testid="root-existing">
                  {i18nT('apps.projectScaffolder.projectScaffolderPage.already_set_up')}
                </Badge>
              )}
            </div>
            {groups.map((group) => (
              <PreviewGroup
                key={group.parentPath ?? ''}
                group={group}
                root={scan.root}
                selected={selected}
                onToggle={toggle}
                onBulk={bulk}
              />
            ))}
            {stale && (
              <div className="bg-warn-subtle text-warn rounded-md px-3 py-2 mb-3" data-testid="stale-selection">
                <div className="text-[12px]">
                  {i18nT('apps.projectScaffolder.projectScaffolderPage.this_preview_no_longer_matches_the_directory_on')}
                </div>
                {stale.length > 0 && (
                  <ul className="list-disc pl-5 m-0 mt-1 text-[11.5px] font-mono">
                    {stale.map((path) => <li key={path} className="break-all">{path}</li>)}
                  </ul>
                )}
                <Btn type="button" className="mt-2" onClick={() => void runScan(scan.root)}>
                  <RefreshCw size={12} className="lucide-inline" />
                  {i18nT('apps.projectScaffolder.projectScaffolderPage.re_scan')}
                </Btn>
              </div>
            )}
            <div className="flex items-center justify-between gap-3 flex-wrap border-t border-border pt-3">
              <span className="text-[12px] text-muted" data-testid="selected-count">
                {i18nT('apps.projectScaffolder.projectScaffolderPage.n_selected', { n: selectedCount })}
              </span>
              <SendBtn onClick={() => void create()} disabled={busy !== ''}>
                {busy === 'create'
                  ? i18nT('apps.projectScaffolder.projectScaffolderPage.creating')
                  : i18nT('apps.projectScaffolder.projectScaffolderPage.create_folders')}
              </SendBtn>
            </div>
          </Card>
        )}

        {result && <ResultsCard result={result} />}
      </div>
    </div>
  )
}
