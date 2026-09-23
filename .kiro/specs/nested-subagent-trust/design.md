# Design Document

## Overview

Per-chat trust is stored as `approval_policy="auto"` on the chat's session key
and is consulted at three sites: the spawn admission gate, the subagent run
loop's tool-permission branch, and the gateway's interactive approver. All three
key their lookup on the IMMEDIATE `parent_session_key`. A subagent's own key is
`subagent:<id>`; on the default shared-runtime path that key is never registered
in the session store, so a grandchild's lookup returns `""` and trust stops at
depth 1. The fix adds one resolver on `SubagentManager` that walks
`subagent:<id>` parent links up to the first non-subagent key (the root chat)
and uses it at all three sites, so trust inherits to any depth.

## Glossary

- **Bug_Condition**: a spawn or tool-permission request whose
  `parent_session_key` starts with `subagent:` while the root chat session
  reached by following `SubagentInfo.parent_session_key` links has
  `approval_policy="auto"`.
- **Property**: after the fix, every such request resolves the root's policy.
- **Preservation**: requests whose `parent_session_key` is not a `subagent:` key
  behave byte-identically to today.
- **root_session_key(key)**: new `SubagentManager` method. While `key` starts
  with `subagent:` and `self._agents[<id>]` exists and has a non-empty
  `parent_session_key`, replace `key` with that parent; stop on a non-subagent
  key, an unknown id, an empty parent, or a revisited key. Returns the final key.
- **resolve_approval_policy(key)**: new `SubagentManager` method returning
  `self._sessions.get_approval_policy(self.root_session_key(key))`.
- **gate.py `parent_trusted`**: `subagent_manager/admission/gate.py` ~line 1057.
- **run.py `parent_policy`**: `subagent_manager/run.py` ~line 969.
- **`_interactive_approval`**: `slack/gateway.py` ~line 2413; `parent_slot =
  dashboard_slot_key(parent_session_key)` and `_spawn_slot_resolver`.

## Bug Details

### Bug Condition

```
FUNCTION isBugCondition(request)
  key := request.parent_session_key
  IF NOT key.startswith("subagent:") THEN RETURN false
  root := follow parent_session_key links from key to first non-subagent key
  RETURN sessions.get_approval_policy(root) == "auto"
END FUNCTION
```

### Examples

- Chat `dashboard:chat-1` trusted. Subagent `A` (parent `dashboard:chat-1`)
  spawns `B` (parent `subagent:A`). Today: `B`'s spawn is routed to the
  interactive approver (`parent_trusted` false). Expected: auto-approved with
  reason `parent_trusted`.
- Same tree; `B` runs a shell tool and emits `session/request_permission`.
  Today: `parent_policy=""`, prompt goes to the interactive approver, which
  resolves slot `""`, so the prompt reaches only the global feed and times out.
  Expected: `parent_policy="auto"`, approved via `parent_policy_auto`.
- Depth 3: `C` (parent `subagent:B`). Today and expected as above; the walk is
  unbounded in depth.
- Chat NOT trusted, same tree. Today: interactive. Expected: interactive
  (unchanged), but the prompt now resolves to `chat-1`'s slot.

## Expected Behavior

### Preservation Requirements

- Depth-1 spawns/tool calls under a trusted chat: unchanged (the walk is a
  no-op for a non-subagent key, so the same store read happens).
- Untrusted root: no grant is invented; every existing fallback
  (explicit `approval_mode`, YOLO, global `approval_mode`, hooks) keeps its
  order.
- The `agent.approval_mode="auto"` fallback's `has_session(parent_session_key)`
  liveness probe keeps reading the literal parent key.
- **Scope**: the fix touches only how the parent key is turned into a policy
  lookup key (and a slot). It does not register shared-session children in the
  session store, does not change `set_approval_policy`, and does not alter the
  low-fidelity child fidelity gate.

## Hypothesized Root Cause

1. **Confirmed by reading code.** `gate.py:1057` computes `parent_trusted =
   parent_session_key and get_approval_policy(parent_session_key) == "auto"`.
   For a nested spawn `parent_session_key == "subagent:<id>"`.
2. **Confirmed.** `run.py:2988` (`_create_shared_session_impl`) creates the
   child on the parent's `AcpRuntime` via `runtime.create_session` +
   `_bind_shared_handle` and never calls `SessionManager.get_or_create`, so no
   `approval_policy` exists under `subagent:<id>`
   (`session_allocation.py:1140` returns `""` for an unknown key). Only the
   dedicated-process fallback (`run.py:1205/1217`) stores `approval_policy`.
