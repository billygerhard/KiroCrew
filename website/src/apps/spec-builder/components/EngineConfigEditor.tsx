// EngineConfigEditor — the editing half of the engine configuration surface.
//
// Four rules this component keeps, each answering a way a config editor goes
// wrong:
//
//   * **one write path.** Every edit becomes ONE patch on `engineApi.putConfig`,
//     which is the app's relay onto `ConfigStore.write(surface=DASHBOARD_SURFACE)`.
//     Nothing here validates a value first: the engine owns the registry bounds,
//     the scope rules, the config-only fence and the screening opt-out rule, and a
//     second copy of any of them in the browser would drift from the one that
//     decides. What the panel does with the answer is show it.
//   * **a refusal reads as a refusal.** The engine's own message is displayed and
//     the form stays dirty, so a rejected save never looks saved. The reverse --
//     clearing the form on any response -- is how an operator comes back to a
//     value they believe they set.
//   * **origin comes from the engine.** Every row's layer is the `origin` field,
//     and every workflow stage's layer is `stage_origins`' own answer. Neither is
//     inferred by comparing a value against a default: a value equal to its
//     default may still be an explicit override, and a byte-identical stage
//     override is still an override.
//   * **a domain with no editor says so.** The backend names which domains this
//     surface writes and why it does not write the rest (workflow and
//     quality-gate argv, program minimums, capability provider bindings). Those
//     render read-only with the reason rather than as a control that fails.
import { useMemo, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { AlertTriangle, Save, Undo2 } from 'lucide-react'
import {
  engineApi,
  originLabel,
  type EffectiveSetting,
  type EngineConfigResponse,
} from '../api'
import { Btn } from './shared'
import { Input } from '../../../components/ui'
import SimpleSelect from '../../../components/SimpleSelect'
import { i18nT } from '../../../i18n/t'

/** One pending edit: the dotted path it writes and the value to write. `null`
 *  DELETES the key, which is how the engine returns a setting to its bundled
 *  default — distinct from writing the default's current value, which pins it. */
interface PendingEdit {
  path: string[]
  value: unknown
}

/** Build the nested patch the engine's merge expects from flat path edits.
 *
 *  This is JSON shaping, not precedence: the engine merges key by key and treats
 *  `null` as a delete, both documented on its write path. Nothing here decides
 *  which layer wins. */
export function patchFrom(edits: Map<string, PendingEdit>): Record<string, unknown> {
  const patch: Record<string, unknown> = {}
  for (const { path, value } of edits.values()) {
    let node = patch
    for (const segment of path.slice(0, -1)) {
      const next = node[segment]
      if (next === undefined || next === null || typeof next !== 'object') node[segment] = {}
      node = node[segment] as Record<string, unknown>
    }
    node[path[path.length - 1]] = value
  }
  return patch
}

const editKey = (path: string[]) => path.join('.')

/** A setting's value coerced back to the kind the registry declared. The engine
 *  refuses a mistyped value, so this is about not sending a string where the
 *  operator typed a number — never about deciding whether the value is legal. */
function typedValue(row: EffectiveSetting, raw: string): unknown {
  if (row.kind === 'bool') return raw === 'true'
  if (row.kind === 'int') return raw.trim() === '' ? raw : Number.parseInt(raw, 10)
  if (row.kind === 'float') return raw.trim() === '' ? raw : Number.parseFloat(raw)
  return raw
}

/** Settings this scope may write, grouped by their dotted prefix. A setting not
 *  overridable at the scope on screen is offered read-only rather than collected
 *  and refused. */
function grouped(settings: Record<string, EffectiveSetting>): [string, EffectiveSetting[]][] {
  const groups = new Map<string, EffectiveSetting[]>()
  for (const key of Object.keys(settings)) {
    const group = key.split('.')[0]
    const rows = groups.get(group)
    if (rows) rows.push(settings[key])
    else groups.set(group, [settings[key]])
  }
  return [...groups.entries()].sort((a, b) =>
    a[0].localeCompare(b[0], undefined, { sensitivity: 'base' }),
  )
}

/** Records under `sources` / `cost_profiles`, read as objects without asserting
 *  a shape the document may not have: these are relayed as stored. */
function entries(node: unknown): [string, Record<string, unknown>][] {
  if (node === null || typeof node !== 'object') return []
  return Object.entries(node as Record<string, unknown>).filter(
    ([, value]) => value !== null && typeof value === 'object',
  ) as [string, Record<string, unknown>][]
}

const asRecord = (node: unknown): Record<string, unknown> =>
  node !== null && typeof node === 'object' ? (node as Record<string, unknown>) : {}

const REASON_KEY: Record<string, string> = {
  executes_argv: 'apps.specBuilder.engineOps.reason_executes_argv',
  host_assertion: 'apps.specBuilder.engineOps.reason_host_assertion',
  binds_provider: 'apps.specBuilder.engineOps.reason_binds_provider',
  argv_read_only: 'apps.specBuilder.engineOps.reason_argv_read_only',
}

function reasonText(code: string): string {
  return Object.prototype.hasOwnProperty.call(REASON_KEY, code) ? i18nT(REASON_KEY[code]) : code
}

export interface EngineConfigEditorProps {
  config: EngineConfigResponse
  /** Project scope on screen, forwarded to the workflow-origin read so the rows
   *  describe the same scope the settings above them do. */
  project?: string
}

export default function EngineConfigEditor({ config, project }: EngineConfigEditorProps) {
  const qc = useQueryClient()
  const [edits, setEdits] = useState<Map<string, PendingEdit>>(new Map())
  const [refusal, setRefusal] = useState('')
  const [saved, setSaved] = useState(false)

  const catalogs = config.catalogs
  const editors = config.domain_editors ?? []
  const editorFor = (domain: string) => editors.find((e) => e.domain === domain)
  const sources = entries(config.domains?.sources)
  const profiles = entries(config.domains?.cost_profiles)

  const originsQuery = useQuery({
    queryKey: ['spec-builder', 'engine-workflow-origins', project ?? ''],
    queryFn: () => engineApi.getWorkflowOrigins(project),
  })

  const save = useMutation({
    mutationFn: () => engineApi.putConfig(patchFrom(edits)),
    onSuccess: () => {
      setEdits(new Map())
      setRefusal('')
      setSaved(true)
      void qc.invalidateQueries({ queryKey: ['spec-builder', 'engine-config'] })
      void qc.invalidateQueries({ queryKey: ['spec-builder', 'engine-workflow-origins'] })
    },
    // The engine's own reason, and the edits are LEFT in place: a refused save
    // that cleared the form would read as saved.
    onError: (e: Error) => {
      setSaved(false)
      setRefusal(e.message)
    },
  })

  const setEdit = (path: string[], value: unknown) => {
    setSaved(false)
    setEdits((current) => {
      const next = new Map(current)
      next.set(editKey(path), { path, value })
      return next
    })
  }

  /** The value showing in a control: the pending edit if there is one, else what
   *  the engine says is in force. */
  const shown = (path: string[], fallback: unknown): unknown => {
    const pending = edits.get(editKey(path))
    return pending ? pending.value : fallback
  }

  const autonomyRows = useMemo(() => {
    const rows: { source: string; klass: string; specType: string; level: string }[] = []
    for (const [name, entry] of sources) {
      const autonomy = asRecord(entry.autonomy)
      for (const [klass, byType] of Object.entries(autonomy)) {
        for (const [specType, level] of Object.entries(asRecord(byType))) {
          rows.push({ source: name, klass, specType, level: String(level) })
        }
      }
    }
    return rows
  }, [sources])

  const dirty = edits.size > 0

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 16 }}>
      {/* ── registry settings, editable where the scope allows ─────────── */}
      {grouped(config.settings).map(([group, rows]) => (
        <div key={group}>
          <h4 style={{ margin: '0 0 4px' }}>{group}</h4>
          <table style={{ width: '100%', borderCollapse: 'collapse' }}>
            <thead>
              <tr>
                <th scope="col" style={{ textAlign: 'left' }}>
                  {i18nT('apps.specBuilder.engineOps.col_setting')}
                </th>
                <th scope="col" style={{ textAlign: 'left' }}>
                  {i18nT('apps.specBuilder.engineOps.col_effective')}
                </th>
                <th scope="col" style={{ textAlign: 'left' }}>
                  {i18nT('apps.specBuilder.engineOps.col_origin')}
                </th>
              </tr>
            </thead>
            <tbody>
              {rows.map((row) => {
                const path = row.key.split('.')
                // The registry's own answer about where a write is accepted. A
                // field offered outside it would collect an edit the engine
                // refuses.
                const writable = (row.scopes ?? []).includes('app')
                const current = shown(path, row.value)
                return (
                  <tr key={row.key} title={row.summary || ''}>
                    <td>{row.key}</td>
                    <td>
                      {!writable ? (
                        <span>
                          {String(row.value)}{' '}
                          <span>{i18nT('apps.specBuilder.engineOps.not_writable_here')}</span>
                        </span>
                      ) : row.choices && row.choices.length > 0 ? (
                        <SimpleSelect
                          aria-label={row.key}
                          options={row.choices}
                          value={String(current)}
                          onChange={(value) => setEdit(path, value)}
                        />
                      ) : row.kind === 'bool' ? (
                        <SimpleSelect
                          aria-label={row.key}
                          options={['true', 'false']}
                          optionLabels={[
                            i18nT('apps.specBuilder.engineOps.bool_true'),
                            i18nT('apps.specBuilder.engineOps.bool_false'),
                          ]}
                          value={String(current)}
                          onChange={(value) => setEdit(path, value === 'true')}
                        />
                      ) : (
                        <Input
                          aria-label={row.key}
                          value={String(current)}
                          onChange={(e) => setEdit(path, typedValue(row, e.target.value))}
                        />
                      )}
                    </td>
                    <td>
                      {originLabel(row.origin)}
                      {row.declared_at ? ` (${row.declared_at})` : ''}
                      {writable && !row.is_default && (
                        <Btn
                          label={
                            <>
                              <Undo2 className="lucide-inline" aria-hidden="true" />
                              {i18nT('apps.specBuilder.engineOps.reset')}
                            </>
                          }
                          ariaLabel={i18nT('apps.specBuilder.engineOps.reset_setting', {
                            setting: row.key,
                          })}
                          // null DELETES the key, which returns the setting to its
                          // bundled default. Writing the default's current value
                          // would PIN it, which is a different thing.
                          onClick={() => setEdit(path, null)}
                        />
                      )}
                    </td>
                  </tr>
                )
              })}
            </tbody>
          </table>
        </div>
      ))}

      {/* ── autonomy: how far unattended work may go, per source ───────── */}
      <section aria-label={i18nT('apps.specBuilder.engineOps.autonomy_section')}>
        <h4 style={{ margin: '0 0 4px' }}>
          {i18nT('apps.specBuilder.engineOps.autonomy_section')}
        </h4>
        <p style={{ margin: '0 0 6px' }}>{i18nT('apps.specBuilder.engineOps.autonomy_note')}</p>
        {sources.length === 0 ? (
          <p style={{ margin: 0 }}>{i18nT('apps.specBuilder.engineOps.no_sources')}</p>
        ) : autonomyRows.length === 0 ? (
          <p style={{ margin: 0 }}>{i18nT('apps.specBuilder.engineOps.autonomy_none')}</p>
        ) : (
          <ul style={{ margin: 0, paddingLeft: 18 }}>
            {autonomyRows.map((row) => {
              const path = ['sources', row.source, 'autonomy', row.klass, row.specType]
              return (
                <li key={editKey(path)}>
                  <span>{`${row.source} · ${row.klass} · ${row.specType}`}</span>{' '}
                  {/* The control names ITSELF with the dotted path it writes, which
                      is also what an operator needs when the engine refuses the
                      write and answers by path. */}
                  <SimpleSelect
                    aria-label={editKey(path)}
                    options={catalogs?.autonomy_levels ?? []}
                    value={String(shown(path, row.level))}
                    onChange={(value) => setEdit(path, value)}
                  />
                </li>
              )
            })}
          </ul>
        )}
        <AutonomyAdd
          sources={sources.map(([name]) => name)}
          catalogs={catalogs}
          onAdd={(path, level) => setEdit(path, level)}
        />
      </section>

      {/* ── watch sources: the enable, and the argv it will NOT edit ───── */}
      <section aria-label={i18nT('apps.specBuilder.engineOps.sources_section')}>
        <h4 style={{ margin: '0 0 4px' }}>
          {i18nT('apps.specBuilder.engineOps.sources_section')}
        </h4>
        {sources.length === 0 ? (
          <p style={{ margin: 0 }}>{i18nT('apps.specBuilder.engineOps.no_sources')}</p>
        ) : (
          <ul style={{ margin: 0, paddingLeft: 18 }}>
            {sources.map(([name, entry]) => {
              const path = ['sources', name, 'enabled']
              const poll = Array.isArray(entry.poll) ? (entry.poll as unknown[]).join(' ') : ''
              return (
                <li key={name}>
                  {/* Nested AND matched by id: the a11y rule wants both, and the
                      aria-label carries the dotted path so the control names the
                      key it writes. */}
                  <label htmlFor={editKey(path)}>
                    <input
                      id={editKey(path)}
                      type="checkbox"
                      aria-label={editKey(path)}
                      checked={shown(path, entry.enabled !== false) === true}
                      onChange={(e) => setEdit(path, e.target.checked)}
                    />{' '}
                    {name}
                  </label>
                  {poll && (
                    <>
                      {' '}
                      <code>{poll}</code>{' '}
                      <span>{reasonText(editorFor('watch_sources')?.reason_code ?? '')}</span>
                    </>
                  )}
                </li>
              )
            })}
          </ul>
        )}
      </section>

      {/* ── role assignments ──────────────────────────────────────────── */}
      <section aria-label={i18nT('apps.specBuilder.engineOps.roles_section')}>
        <h4 style={{ margin: '0 0 4px' }}>{i18nT('apps.specBuilder.engineOps.roles_section')}</h4>
        {profiles.length === 0 ? (
          <p style={{ margin: 0 }}>{i18nT('apps.specBuilder.engineOps.no_profiles')}</p>
        ) : (
          <ul style={{ margin: 0, paddingLeft: 18 }}>
            {profiles.map(([profileName, profile]) =>
              (catalogs?.roles ?? []).map((role) => {
                const assignment = asRecord(asRecord(profile.roles)[role])
                const modelPath = ['cost_profiles', profileName, 'roles', role, 'model']
                const effortPath = ['cost_profiles', profileName, 'roles', role, 'effort']
                return (
                  <li key={`${profileName}.${role}`}>
                    {`${profileName} · ${role}`}{' '}
                    <Input
                      aria-label={editKey(modelPath)}
                      value={String(shown(modelPath, assignment.model ?? ''))}
                      placeholder={i18nT('apps.specBuilder.engineOps.role_model')}
                      onChange={(e) => setEdit(modelPath, e.target.value)}
                    />{' '}
                    {/* A picker, not a text field: the write path validates
                        effort against the engine's own levels, and the levels
                        travel with the config read. */}
                    <SimpleSelect
                      aria-label={editKey(effortPath)}
                      options={catalogs?.effort_levels ?? []}
                      value={String(shown(effortPath, assignment.effort ?? ''))}
                      onChange={(value) => setEdit(effortPath, value)}
                      triggerFallback={i18nT('apps.specBuilder.engineOps.role_effort')}
                    />
                  </li>
                )
              }),
            )}
          </ul>
        )}
        <p style={{ margin: '6px 0 0' }}>{i18nT('apps.specBuilder.engineOps.roles_inherit_note')}</p>
      </section>

      {/* ── workflow commands: read-only, with the layer PER STAGE ────── */}
      <section aria-label={i18nT('apps.specBuilder.engineOps.workflow_section')}>
        <h4 style={{ margin: '0 0 4px' }}>
          {i18nT('apps.specBuilder.engineOps.workflow_section')}
        </h4>
        <p style={{ margin: '0 0 6px' }}>
          {reasonText(editorFor('workflow')?.reason_code ?? 'executes_argv')}
        </p>
        {originsQuery.isError ? (
          <p style={{ margin: 0 }}>
            <AlertTriangle className="lucide-inline" aria-hidden="true" />
            {(originsQuery.error as Error).message}
          </p>
        ) : (
          <>
            <p style={{ margin: '0 0 6px' }}>
              {originsQuery.data?.preset
                ? i18nT('apps.specBuilder.engineOps.workflow_preset', {
                    preset: originsQuery.data.preset.name,
                    origin: originLabel(originsQuery.data.preset.origin),
                  })
                : i18nT('apps.specBuilder.engineOps.workflow_no_preset')}
            </p>
            <table style={{ width: '100%', borderCollapse: 'collapse' }}>
              <thead>
                <tr>
                  <th scope="col" style={{ textAlign: 'left' }}>
                    {i18nT('apps.specBuilder.engineOps.col_stage')}
                  </th>
                  <th scope="col" style={{ textAlign: 'left' }}>
                    {i18nT('apps.specBuilder.engineOps.col_origin')}
                  </th>
                </tr>
              </thead>
              <tbody>
                {(originsQuery.data?.stages ?? []).map((row) => (
                  <tr key={row.stage}>
                    <td>{row.stage}</td>
                    {/* The engine's own line. A label derived here from
                        `preset` versus `declared_at` would be a second reading of
                        precedence, and it would call a byte-identical override
                        inherited. */}
                    <td>{row.summary}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </>
        )}
      </section>

      {/* ── the domains this surface will not write, and why ──────────── */}
      <section aria-label={i18nT('apps.specBuilder.engineOps.read_only_section')}>
        <h4 style={{ margin: '0 0 4px' }}>
          {i18nT('apps.specBuilder.engineOps.read_only_section')}
        </h4>
        <ul style={{ margin: 0, paddingLeft: 18 }}>
          {editors
            .filter((editor) => !editor.editable)
            .map((editor) => (
              <li key={editor.domain}>
                <code>{editor.path}</code>
                {': '}
                {reasonText(editor.reason_code)}
              </li>
            ))}
        </ul>
      </section>

      {/* ── save, and the engine's answer to it ───────────────────────── */}
      <div style={{ display: 'flex', flexDirection: 'column', gap: 6 }}>
        <div>
          <Btn
            label={
              <>
                <Save className="lucide-inline" aria-hidden="true" />
                {i18nT('apps.specBuilder.engineOps.save')}
              </>
            }
            ariaLabel={i18nT('apps.specBuilder.engineOps.save')}
            onClick={() => save.mutate()}
            disabled={!dirty || save.isPending}
          />
          {dirty && <span> {i18nT('apps.specBuilder.engineOps.unsaved')}</span>}
        </div>
        {refusal && (
          // The ENGINE's reason, verbatim. A generic failure would leave an
          // operator guessing which value it objected to, and the engine names
          // the path.
          <p role="alert" style={{ margin: 0 }}>
            <AlertTriangle className="lucide-inline" aria-hidden="true" />
            {i18nT('apps.specBuilder.engineOps.write_refused', { reason: refusal })}
          </p>
        )}
        {saved && !dirty && (
          <p style={{ margin: 0 }}>{i18nT('apps.specBuilder.engineOps.write_saved')}</p>
        )}
      </div>
    </div>
  )
}

/** The add-a-rule row for the autonomy ladder. Its pickers are the ENGINE's
 *  vocabularies, including the wildcard key, so it cannot offer a class or a
 *  spec type the validator refuses. */
function AutonomyAdd({
  sources,
  catalogs,
  onAdd,
}: {
  sources: string[]
  catalogs: EngineConfigResponse['catalogs']
  onAdd: (path: string[], level: string) => void
}) {
  const wildcard = catalogs?.wildcard ?? '*'
  const classes = [...(catalogs?.submitter_classes ?? []), wildcard]
  const types = [...(catalogs?.spec_types ?? []), wildcard]
  const levels = catalogs?.autonomy_levels ?? []
  const [source, setSource] = useState(sources[0] ?? '')
  const [klass, setKlass] = useState(classes[0] ?? '')
  const [specType, setSpecType] = useState(types[0] ?? '')
  const [level, setLevel] = useState(levels[0] ?? '')

  if (sources.length === 0 || levels.length === 0) return null
  return (
    <div style={{ marginTop: 6 }}>
      <SimpleSelect
        aria-label={i18nT('apps.specBuilder.engineOps.autonomy_add_source')}
        options={sources}
        value={source}
        onChange={setSource}
      />{' '}
      <SimpleSelect
        aria-label={i18nT('apps.specBuilder.engineOps.autonomy_add_class')}
        options={classes}
        value={klass}
        onChange={setKlass}
      />{' '}
      <SimpleSelect
        aria-label={i18nT('apps.specBuilder.engineOps.autonomy_add_type')}
        options={types}
        value={specType}
        onChange={setSpecType}
      />{' '}
      <SimpleSelect
        aria-label={i18nT('apps.specBuilder.engineOps.autonomy_add_level')}
        options={levels}
        value={level}
        onChange={setLevel}
      />{' '}
      <Btn
        label={i18nT('apps.specBuilder.engineOps.autonomy_add')}
        ariaLabel={i18nT('apps.specBuilder.engineOps.autonomy_add')}
        onClick={() => onAdd(['sources', source, 'autonomy', klass, specType], level)}
        disabled={!source || !klass || !specType || !level}
      />
    </div>
  )
}
