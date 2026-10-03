# Task checkpoint handoff

Save unfinished work so a new agent session can continue from the goal, current files, evidence and next action. This package combines a Markdown Skill with a Python/Git helper. It works through explicit file paths and does not require Personal AI OS or a particular agent runtime.

The Skill reconciles intent and authorization. The helper captures scoped file hashes, binds optional verification receipts, seals immutable checkpoint versions and maintains a project-level `HANDOFF.md` navigation entry.

## Requirements

- Python 3.9+ and Git, with no third-party Python dependencies.
- A local Git worktree for machine evidence. Without Git or shell access, the Skill can produce a manual handoff with machine verification marked UNKNOWN.
- A POSIX environment: macOS or Linux. The helper uses POSIX process groups, file modes and pipes; native Windows execution is not supported. CI is configured for Linux and macOS; a configured job alone is not evidence of a passing run.

The Skill instructions are currently written in Chinese; this README and the CLI reference are in English.

## Get and discover the Skill

Clone this repository into a directory you maintain:

```sh
git clone https://github.com/zhangjian0410/task-checkpoint-handoff.git
cd task-checkpoint-handoff
python3 scripts/checkpoint.py --help
python3 -B -m unittest discover -s tests
```

For a shared installation, select an exact tested commit with `git checkout <tested-commit>` and retain that revision in your installation record. To update, inspect the diff, run the tests, then check the actual host integration; rolling back the package must not rewrite saved checkpoint bundles.

An agent host can read [SKILL.md](SKILL.md) directly or register its absolute path in workspace navigation. Read its linked references when needed. Native Skill discovery is optional; install the entire directory, including references, assets and scripts, using that host's supported installation mechanism. This repository does not modify host configuration automatically.

Example explicit requests:

> Read `/absolute/path/to/task-checkpoint-handoff/SKILL.md` and save a handoff for this task. Use the existing task output directory and report the project entry and saved version.

> Read the Skill and resume the task from `/absolute/path/to/checkpoint.json` in `/absolute/path/to/project`. Reconcile current state and execution ownership before continuing.