3. **Confirmed.** `run.py:969` reads `get_approval_policy(info.parent_session_key)`
   for the child's tool prompts, so the grandchild's `parent_policy` is `""`.
4. **Confirmed.** `gateway.py:2413 _interactive_approval` calls
   `dashboard_slot_key(parent_session_key)`, which returns `""` for a
   `subagent:` key; `_spawn_slot_resolver` maps `spawn:<id>` to
   `_event_slot(info.parent_session_key)`, also `""` for a subagent parent. The
   slot-trust shortcut is therefore skipped and the prompt has no tab.

## Correctness Properties

### Property 1: Root trust inherits to any depth

For any spawn or tool-permission request whose parent chain ends at a chat with
`approval_policy="auto"`, the spawn gate auto-approves with reason
`parent_trusted` and the run loop resolves `parent_policy="auto"`, regardless of
chain length.

**Validates: Requirements 2.1, 2.2, 2.3, 2.4, 2.5, 2.6**

### Property 2: Non-subagent parents and untrusted roots are unchanged

For any request whose `parent_session_key` is not a `subagent:` key, the policy
read is the same single store lookup as before; for any chain whose root is
untrusted, no branch approves that did not approve before.

**Validates: Requirements 3.1, 3.2, 3.3, 3.4, 3.5**

## Fix Implementation

### Changes Required

- `src/kiro_crew/subagent.py` (`SubagentManager`): add
  `root_session_key(key) -> str` (walk with a `seen` set; stop at non-subagent
  key, unknown id, empty parent, or cycle), `conversation_root_for_new_run`
  (a run founds its conversation with its own root; a continuation inherits the
  founding root when equal, else marks the conversation contested with the key
  itself), and `root_session_key_for(info)`. `SubagentInfo` gains
  `root_session_key` and `conversation_root_session_key`, both stamped at
  admission and carried through the spawn queue.
- `src/kiro_crew/dashboard/handlers/files.py` (`_subagent_parent_session_key`):
  answer with the run's stamped root so a nested run's file card lands in the
  root chat's tab instead of being suppressed.
- `src/kiro_crew/subagent_manager/admission/gate.py` (`parent_trusted`): read
  `self._manager.resolve_approval_policy(parent_session_key)` instead of the
  direct store read. No change to the branch order below it.
- `src/kiro_crew/subagent_manager/run.py` (`parent_policy`, ~969): same
  substitution. **No change** to the `has_session(info.parent_session_key)`
  liveness probe in the global-config fallback, nor to the
  `child_unconditional_grant_eligible` gate.
- `src/kiro_crew/slack/gateway.py` (`_interactive_approval` and
  `_spawn_slot_resolver`): resolve the slot from
  `self.subagent_mgr.root_session_key(parent_session_key)` when the key is a
  `subagent:` key and a manager exists; otherwise the existing
  `dashboard_slot_key(parent_session_key)`. No change to the fidelity,
  auto_approve_sources, `--approval`, YOLO or slot-trust branch logic.
- No changes to `SessionManager`/`session_allocation.py`, to
  `_create_shared_session_impl`, or to `set_approval_policy`.

## Testing Strategy

Framework: pytest (`test/` conftest, asyncio tests as in
`test/test_subagent.py::TestParentTrustedSpawnApproval`).

Phase 1 (unfixed code): a spawn-gate test builds a `SubagentManager` with a
mocked session store whose `get_approval_policy` returns `"auto"` only for
`dashboard:chat-1`, registers a live `SubagentInfo("A", parent="dashboard:chat-1")`
in `manager._agents`, then spawns with `parent_session_key="subagent:A"` and
asserts the approval callback is NOT awaited. A second test asserts
`resolve_approval_policy("subagent:C")` returns `"auto"` through a three-link
chain and terminates on a cycle. Both must FAIL before the fix.

Phase 2: preservation tests recorded on unfixed code — depth-1 trusted spawn
auto-approves; untrusted root at depth 2 still awaits approval; non-subagent key
reads the store once. Then re-run Phase 1 tests (pass) and Phase 2 tests (pass),
plus the related suite via `python3 scripts/local-gate.py`.
