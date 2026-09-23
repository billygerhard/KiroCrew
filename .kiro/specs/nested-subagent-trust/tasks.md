# Implementation Plan

- [ ] 1. Write bug condition exploration test
  - **Property 1: Bug Condition** — Root trust inherits to any depth
  - **CRITICAL**: This test MUST FAIL on unfixed code — failure confirms the bug exists
  - **DO NOT attempt to fix the test or the code when it fails**
  - **GOAL**: Surface counterexamples that demonstrate the bug exists
  - File `test/test_subagent_nested_trust.py`, class `TestNestedTrustInheritance`: build `SubagentManager` with `_mock_sessions()` whose `get_approval_policy` returns `"auto"` only for `"dashboard:chat-1"`, insert a live `SubagentInfo(id="A", parent_session_key="dashboard:chat-1")` into `manager._agents`, then `manager.spawn(task, parent_session_key="subagent:A")` with an `AsyncMock` `on_spawn_approval`; assert the callback is not awaited and `info.error == ""`
  - Same class: a three-link chain `dashboard:chat-1 <- A <- B <- C`; assert `manager.resolve_approval_policy("subagent:C") == "auto"`; and a cycle `A -> B -> A` returns without raising
  - Run the tests on UNFIXED code
  - **EXPECTED OUTCOME**: Test FAILS (correct — it proves the bug exists)
  - Document the counterexamples observed in the task completion report
  - _Requirements: 2.1, 2.2, 2.4_

- [ ] 2. Write preservation tests (BEFORE implementing the fix)
  - **Property 2: Preservation** — Non-subagent parents and untrusted roots are unchanged
  - **IMPORTANT**: Follow observation-first methodology
  - **Observe on UNFIXED code first**: run the adjacent cases and record what the code does today
  - **Then encode the observed behavior as tests** — one bullet per test, with its assertions
  - Depth-1 spawn under trusted `dashboard:chat-1` auto-approves (callback not awaited), mirroring `TestParentTrustedSpawnApproval`
  - Untrusted root (`get_approval_policy` returns `""` everywhere) with a depth-2 spawn awaits the approval callback exactly once
  - Non-subagent parent key: `get_approval_policy` is called with the literal key and no `_agents` lookup occurs
  - These tests MUST PASS on unfixed code; a preservation test that fails today is describing the defect, not the baseline — move it to task 1 or drop it
  - Run the tests on UNFIXED code
  - **EXPECTED OUTCOME**: Tests PASS (confirms the baseline behavior to preserve)
  - _Requirements: 3.1, 3.2, 3.4_

- [ ] 3. Fix for nested subagent trust inheritance
  - [ ] 3.1 Implement the fix
    - `src/kiro_crew/subagent.py`: add `SubagentManager.root_session_key(key)` (walk `subagent:<id>` parents via `self._agents`, stop on non-subagent key / unknown id / empty parent / cycle) and `resolve_approval_policy(key)`
    - `src/kiro_crew/subagent_manager/admission/gate.py`: `parent_trusted` reads `self._manager.resolve_approval_policy(parent_session_key)`
    - `src/kiro_crew/subagent_manager/run.py`: `parent_policy = self._manager.resolve_approval_policy(info.parent_session_key)`; leave the `has_session(info.parent_session_key)` probe untouched
    - `src/kiro_crew/slack/gateway.py`: in `_interactive_approval` resolve `parent_slot` from the root key for `subagent:` parents; make `_spawn_slot_resolver` use the root key as well
    - _Bug_Condition: parent_session_key startswith "subagent:" AND root chat approval_policy == "auto"_
    - _Expected_Behavior: spawn auto-approved as parent_trusted; tool prompts resolve parent_policy="auto"; interactive prompts land in the root chat's slot_
    - _Preservation: non-subagent keys read the store directly; untrusted roots stay interactive; other grant sources keep their precedence_
    - _Requirements: 2.1, 2.2, 2.3, 2.4, 2.5, 2.6, 3.1, 3.2, 3.3, 3.4, 3.5_
  - [ ] 3.2 Verify bug condition exploration test now passes
    - **IMPORTANT**: Re-run the SAME test from task 1 — do NOT write a new test
    - **EXPECTED OUTCOME**: Test PASSES (confirms the bug is fixed)
    - _Requirements: 2.1, 2.2, 2.4_
  - [ ] 3.3 Verify preservation tests still pass
    - **IMPORTANT**: Re-run the SAME tests from task 2 — do NOT write new tests
    - **EXPECTED OUTCOME**: Tests PASS (confirms no regressions)
    - _Requirements: 3.1, 3.2, 3.4_

- [ ] 4. Checkpoint — ensure the whole affected suite passes
  - Run `python3 scripts/local-gate.py` plus `test/test_subagent.py`, `test/test_admission_gate.py`, `test/test_approval_modes_enforcement.py` with bounded workers; no regressions anywhere
