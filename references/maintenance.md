# Navigation maintenance

Read only for an explicit request to register an existing sealed checkpoint or investigate a missing Dashboard record. Follow the current project and viewer maintenance contracts. This branch does not create a fresh checkpoint or resume task execution.

## Register an existing sealed version

1. Identify the exact checkpoint and task, read the project's `HANDOFF.md` if present, and confirm the project permits this managed entry. Read [the helper's fixed-entry contract](helper.md#fixed-project-entry) for output, lock and expected-digest handling. Preserve unmanaged documents.
2. Run read-only `resume` against the recorded worktree. All sealed bindings must remain intact. Missing/corrupt evidence, a different worktree or unreadable current inputs remain errors; retain the original bytes and hashes.
3. For this explicitly requested navigation repair, use `index --register-existing` with the exact checkpoint, stable task ID and title. Supply the previously read entry digest when required. This option accepts a valid DRIFT report while retaining lock/CAS, unmanaged-entry, input-overlap and monotonic-version guards. Inspect its `workspace_status` (MATCH/DRIFT) and `registration_mode: existing-sealed` receipt.

Registration publishes a link to the original version. It does not recapture, reseal, renew old checks or transfer execution authority. Normal saves still require live MATCH; actual task changes require a new version. Do not use registration to conceal a failed save.

## Dashboard lookup

Check the fixed project entry and the viewer's explicitly registered project/artifact sources. Saving a checkpoint does not automatically enroll its location in a Dashboard. Any viewer configuration or service reload follows that viewer's maintenance contract and the user's authorization; this Skill does not expand its read scope.

Report a valid sealed checkpoint separately from successful index publication and successful viewer lookup. A read-only lookup remains a lookup, even when the document contains a resume prompt.