Discussion about handoffs does not trigger saving. Reading or reviewing one does not authorize task execution. See [the invocation contract](SKILL.md#调用条件). Optional Codex metadata in `agents/openai.yaml` declares `allow_implicit_invocation: false`; the core helper does not read it, and direct file reads still follow the same explicit-invocation contract. Installing metadata does not enable an existing disabled native entry.

## Where saved work lives

The caller supplies the repository and exactly one absolute output argument to `reserve`:

| Argument | Version directory |
|---|---|
| `--task-dir /existing/task` | `/existing/task/handoffs/<checkpoint-id>/` |
| `--output-root /selected/root` | `/selected/root/<task-id>/handoffs/<checkpoint-id>/` |

Project/task output conventions take precedence. There is no implicit Codex, AIOS or home-directory default. Choose an output location outside the captured inputs; a whole-repository capture requires an output directory outside that repository. Keep actual task outputs out of this Skill's source repository.

A checkpoint ID has a UTC timestamp and random suffix, such as `20261002T133000Z-a1b2c3d4`. Each save creates a new version; sealed versions are not incrementally overwritten. The project-root `HANDOFF.md` links tasks to their saved versions. An existing unmanaged file is preserved, and an existing managed entry requires its previously observed digest when updating. A Dashboard must separately register its lookup sources; saving does not automatically configure a viewer.

## Minimal CLI example

The agent normally follows the full Skill workflow. This example demonstrates the helper after you have selected the task, read project rules and reconciled the handoff content. It writes a new bundle and the project's managed `HANDOFF.md`; use a disposable Git repository to try it first.

Set these to actual absolute paths. `SKILL` is the cloned package directory; `REPO` is the target worktree. `TASK_DIR` must be outside the target repository for this whole-repository example.

```sh
SKILL=/absolute/path/to/task-checkpoint-handoff
REPO=/absolute/path/to/project
TASK_DIR=/absolute/path/to/task-output

LOCATION=$(python3 "$SKILL/scripts/checkpoint.py" reserve \
  --repo "$REPO" --task-id project-next-phase --title 'Project next phase' \
  --task-dir "$TASK_DIR")
BUNDLE=$(printf '%s' "$LOCATION" | python3 -c \
  'import json,sys; print(json.load(sys.stdin)["bundle_dir"])')

python3 "$SKILL/scripts/checkpoint.py" capture \
  --repo "$REPO" --scope . --navigation-entry HANDOFF.md \
  --output "$BUNDLE/snapshot.json"
```

Copy [the handoff template](assets/handoff-template.md) to `$BUNDLE/handoff.md` and fill in the actual goal, authority, evidence, ownership gaps and next action. Add required external inputs with explicit `--ref` arguments during capture; `--scope .` alone does not cover external or all ignored dependencies. Do not copy secrets or whole chat transcripts.

After writing that document:

```sh
python3 "$SKILL/scripts/checkpoint.py" seal \
  --snapshot "$BUNDLE/snapshot.json" --handoff "$BUNDLE/handoff.md" \
  --output "$BUNDLE/checkpoint.json"

# First publication only: reserve must have reported entry_status ABSENT.
python3 "$SKILL/scripts/checkpoint.py" index \
  --checkpoint "$BUNDLE/checkpoint.json" \
  --task-id project-next-phase --title 'Project next phase'

python3 "$SKILL/scripts/checkpoint.py" resume \
  --checkpoint "$BUNDLE/checkpoint.json" --repo "$REPO"
```

For an existing managed entry, pass `--expect-entry-sha256` using the `expected_entry_sha256` returned by `reserve`. Preserve unmanaged entries and follow [the fixed-entry contract](references/helper.md#fixed-project-entry). Inspect each JSON result before proceeding; a failed operation must not be treated as a successful save.

`resume` reports MATCH or DRIFT for the declared scope; it does not continue the task or run tests. Exit 0 means a report was produced, even for DRIFT or a verification receipt with FAIL/UNKNOWN. Exit 2 reports an input, collection or artifact error. Inspect report fields, not only the shell exit code.

## What the evidence can establish

- Snapshots capture file types, modes and hashes, scoped Git state and explicit reference hashes. They do not copy source content or back up the worktree.
- MATCH means the selected bytes and recorded Git state match. It does not prove intent, writer ownership, authorization, secret safety or complete environment equivalence.
- Capture checks state twice; it is not an atomic filesystem snapshot or continuous isolation.
- Optional `verify` records an explicitly authorized command, bounded logs and its exit status. FRESH preserves the original PASS/FAIL/UNKNOWN result. It does not convert missing or failing checks into success.
- Machine-bound bundles are tied to the recorded worktree and referenced paths. Another machine/worktree requires reconciliation; moving the Markdown alone does not transfer the code or machine evidence.

## Maintenance and provenance

Read [helper.md](references/helper.md) for the command and evidence contract. Read [navigation maintenance](references/maintenance.md) only for explicitly requested historical registration or Dashboard lookup repair.

This repository owns its Skill, references, helper, template and tests. Global Rules, personal Axioms, credentials and host configuration remain with the consuming workspace. Project rules remain applicable.

Extracted from the AIOS working-tree package on 2026-10-03. Snapshot, checkpoint and navigation-index schema v1 are unchanged. Older invocations that relied on a Codex output default must supply an explicit output argument; existing sealed artifacts need no rewriting. [Design references](references/helper.md#design-provenance) informed requirements; no upstream runtime or source code is bundled.

Tests use disposable repositories and synthetic data. Passing them establishes helper behavior for the tested environment, not native discovery, cross-runtime end-to-end acceptance or general availability.

## License

[MIT](LICENSE). Copyright (c) 2026 zhangjian0410.
