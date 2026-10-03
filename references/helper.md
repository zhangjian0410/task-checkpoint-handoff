# Scoped Git evidence helper

Use `scripts/checkpoint.py` relative to this skill directory. Python 3 standard library and Git are the only core dependencies. This helper records observations; it does not infer task completion, user intent, authorization, secret safety, writer ownership or full environment equivalence. Review explicit paths and commands before using them.

## Capture and save

Reuse the existing task evidence directory first. Reserve a unique bundle instead of composing a path or choosing a version by mtime:

```sh
python3 /path/to/skill/scripts/checkpoint.py reserve \
  --repo /path/to/repo --task-id project-next-phase --title 'Project next phase' \
  --task-dir /path/to/existing/task
```

Pass exactly one of `--task-dir /absolute/existing/task` or `--output-root /absolute/root`. The latter creates `<output-root>/<task-id>/handoffs/<checkpoint-id>/`. The core has no Harness-specific default. Select the location from the actual task/project/workspace policy; if none exists, choose and disclose a task directory outside the captured inputs. Task IDs are stable lowercase ASCII slugs (letters/digits/hyphens/underscores, up to 96 characters); titles are readable single lines. Reuse an established ID. For a new task, use `<project>-<task>-<creation-date>`. These are navigation labels, not replacements for managed evaluation identities.

`reserve` atomically creates a private directory named `<YYYYMMDDTHHMMSSZ>-<8hex>`, for example `20261002T133000Z-a1b2c3d4`, and writes `location.json`. Its JSON returns exact snapshot/handoff/checkpoint paths, entry status (ABSENT/MANAGED/UNMANAGED) and the managed entry's digest (or null). An unmanaged entry stays untouched: follow its project convention, omit the navigation exclusion, and select task inputs explicitly so saving a version remains possible. A partial attempt keeps its own directory and never advances the entry. Human-facing times may use the user's timezone; the directory always uses UTC. Commands print JSON for the agent to use directly.

Choose task-related repo-relative `--scope` files/directories and explicit `--ref` files for required rules, current specs, ignored inputs and evidence outside the repo. Directories include Git tracked and nonignored untracked files; name an ignored file explicitly when relevant. Include dependency lock/config files needed for verification. Scope omission is not proof those files are irrelevant. Do not include secrets or private material merely because it is readable.

Keep helper artifacts outside those scopes/refs. With whole-repo scope `.`, use an output location outside that repo. Separate immutable version directories make concurrent tasks and later snapshots distinguishable. No auto-select by latest mtime.

Illustrative commands, replacing paths with actual task locations:

```sh
python3 /path/to/skill/scripts/checkpoint.py capture \
  --repo /path/to/repo --scope src --scope tests --scope package-lock.json \
  --navigation-entry HANDOFF.md \
  --ref /path/to/repo/AGENTS.md --ref /path/to/current-spec.md \
  --output /path/to/task/handoffs/001/snapshot.json
```

Capture records canonical worktree identity, branch/HEAD, selected index and status, file type/mode/content hashes and explicit reference hashes. It reads state twice and rejects observed drift; this is not an atomic filesystem snapshot. It does not copy file content or back up code. Symlink traversal, unsupported types/submodules and resource limits are errors, not truncated success. A selected leaf symlink describes the link, not its target contents; select the actual dependency separately if needed.

The optional `--navigation-entry HANDOFF.md` records a narrow exclusion for the project-root navigation file, including its index/status entry, so updating links does not invalidate the checkpoint. It cannot also be selected explicitly as a scope/ref and only accepts that exact filename. Existing entries must be valid generated navigation, not arbitrary project documents. Immutable handoff/spec/code/evidence inputs are still hashed; other file changes still cause DRIFT. Old snapshots without this field keep their original behavior. If capturing the whole repo, put the reserved bundle outside it.

After writing `handoff.md` using the template and reconciling sources, bind it:

```sh
python3 /path/to/skill/scripts/checkpoint.py seal \
  --snapshot /path/to/task/handoffs/001/snapshot.json \
  --handoff /path/to/task/handoffs/001/handoff.md \
  --output /path/to/task/handoffs/001/checkpoint.json
```

`seal` validates live state and all supplied references. Artifacts are new files; choose a new version instead of overwriting. Digests detect changes to referenced bytes, not the truth or provenance of their claims. The Markdown is independently readable; machine binding requires the referenced snapshot, receipts and logs to remain available.

## Fixed project entry

After successful sealing, publish navigation with the same task ID and title:

```sh
python3 /path/to/skill/scripts/checkpoint.py index \
  --checkpoint /absolute/bundle/checkpoint.json \
  --task-id project-next-phase --title 'Project next phase'
```

An existing entry requires `--expect-entry-sha256 <digest-read-earlier>` (returned by reserve). If another writer updates it, reread the entry and retry only this step with the new digest; preserve other task links. Do not refresh hashes blindly or rerun all checks. The command checks live MATCH, guards replacement, uses a cooperative lock and returns the actual entry digest. Like capture/seal, this is an observation, not filesystem isolation: later work can legitimately drift.

Default entry: `<repo>/HANDOFF.md`. It contains a readable task list plus a marked JSON block `{schema_version:1,kind:"handoff-index",repo,entries:{<task-id>:{title,checkpoint:{path,sha256},handoff:{path,sha256},sealed_at}}}`. This is navigation to immutable artifacts, not another task ledger, business state or verification receipt. Titles are not statuses; a saved task can remain WIP. Only a successfully sealed version can advance the entry. Keep older bundles. Machine readers must validate these references before relying on their bytes; integrity does not prove current repo state or authority.

