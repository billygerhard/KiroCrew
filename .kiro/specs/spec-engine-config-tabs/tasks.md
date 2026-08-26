# Implementation Plan: Configuration pane section tabs and grouped settings

## Overview

Restructure the Configuration pane's editing surfaces into four tabs with
per-tab pending badges and staged-state survival, group the settings rows by
registry group with a jump navigation, then run the closing gate sweep.
Three serial waves — every task touches ConfigPanel.tsx, so nothing runs in
parallel.

## Notes

- Every surface keeps its existing write machinery untouched: staging,
  review cards, refusal retention, and invalidation are NOT changed by this
  spec — only visibility structure is.
- Contract facts from the forms-first build (binding): all three forms
  consume the shared `useStagedEdits` hook and own their `onSuccess`
  invalidation; the JSON `draft` is lifted into ConfigPane deliberately;
  `SourceForm.onOpenJson` and `onShowGrid` already exist as props; the
  registry read is `GET {PREFIX}/config/registry` with `staleTime:
  Infinity`; `settingSegments` splits a registry key at the FIRST dot only.
- Surface invariants (standing): no overlay/modal/drawer; no
  `position:absolute/fixed` in the app stylesheet (the jump nav is in-flow,
  not sticky); no native `<select>`; new strings in all 13 catalogs with
  en-XA regenerated (order-preserving catalog edits, never `sort_keys`);
  zh-CN no ASCII punctuation between CJK; ko both-form 조사.
- Suite-stub discipline: this spec adds NO new reads, so no fetch-stub
  changes should be needed; if one becomes necessary, answer it in EVERY
  SpecEngine suite's stub before the generic `/config` prefix branch.
- Known-inherited failures (excusable with proof): full vitest exit 1 on
  hiStyle formal-आप 125>119 and the flaky App.test.tsx credits pill;
  i18n-check source-strings fails only on `pages.artifactDeployPage.domain`.

## Tasks

- [ ] 1. Section tabs
  - [ ] 1.1 Restructure the pane's editing surfaces into four tabs
    - `SECTION_TABS` fixed table (settings, profiles, sources, json) with
      `activeTab` state in ConfigPane replacing `jsonOpen`; projects table
      and shared selection stay above the tabs; resolved inspector column
      untouched.
    - All four panels stay mounted; inactive panels carry `hidden`. Staged
      edits, the JSON draft, armed removal confirmations, and typed add
      text survive switching — pinned by a named test and Property 1 over
      generated switch/stage sequences.
    - Pending badges: each form gains `onPendingCount`, ConfigPane keeps a
      per-tab count; the JSON tab carries the dirty flag plus the stored
      problems/advisories counts the toggle shows today.
    - `SourceForm.onOpenJson` activates the JSON tab; the autonomy grid
      renders inside the Watch sources panel so `onShowGrid` never crosses
      tabs.
    - Tablist semantics: `role="tablist"`/`role="tab"`/`aria-selected`,
      panels labelled by tabs, ArrowLeft/ArrowRight movement; `se-filter`
      visual idiom, no overlay or absolute/fixed positioning.
    - Refusal, reading, and first-run states render without tabs, unchanged.
    - Vitest: tab rendering and semantics, badge counts per tab, JSON
      routing from the source form, staged-state survival, unchanged
      refusal states; tab labels and any badge copy in all 13 catalogs +
      en-XA.
    - _Requirements: 1.1, 1.2, 1.3, 1.4, 1.5, 1.6, 1.7_
- [ ] 2. Grouped settings
  - [ ] 2.1 Group the settings rows by registry group with a jump navigation
    - Pure exported `settingGroups(fields)` partitioning by the key's first
      dot-segment, first-appearance order, total over the input — Property
      2 fast-check over generated vocabularies.
    - Subsection headings: authored `GROUP_LABEL_KEY` labels for the
      shipped groups (all 13 catalogs + en-XA) with the raw segment as the
      detail line; unmapped groups render their raw segment, never dropped.
    - In-flow jump navigation above the rows (`se-filter` buttons, one per
      subsection, `scrollIntoView` on activation) rendered only when more
      than one subsection exists; no sticky or floating positioning.
    - Write machinery untouched: existing SettingsForm named tests keep
      passing with at most structural selector updates; scope offering,
      staging, reconciliation, review card, and refusal retention behavior
      unchanged.
    - Vitest: grouped rendering for the shipped vocabulary, authored and
      fallback labels, jump-nav presence/absence and scroll behavior,
      single-group vocabulary renders no jump nav.
    - _Requirements: 2.1, 2.2, 2.3, 2.4, 2.5_
- [ ] 3. Gate sweep
  - [ ] 3.1 Verification sweep and dispositions
    - Run all gates with real unpiped exit codes: spec_engine pytest,
      tsc, eslint, full SpecEngine vitest, full vitest (inherited-failure
      proofs where red), i18n:check, key-refs, manifest-sync, build.
    - Confirm both correctness properties have executed mutation probes
      recorded (plant, named test fails, restore byte-identical,
      SHA-verified).
    - Disposition every carried finding from tasks 1.1–2.1 in this file;
      verify catalog completeness for every new string; append a dated
      verification record section.
    - _Requirements: 1.2, 2.1, 2.5_

## Task Dependency Graph

```json
{"waves": [{"id": 0, "tasks": ["1.1"]}, {"id": 1, "tasks": ["2.1"]}, {"id": 2, "tasks": ["3.1"]}]}
```
