# Task checkpoint handoff

Keep the goal, current state, evidence/gaps and next action. Omit inapplicable fields or sections rather than filling a checklist. An unknown relevant fact is a gap, not an inapplicable field. For execution ownership or external actions, a confirmed absence needs only one line; unresolved status needs its evidence and coordination step.

Task ID:
Readable task title:
Updated at with timezone:
Intent: checkpoint | handoff
Task status:
Source session or executor:
Recipient role: implementer
Repository and worktree:
Branch and HEAD:
Machine evidence: snapshot path | UNKNOWN with reason
Planned fixed project entry: HANDOFF.md path | unavailable (actual publication is a separate receipt)
Existing workflow identity: Record/Segment/Attempt/issue/plan refs if applicable

## Goal and authority

Current goal and acceptance criteria:
Current user direction and authoritative Spec/Plan/ADR references:
Authorized scope and source; restrictions and pending approvals:
Explicit deferrals and reopening conditions:

## Current work

| Item | State | Evidence and remaining gap |
|---|---|---|
| Relevant work item | implemented / implemented + verified / WIP / not done / unknown | File, command receipt or source reference |

Uncommitted changes and unrelated changes to preserve:
Exact interruption point:

## Active decisions

Decision, rationale and current authority reference:
Superseded approach, replacement and superseding evidence, only if still relevant:

## Verification

For each relevant check: actual command, result, time, tested candidate/snapshot, receipt/log reference and current applicability. Distinguish a historical PASS from current evidence; absent checks remain not run or UNKNOWN.

## Execution ownership and external actions

Original writer status and observation time/source:
Active command/Agent/job identities, scope, status and evidence:
External actions: completed / pending / outcome unknown, with receipts:
Required coordination before another writer continues:
Relevant environment assumptions to recheck:

## Open issues and next action

Known issue or open question, evidence and impact:
First action, exact target and completion condition:
Then, only necessary remaining steps:

## Read first

Project rules, current specification, relevant implementation and evidence paths with their purpose. Use repo-relative paths for project files and explicit paths/URLs for external sources. Do not copy whole documents.

Suggested skills, only when useful: name, actual entrypoint path/URL and the next step it supports. The receiving agent resolves the current source and follows its invocation policy and existing authorization; this list alone does not authorize execution. Omit when no additional skill is needed.

Resume prompt: Use task-checkpoint-handoff to resume this task from <absolute checkpoint.json path>, with repository <absolute worktree path>. Read current project rules and handoff first; reconcile current state and execution ownership before continuing within the existing authorized scope.
