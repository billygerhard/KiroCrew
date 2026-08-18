/**
 * The configuration pane: the document as the write path, the resolution beside it.
 *
 * Built to `design/mockup-b.html`'s config pane, in the same split the queue uses —
 * the document on the left where the list was, its resolved read on the right where
 * the inspector was.
 *
 * ## `config.json` is the write path, and there is only one
 *
 * The left pane edits the document and saves it through `PUT /config`, which is
 * `ConfigStore.write`: the engine merges, validates the MERGED document, and
 * persists it atomically under a lock. Every rule an operator can trip — an unknown
 * key, an out-of-range value, a setting written at a scope it is not overridable at
 * — is the engine's, reported back by path, so this panel keeps no validation of its
 * own beyond "is this JSON at all". The right pane writes NOTHING; it is a read, and
 * the only writes it offers are per-role resets that go through the same PUT.
 *
 * ## Three properties of the write, none of them cosmetic
 *
 * 1. **A save sends a patch, not the document.** The write path merges, so a key the
 *    operator DELETED has to be spelled as an explicit `null` or the merge keeps the
 *    old value and the editor shows a change that did not happen. See
 *    `configDocument.ts`.
 * 2. **An elided value is never written back.** Credential-classified values are
 *    withheld from the read and can be overwritten but never displayed, so the
 *    sentinel is dropped from every patch — otherwise a save replaces a live token
 *    with the literal string `<elided>` and nothing reports it.
 * 3. **A per-role reset names the node it clears.** The mockup's disabled buttons
 *    read `Nothing to reset` with the missing node in a tooltip, and the enabled one
 *    reads `Clear <path>`; that is not decoration. Clearing a profile's role
 *    assignment and clearing a project's own override are different edits with
 *    different blast radii, and a button labelled only `Reset` is how somebody
 *    clears the wrong one.
 *
 * ## One departure from the mockup, and why
 *
 * The mockup's roles table resets `projects.<project>.roles.<role>`. **The engine has
 * no such node.** Role assignments live only inside a cost profile
 * (`engine/config/profiles.py`: `cost_profiles.<name>.roles.<role>`), and a project
 * SELECTS a profile rather than overriding roles within it. So the reset clears the
 * profile's assignment, the label says so, and the note beside the table states that
 * the profile is shared — because clearing it changes every project that selected
 * that profile, which is exactly the fact a label reading `Reset` would hide.
 *
 * ## Layout rules this file must not break
 *
 * No drawer, no modal, no scrim: the selected design passes the "safety controls are
 * never behind navigation" criterion only because it contains no overlay, and
 * `SpecEngineShell.test.tsx` fails on a `position:fixed`/`absolute` declaration. The
 * document editor is a fixed-height region for the same reason the untrusted blocks
 * are — a document that grows with its line count would push the save controls off
 * the pane.
 */