For an explicit request to repair navigation to an already sealed version, read [navigation maintenance](maintenance.md). Ordinary saves require live MATCH; task changes need a new checkpoint.

Manual edits, unmanaged documents, symlinks, stale expected digests, missing/tampered artifacts, Git metadata, input overlap or a held lock block publication. Do not delete someone else's lock or replace an existing project document. An alternative `--entry` must be named HANDOFF.md inside the same repo and outside the captured inputs; the only supported navigation exclusion remains the root `HANDOFF.md`. If project rules prevent entry writes, deliver the sealed version path and disclose the missing navigation update.

For the same task ID, a different checkpoint must have a strictly later timezone-aware seal timestamp than the published one. Older or equal-time versions cannot roll back the entry even with its current digest; republishing the identical checkpoint is allowed. Clock regressions require inspection, not a force overwrite.

After index succeeds run the read-only resume command below and inspect its report. The normal save workflow is reserve → capture → write handoff → seal → index → resume. Verification is conditional on a genuine need; no automatic test run.

## Optional new verification

Use only when a new check is necessary and its command, environment and side effects are already authorized. A checkpoint request is not blanket authority to run migrations, network writes or expensive tests. Existing reliable evidence can be referenced in the handoff without rerunning it; missing version binding stays UNKNOWN.

```sh
python3 /path/to/skill/scripts/checkpoint.py verify \
  --snapshot /path/to/task/handoffs/001/snapshot.json \
  --output /path/to/task/handoffs/001/unit-check.json --timeout 120 \
  -- python3 -m unittest discover -s tests
```

The helper invokes explicit argv in the recorded repo without a shell, removing inherited `GIT_*` variables so Git-based checks cannot silently target another worktree. Other task environment variables are preserved. It captures bounded stdout/stderr, checks state before/after and records the actual exit status. PASS means this command exited zero with matching observed inputs; it is not overall task acceptance. FAIL is a completed nonzero check. Timeout, excessive output or changed/unobservable inputs yields UNKNOWN. Command selection still determines coverage. A process may change inputs and restore them between observations; do not claim continuous isolation or purity. Relevant environment or external services can change without a file digest changing, so recheck those assumptions at resume.

Generated files within the scope cause drift. Use the project's established ignored/external cache configuration or an authorized isolated execution copy; do not weaken input coverage to manufacture PASS. Changed inputs require a fresh snapshot before another check. Treat log paths as private task evidence and review before sharing. The helper cannot universally redact application output.

Supply each receipt as `--check /absolute/receipt.json` to `seal`. The receipt must bind to the selected snapshot and its retained logs. Do not relabel unbound historical output or user-written PASS as a helper receipt. Failure/UNKNOWN receipts may still be preserved; a handoff can faithfully describe unfinished work.

## Resume

Without a supplied version, read the project's `HANDOFF.md` after project rules, select the requested task and verify the selected checkpoint/handoff digests from its entry. If it has several tasks, use the user's stated goal or ask only for the missing selection; never guess by newest time. Then pass that exact checkpoint to the helper. The CLI still requires the checkpoint path; the Skill resolves navigation for the user.

```sh
python3 /path/to/skill/scripts/checkpoint.py resume \
  --checkpoint /path/to/task/handoffs/001/checkpoint.json --repo /path/to/repo
```

The report returns MATCH or DRIFT for the explicitly captured state and marks recorded checks FRESH or STALE. FRESH preserves the original PASS/FAIL/UNKNOWN; it never upgrades a failing or unrun check. Empty checks means no helper verification. Missing/tampered artifacts or a different worktree cause an error. Inspect differences and reacquire necessary evidence; do not update old hashes to conceal drift. Relocation across machines/worktrees is a separate reconciliation task, not automatic path rewriting.

CLI exit 0 means a report/receipt was produced, even when the report says DRIFT or the check says FAIL/UNKNOWN. Exit 2 is an input, collection or artifact error with a JSON explanation. Never use exit 0 alone as the task's Gate. Resume reads and reports; it does not execute the next action or rerun commands.

For managed AIOS evaluation, keep the original Record/Segment/Attempt and candidate/check receipts authoritative. Reference them here; this helper does not replace Coding Verification, lightweight eval, Work Eval or their lifecycle.

## Design provenance

Original AIOS implementation authorized by the user on 2026-10-02 after the GitHub comparison; no upstream runtime or source code is bundled. The initial authority and evidence design came from AIOS workspace and verification contracts. At runtime, follow the current host/project policies; this package does not load AIOS files or require a fixed AIOS installation. Design references: [GSD external-job reconciliation](https://github.com/open-gsd/gsd-core/blob/b18e8c53b2c03c82a54a2a358ad51603a7449c52/gsd-core/workflows/resume-project.md), [PWF task ownership](https://github.com/OthmanAdi/planning-with-files/blob/dab9d16fbd9314448b319d112e99f497d7638d89/README.md), and [project-checkpoint state comparison](https://github.com/ted0103/ai-project-checkpoint/blob/890cfa46cfbb2f8437df7fff4573fe3c13a194a6/skills/project-checkpoint/scripts/checkpoint.py). These references informed requirements, not installation or an upstream maintenance dependency.