import { Fragment, useCallback, useMemo, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'

import { i18nT } from '../../i18n/t'
import { fmtNumber } from '../../i18n/format'
import {
  QK,
  QK_RESOLVED_ROOT,
  SpecEngineApiError,
  specEngineApi,
  type ConfigAdvisory,
  type ConfigSnapshot,
  type EffectiveSetting,
  type ResolvedRole,
} from './api'
import {
  COST_PROFILES,
  documentText,
  dotted,
  isDescendant,
  isObject,
  mergePatch,
  nodeAt,
  parseDocument,
  patchAt,
  roleSegments,
  type Document,
} from './configDocument'

/** Separator between two identifiers on one line. Punctuation, not copy. */
const SEP = ' \u00b7 '

/** Stands in for a field the engine has no value for. Punctuation, not copy. */
const NONE = '\u2014'

/** The resolved read with no project named. Not a project id, so not a valid one. */
const APP_WIDE = ''

/**
 * The origin of a value in force, in words, keyed by the engine's own enum.
 *
 * Keys, not resolved strings: a module-level `i18nT()` runs once at import and would
 * freeze this table in whichever language happened to be active then.
 */
const ORIGIN_KEY: Record<EffectiveSetting['origin'], string> = {
  bundled_default: 'apps.specEngine.configPanel.origin_bundled_default',
  app_config: 'apps.specEngine.configPanel.origin_app_config',
  cost_profile: 'apps.specEngine.configPanel.origin_cost_profile',
  project_config: 'apps.specEngine.configPanel.origin_project_config',
  source_config: 'apps.specEngine.configPanel.origin_source_config',
}

/** The refusal code behind an error, or `''` when it is not one of ours. */
function codeOf(error: unknown): string {
  return error instanceof SpecEngineApiError ? error.code : ''
}

/** A refusal block: the sentence a reader acts on, with the code underneath. */
function Refused({ title, error }: { title: string; error: unknown }) {
  const code = codeOf(error)
  const text = error instanceof Error ? error.message : ''
  return (
    <div className="se-refusal" role="alert">
      {title}
      <code>{code ? `${code}${SEP}${text}` : text}</code>
    </div>
  )
}

/**
 * The advisories a write earned: valid configuration somebody should know about.
 *
 * `requires_acknowledgment` is rendered as its own mark because an advisory a human
 * must say "yes, I know" to is a different obligation from one they only read, and a
 * surface that showed both the same way teaches a reader to skip both.
 */
export function Advisories({ advisories }: { advisories: ConfigAdvisory[] }) {
  if (advisories.length === 0) return null
  return (
    <ul className="se-advisories">
      {advisories.map((advisory) => (
        <li key={`${advisory.code}:${advisory.path}`} data-ack={advisory.requires_acknowledgment}>
          {/* Code and path are engine identifiers an operator greps for, not copy. */}
          <span className="se-fc">{advisory.code}</span>
          <span className="se-fkind">{advisory.path}</span>
          {advisory.requires_acknowledgment && (
            <span className="se-flag" data-flag="ack">
              {i18nT('apps.specEngine.configPanel.acknowledgment_required')}
            </span>
          )}
          <span className="se-adv-text">{advisory.message}</span>
        </li>
      ))}
    </ul>
  )
}

/**
 * The document, edited and saved through the engine's one write path.
 *
 * The editor holds text rather than a parsed object, because half-typed JSON is a
 * legitimate state of an editor and a parsed model cannot represent it. `text ===
 * null` means "showing what the read returned", which is what makes the revert
 * exact: it drops local text and shows the document again rather than reconstructing
 * a copy of it.
 */
function DocumentEditor({ config }: { config: ConfigSnapshot }) {
  const client = useQueryClient()
  const [text, setText] = useState<string | null>(null)
  const [localError, setLocalError] = useState('')
  const [saved, setSaved] = useState<ConfigAdvisory[] | null>(null)
  const [empty, setEmpty] = useState(false)

  const baseline = config.document
  const shown = text ?? documentText(baseline)
  const dirty = text !== null && text !== documentText(baseline)

  const save = useMutation({
    mutationFn: (patch: Document) => specEngineApi.writeConfig(patch),
    onSuccess: (result) => {
      setSaved(result.advisories)
      // Dropped rather than replaced with the merged document: the read is the
      // authority on what is persisted (and on what is elided), so the editor goes
      // back to showing it.
      setText(null)
      void client.invalidateQueries({ queryKey: QK.config })
      void client.invalidateQueries({ queryKey: QK_RESOLVED_ROOT })
    },
  })

  const onSave = useCallback(() => {
    setSaved(null)
    setEmpty(false)
    const parsed = parseDocument(
      shown,
      i18nT('apps.specEngine.configPanel.a_document_must_be_a_json_object'),
    )
    if (!parsed.ok) {
      setLocalError(parsed.error)
      return
    }
    setLocalError('')
    const patch = mergePatch(baseline, parsed.document, config.elided_marker)
    if (Object.keys(patch).length === 0) {
      // Every write is recorded, so sending an empty patch would put a line in the
      // durable write record for a change nobody made.
      setEmpty(true)
      return
    }
    save.mutate(patch)
  }, [baseline, config.elided_marker, save, shown])

  return (
    <>
      <textarea
        aria-label={i18nT('apps.specEngine.configPanel.the_configuration_document')}
        className="se-json se-m"
        spellCheck={false}
        value={shown}
        onChange={(event) => {
          setText(event.target.value)
          setLocalError('')
          setEmpty(false)
          setSaved(null)
        }}
      />
      <div className="se-acts" style={{ marginTop: 11 }}>
        <button
          type="button"
          className="se-btn"
          disabled={save.isPending}
          onClick={onSave}
        >
          {save.isPending
            ? i18nT('apps.specEngine.configPanel.saving')
            : i18nT('apps.specEngine.configPanel.validate_and_save')}
        </button>
        <button
          type="button"
          className="se-btn"
          disabled={!dirty || save.isPending}
          onClick={() => {
            setText(null)
            setLocalError('')
            setEmpty(false)
          }}
        >
          {i18nT('apps.specEngine.configPanel.revert_unsaved_edits')}
        </button>
        {dirty && (
          <span className="se-lbl">{i18nT('apps.specEngine.configPanel.unsaved_edits')}</span>
        )}
      </div>

      {localError !== '' && (
        <div className="se-refusal" role="alert">
          {i18nT('apps.specEngine.configPanel.the_document_is_not_valid_json')}
          <code>{localError}</code>
        </div>
      )}
      {empty && (
        <p className="se-note">{i18nT('apps.specEngine.configPanel.nothing_to_save')}</p>
      )}
      {save.isError && (
        <Refused
          title={i18nT('apps.specEngine.configPanel.could_not_save_the_configuration')}
          error={save.error}
        />
      )}
      {saved !== null && (
        <div className="se-torn">
          {i18nT('apps.specEngine.configPanel.saved_the_document')}
          <Advisories advisories={saved} />
        </div>
      )}

      {config.errors.length > 0 && (
        <div className="se-blk">
          <h3>{i18nT('apps.specEngine.configPanel.problems_in_the_persisted_document')}</h3>
          <ul className="se-findings">
            {config.errors.map((error) => (
              <li key={`${error.path}:${error.message}`}>
                <span className="se-fkind">{error.path || NONE}</span>
                {error.message}
              </li>
            ))}
          </ul>
        </div>
      )}
      {config.advisories.length > 0 && (
        <div className="se-blk">
          <h3>{i18nT('apps.specEngine.configPanel.advisories')}</h3>
          <Advisories advisories={config.advisories} />
        </div>
      )}

      <p className="se-note">
        {i18nT('apps.specEngine.specEnginePage.secret_values_are_withheld_from_this_read')}
      </p>
      {config.elided.length > 0 && (
        <p className="se-note">
          {i18nT('apps.specEngine.configPanel.withheld_at')}
          <span className="se-m">{SEP}{config.elided.join(SEP)}</span>
        </p>
      )}
      {/* Both properties of the write, stated where the operator is about to make
          one. The second is the one nobody would guess: a merge keeps what a patch
          omits, so a deletion has to be sent as a deletion. */}
      <p className="se-note">
        {i18nT('apps.specEngine.configPanel.elided_values_are_never_written_back')}
      </p>
      <p className="se-note">
        {i18nT('apps.specEngine.configPanel.deletions_are_sent_as_explicit_nulls')}
      </p>
    </>
  )
}

/** The value in force for one setting, rendered as JSON so a type is visible. */
function settingValue(value: unknown): string {
  return typeof value === 'string' ? value : JSON.stringify(value)
}

/**
 * One role's row: its resolution, where it was decided, and the reset for it.
 *
 * Model and effort are separate columns rather than one joined string, per the
 * losing mockup's shape which the reviewer preferred and correction 6 carried over:
 * they are two independent decisions and a reader scans down one of them.
 */
function RoleRow({
  role,
  profileInForce,
  document,
  onReset,
  onSelect,
  selected,
  resetting,
}: {
  role: ResolvedRole
  profileInForce: string
  document: Document
  onReset: (segments: string[]) => void
  onSelect: () => void
  selected: boolean
  resetting: boolean
}) {
  const profile = role.profile ?? ''
  // Segments, from the read's own fields. The declaring path is displayed but never
  // parsed: a profile named `thrifty.roles` has no recoverable split.
  const segments = profile ? roleSegments(profile, role.role) : []
  // And the node must lie inside the profile actually in force, compared SEGMENT for
  // segment. String matching would read `cost_profiles.thrifty.roles.roles.review`
  // as a path inside a profile named `thrifty`, so a reset offered on that row would
  // clear the role assignment of a DIFFERENT profile — one that some other project
  // selected. As segments, `thrifty.roles` and `thrifty` are two sibling names and
  // neither contains the other.
  const inForce = segments.length > 0 && isDescendant(segments, [COST_PROFILES, profileInForce])
  const present = inForce && nodeAt(document, segments) !== undefined
  const path = dotted(segments)
  return (
    <tr aria-selected={selected}>
      <td className="se-r">
        {/* The role name selects it for the match trace below. A button rather than a
            row click, so the traversal is keyboard-reachable without inventing a
            grid pattern for a five-row table. */}
        <button type="button" className="se-rolebtn" aria-pressed={selected} onClick={onSelect}>
          {role.role}
        </button>
      </td>
      <td className="se-m">
        {role.model || i18nT('apps.specEngine.configPanel.inherited_from_the_provider')}
      </td>
      <td>
        {role.effort || NONE}
        {role.dropped_effort && (
          <span className="se-flag" data-flag="dropped">
            {i18nT('apps.specEngine.configPanel.effort_dropped')}
          </span>
        )}
      </td>
      <td className="se-src">
        {role.declared_at ? (
          <b className="se-m">{role.declared_at}</b>
        ) : (
          i18nT('apps.specEngine.configPanel.session_default')
        )}
      </td>
      <td>
        {present ? (
          <button
            type="button"
            className="se-btn se-sm se-danger"
            disabled={resetting}
            onClick={() => onReset(segments)}
          >
            {/* Named with the node it clears, so nobody clears a profile believing
                they cleared something narrower. */}
            {i18nT('apps.specEngine.configPanel.clear_node', { path })}
          </button>
        ) : (
          <button
            type="button"
            className="se-btn se-sm"
            disabled
            title={
              path
                ? i18nT('apps.specEngine.configPanel.no_node_exists_at', { path })
                : i18nT('apps.specEngine.configPanel.no_profile_declares_this_role')
            }
          >
            {i18nT('apps.specEngine.configPanel.nothing_to_reset')}
          </button>
        )}
      </td>
    </tr>
  )
}

/**
 * How one role's assignment was matched, segment by segment.
 *
 * The mockup showed a precedence trace for `roles.implement.effort`. Roles are not
 * registry settings, so the layers here are the ones that actually exist: the
 * profile's assignment for this role, and the session default under it. The point
 * the block makes is the one the mockup made — the match is SEGMENT-wise, so a
 * profile whose name contains a dot is one segment and not two — and it names the
 * fallback the engine reported rather than a rule restated here.
 */
function MatchTrace({ role, document }: { role: ResolvedRole; document: Document }) {
  const profile = role.profile ?? ''
  const segments = profile ? roleSegments(profile, role.role) : []
  const declared = segments.length > 0 && nodeAt(document, segments) !== undefined
  return (
    <div className="se-blk">
      <h3>
        {i18nT('apps.specEngine.configPanel.segment_wise_match')}
        {SEP}
        <span className="se-m">{role.role}</span>
      </h3>
      <div className="se-seg">
        {segments.length > 0 && (
          <div>
            <span className={declared ? 'se-hit' : 'se-miss'}>{dotted(segments)}</span>
            {SEP}
            <span className="se-m">
              {declared
                ? settingValue(nodeAt(document, segments))
                : i18nT('apps.specEngine.configPanel.unset')}
            </span>
          </div>
        )}
        <div>
          <span className={role.source === 'session_default' ? 'se-hit' : 'se-miss'}>
            {i18nT('apps.specEngine.configPanel.session_default')}
          </span>
          {SEP}
          <span className="se-m">
            {role.model || i18nT('apps.specEngine.configPanel.inherited_from_the_provider')}
          </span>
        </div>
        {/* The engine's own sentence about the fallback, not a paraphrase: the four
            fallback conditions are fixed in four different places, and the report is
            what says which one this is. */}
        {role.report && <div className="se-seg-note">{role.report}</div>}
        <div className="se-seg-note">
          {i18nT('apps.specEngine.configPanel.most_specific_segment_wins')}
        </div>
      </div>
    </div>
  )
}

/**
 * The resolved read: what is in force, and where each value was decided.
 *
 * Keyed by project, because most of the precedence only exists once a project is
 * named — without one, a project-scoped value and the profile a project selected are
 * both invisible, and a table that showed the app-wide resolution as "the" answer
 * would be wrong for every project.
 */
function ResolvedPane({ config }: { config: ConfigSnapshot }) {
  const client = useQueryClient()
  const [project, setProject] = useState<string>(APP_WIDE)
  const [selectedRole, setSelectedRole] = useState<string>('')

  const projects = useMemo(() => {
    const node = config.document.projects
    return isObject(node) ? Object.keys(node).sort() : []
  }, [config.document])

  const resolved = useQuery({
    queryKey: QK.resolved(project),
    queryFn: () => specEngineApi.resolvedConfig(project || undefined),
    retry: false,
  })

  const reset = useMutation({
    // A reset is an ordinary configuration write: `null` at the node, through the
    // same single write path a save uses, so the engine validates the document a
    // clear produces exactly as it validates any other.
    mutationFn: (segments: string[]) => specEngineApi.writeConfig(patchAt(segments, null)),
    onSuccess: () => {
      void client.invalidateQueries({ queryKey: QK.config })
      void client.invalidateQueries({ queryKey: QK_RESOLVED_ROOT })
    },
  })

  const order = resolved.data?.role_order ?? []
  const roles = resolved.data?.roles
  const configured = (resolved.data?.settings ?? []).filter((value) => !value.is_default)
  const defaults = (resolved.data?.settings ?? []).length - configured.length
  const shownRole = roles?.roles[selectedRole || order[0] || '']

  return (
    <>
      <div className="se-insp-head">
        <span className="se-insp-title">
          {project
            ? i18nT('apps.specEngine.configPanel.resolved_for_project', { project })
            : i18nT('apps.specEngine.configPanel.resolved_app_wide')}
        </span>
        <span className="se-insp-sub">
          {i18nT('apps.specEngine.configPanel.a_read_never_a_second_write_path')}
        </span>
      </div>
      <div className="se-insp-body">
        {/* Chips rather than a dropdown: the same control the queue's filters use, and
            a dropdown here would be a popup drawn over the page — the one thing this
            design's safety criterion rests on not existing. */}
        <div className="se-filters" role="group" aria-label={i18nT('apps.specEngine.configPanel.resolve_for')}>
          <button
            type="button"
            className="se-filter"
            aria-pressed={project === APP_WIDE}
            onClick={() => setProject(APP_WIDE)}
          >
            {i18nT('apps.specEngine.configPanel.no_project_app_wide')}
          </button>
          {projects.map((name) => (
            <button
              key={name}
              type="button"
              className="se-filter se-m"
              aria-pressed={project === name}
              onClick={() => setProject(name)}
            >
              {name}
            </button>
          ))}
          {projects.length === 0 && (
            <span className="se-note">
              {i18nT('apps.specEngine.configPanel.no_project_is_configured_yet')}
            </span>
          )}
        </div>

        {resolved.isError ? (
          <Refused
            title={i18nT('apps.specEngine.configPanel.could_not_resolve_the_configuration')}
            error={resolved.error}
          />
        ) : resolved.isPending ? (
          <p className="se-note">{i18nT('apps.specEngine.configPanel.resolving')}</p>
        ) : (
          <>
            <div className="se-blk">
              <h3>{i18nT('apps.specEngine.configPanel.per_role_model_and_effort')}</h3>
              {roles && roles.profile === '' && (
                <p className="se-note">
                  {roles.requested_profile
                    ? i18nT('apps.specEngine.configPanel.the_selected_profile_is_not_defined', {
                        profile: roles.requested_profile,
                      })
                    : i18nT('apps.specEngine.configPanel.no_cost_profile_is_selected')}
                </p>
              )}
              <table className="se-roles">
                <thead>
                  <tr>
                    <th>{i18nT('apps.specEngine.configPanel.col_role')}</th>
                    <th>{i18nT('apps.specEngine.configPanel.col_model')}</th>
                    <th>{i18nT('apps.specEngine.configPanel.col_effort')}</th>
                    <th>{i18nT('apps.specEngine.configPanel.col_from')}</th>
                    <th>{i18nT('apps.specEngine.configPanel.col_reset')}</th>
                  </tr>
                </thead>
                <tbody>
                  {order.map((name) => {
                    const role = roles?.roles[name]
                    if (!role) return null
                    return (
                      <RoleRow
                        key={name}
                        role={role}
                        profileInForce={roles?.profile ?? ''}
                        document={config.document}
                        resetting={reset.isPending}
                        selected={name === (selectedRole || order[0])}
                        onSelect={() => setSelectedRole(name)}
                        onReset={(segments) => {
                          setSelectedRole(name)
                          reset.mutate(segments)
                        }}
                      />
                    )
                  })}
                </tbody>
              </table>
              {/* The departure from the mockup, and the fact a bare `Reset` label
                  would hide: the node is the PROFILE's, and a profile is shared by
                  every project that selected it. */}
              <p className="se-note">
                {i18nT('apps.specEngine.configPanel.a_role_lives_on_the_shared_profile')}
              </p>
              {reset.isError && (
                <Refused
                  title={i18nT('apps.specEngine.configPanel.could_not_clear_the_role')}
                  error={reset.error}
                />
              )}
            </div>

            {shownRole && <MatchTrace role={shownRole} document={config.document} />}

            <div className="se-blk">
              <h3>{i18nT('apps.specEngine.configPanel.values_not_at_their_default')}</h3>
              {configured.length === 0 ? (
                <p className="se-note">
                  {i18nT('apps.specEngine.configPanel.every_setting_is_at_its_bundled_default')}
                </p>
              ) : (
                <dl className="se-kv">
                  {configured.map((value) => (
                    <Fragment key={value.key}>
                      <dt className="se-m">{value.key}</dt>
                      <dd>
                        {settingValue(value.value)}
                        <span className="se-note">
                          {SEP}
                          {i18nT(ORIGIN_KEY[value.origin])}
                          {value.declared_at ? `${SEP}${value.declared_at}` : ''}
                        </span>
                      </dd>
                    </Fragment>
                  ))}
                </dl>
              )}
              <p className="se-note">
                {i18nT('apps.specEngine.configPanel.settings_at_their_bundled_default')}
                {SEP}
                <span className="se-m">{fmtNumber(Math.max(defaults, 0))}</span>
              </p>
            </div>
          </>
        )}
      </div>
    </>
  )
}

/**
 * The whole configuration pane: the document, and the resolution beside it.
 *
 * Takes the config read rather than performing its own, so the page's first-run
 * detection and this pane cannot disagree about whether a document exists — and a
 * read that FAILED is rendered here as the refusal it is, because `config_unreadable`
 * means a document exists and cannot be parsed, which is a repair and not an empty
 * form to fill in.
 */
export function ConfigPane({
  config,
  error,
  pending,
}: {
  config: ConfigSnapshot | undefined
  error: unknown
  pending: boolean
}) {
  return (
    <>
      <section className="se-cfg">
        <div className="se-cfg-head">
          {/* A filename, not copy: translating it would name a file that does not exist. */}
          <h1 className="se-m">config.json</h1>
          <span className="se-sort">
            {i18nT('apps.specEngine.specEnginePage.the_write_path_validated_on_save')}
          </span>
        </div>
        <div className="se-cfg-body">
          {error ? (
            <Refused
              title={i18nT('apps.specEngine.specEnginePage.could_not_read_the_configuration')}
              error={error}
            />
          ) : pending || !config ? (
            <p className="se-note">
              {i18nT('apps.specEngine.specEnginePage.reading_the_configuration')}
            </p>
          ) : (
            <DocumentEditor config={config} />
          )}
        </div>
      </section>
      <section
        className="se-inspector"
        aria-label={i18nT('apps.specEngine.specEnginePage.resolved_configuration')}
      >
        {error || pending || !config ? (
          <div className="se-insp-body">
            <p className="se-note">
              {i18nT('apps.specEngine.configPanel.the_resolved_read_needs_the_document_first')}
            </p>
          </div>
        ) : (
          <ResolvedPane config={config} />
        )}
      </section>
    </>
  )
}