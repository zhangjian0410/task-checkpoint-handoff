"""Black-box acceptance tests; all Git and command effects stay in temporary repos."""

import hashlib
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import time
import unittest


HELPER = Path(__file__).resolve().parents[1] / "scripts" / "checkpoint.py"


class CheckpointCLITests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="checkpoint-cli-test-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.repo = self.root / "repo"
        self.repo.mkdir()
        self.evidence = self.root / "evidence"
        self.evidence.mkdir()
        self.sequence = 0
        self.env = dict(os.environ)
        for key in list(self.env):
            if key.startswith("GIT_"):
                del self.env[key]
        self.env.update(GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM="1")
        self.git("-c", "init.templateDir=", "init", "-q")
        self.git("config", "user.name", "Checkpoint Test")
        self.git("config", "user.email", "checkpoint-test@example.invalid")
        self.git("config", "core.filemode", "true")
        # Fixture commits must not leave a background Git maintenance writer
        # racing the read-only metadata assertions on newer Git versions.
        self.git("config", "maintenance.auto", "false")
        (self.repo / "src").mkdir()
        self.source = self.repo / "src" / "module.py"
        self.source.write_text("VALUE = 1\n", encoding="utf-8")
        (self.repo / ".gitignore").write_text("src/ignored.txt\n", encoding="utf-8")
        self.git("add", ".")
        self.git("commit", "-qm", "test baseline")

    def git(self, *args):
        return subprocess.run(
            ["git", *args], cwd=self.repo, env=self.env, check=True,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=15,
        ).stdout.strip()

    def path(self, label, suffix=".json"):
        self.sequence += 1
        return self.evidence / f"{self.sequence:02d}-{label}{suffix}"

    def cli(self, *args, expected=0):
        proc = subprocess.run(
            [sys.executable, str(HELPER), *(str(arg) for arg in args)],
            cwd=self.repo, env=self.env, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True, timeout=20,
        )
        self.assertEqual(
            proc.returncode, expected,
            f"CLI {args!r}\nstdout={proc.stdout}\nstderr={proc.stderr}",
        )
        try:
            result = json.loads(proc.stdout)
        except json.JSONDecodeError:
            self.fail(f"CLI must return JSON, got {proc.stdout!r}; stderr={proc.stderr!r}")
        self.assertIsInstance(result, dict)
        return result

    def capture(self, scopes=("src",), refs=(), navigation=False):
        output = self.path("snapshot")
        args = ["capture", "--repo", self.repo]
        for scope in scopes:
            args.extend(("--scope", scope))
        for ref in refs:
            args.extend(("--ref", ref))
        if navigation:
            args.extend(("--navigation-entry", "HANDOFF.md"))
        self.cli(*args, "--output", output)
        return output

    def test_reserve_names_existing_task_directory_and_concurrent_unique_versions(self):
        from concurrent.futures import ThreadPoolExecutor
        args = ("reserve", "--repo", self.repo, "--task-id", "project-next-phase-20261002",
                "--title", "下一阶段建设", "--task-dir", self.evidence)
        with ThreadPoolExecutor(max_workers=3) as pool:
            locations = list(pool.map(lambda _: self.cli(*args), range(3)))
        self.assertEqual(len({row["bundle_dir"] for row in locations}), 3)
        for row in locations:
            self.assertRegex(row["checkpoint_id"], r"^\d{8}T\d{6}Z-[a-f0-9]{8}$")
            self.assertEqual(Path(row["bundle_dir"]).parent, self.evidence / "handoffs")
            self.assertEqual(stat.S_IMODE(Path(row["bundle_dir"]).stat().st_mode), 0o700)
            self.assertEqual(Path(row["paths"]["handoff"]).name, "handoff.md")
            self.assertEqual(row["expected_entry_sha256"], None)
            self.assertTrue(Path(row["artifact"]).is_file())

    def test_reserve_requires_explicit_output_policy(self):
        args = ("reserve", "--repo", self.repo, "--task-id", "stable-task", "--title", "Task")
        before = sorted(str(p.relative_to(self.root)) for p in self.root.rglob("*"))
        result = self.cli(*args, expected=2)
        self.assertIn("exactly one", result["error"])
        self.cli(*args, "--task-dir", self.evidence, "--output-root", self.evidence, expected=2)
        self.cli(*args, "--output-root", "relative-root", expected=2)
        self.assertEqual(before, sorted(str(p.relative_to(self.root)) for p in self.root.rglob("*")))
        custom = self.cli(*args, "--output-root", self.evidence)
        self.assertEqual(Path(custom["bundle_dir"]).parent, self.evidence / "stable-task/handoffs")
        self.cli("reserve", "--repo", self.repo, "--task-id", "../bad", "--title", "Task",
                 "--output-root", self.evidence, expected=2)

    def test_index_navigation_supports_whole_repo_and_multiple_tasks_without_false_drift(self):
        snapshot = self.capture(scopes=(".",), navigation=True)
        checkpoint, _ = self.seal(snapshot)
        result = self.cli("index", "--checkpoint", checkpoint, "--task-id", "first-task", "--title", "First task")
        self.assertEqual(result["status"], "INDEXED")
        entry = self.repo / "HANDOFF.md"
        self.assertEqual(Path(result["entry"]["path"]), entry)
        self.resumed(checkpoint, "MATCH")
        reserved = self.cli("reserve", "--repo", self.repo, "--task-id", "second-task", "--title", "Second task",
                            "--task-dir", self.evidence)
        self.assertEqual(reserved["expected_entry_sha256"], result["entry"]["sha256"])
        second, _ = self.seal(self.capture(scopes=(".",), navigation=True))
        self.cli("index", "--checkpoint", second, "--task-id", "second-task", "--title", "Second task",
                 "--expect-entry-sha256", result["entry"]["sha256"])
        self.assertIn("first-task", entry.read_text())
        self.assertIn("second-task", entry.read_text())
        self.resumed(checkpoint, "MATCH")
        self.source.write_text("VALUE = 5\n", encoding="utf-8")
        self.resumed(checkpoint, "DRIFT")
        before = entry.read_bytes()
        self.cli("index", "--checkpoint", second, "--task-id", "second-task", "--title", "Second task",
                 "--expect-entry-sha256", hashlib.sha256(entry.read_bytes()).hexdigest(), expected=2)
        self.assertEqual(before, entry.read_bytes())

    def test_register_existing_records_drift_without_rewriting_sealed_files(self):
        snapshot = self.capture(navigation=True)
        checkpoint, handoff = self.seal(snapshot)
        before = {path: path.read_bytes() for path in (snapshot, checkpoint, handoff)}
        self.source.write_text("VALUE = 5\n", encoding="utf-8")
        args = ("index", "--checkpoint", checkpoint, "--task-id", "existing", "--title", "Existing")
        self.cli(*args, expected=2)
        self.assertFalse((self.repo / "HANDOFF.md").exists())
        registered = self.cli(*args, "--register-existing")
        self.assertEqual(registered["workspace_status"], "DRIFT")
        self.assertEqual(registered["registration_mode"], "existing-sealed")
        self.resumed(checkpoint, "DRIFT")
        self.assertEqual(before, {path: path.read_bytes() for path in before})

    def test_register_existing_keeps_integrity_writer_and_document_guards(self):
        checkpoint, handoff = self.seal(self.capture(navigation=True))
        self.source.write_text("VALUE = 5\n", encoding="utf-8")
        args = ("index", "--checkpoint", checkpoint, "--task-id", "existing", "--title", "Existing", "--register-existing")
        entry = self.repo / "HANDOFF.md"
        entry.write_text("User-owned document\n")
        self.cli(*args, expected=2)
        self.assertEqual(entry.read_text(), "User-owned document\n")
        entry.unlink()
        lock = self.repo / ".HANDOFF.md.checkpoint-lock"
        lock.write_text("another writer")
        self.cli(*args, expected=2)
        self.assertTrue(lock.exists())
        lock.unlink()
        original = handoff.read_bytes()
        handoff.write_bytes(original + b"tampered")
        self.cli(*args, expected=2)
        self.assertFalse(entry.exists())
        handoff.write_bytes(original)
        self.cli(*args)
        before = entry.read_bytes()
        self.cli(*args, "--expect-entry-sha256", "0" * 64, expected=2)
        self.assertEqual(entry.read_bytes(), before)

    def test_index_rejects_stale_writer_lock_and_preserves_other_tasks(self):
        checkpoint, _ = self.seal(self.capture(navigation=True))
        first = self.cli("index", "--checkpoint", checkpoint, "--task-id", "first", "--title", "First")
        entry = self.repo / "HANDOFF.md"
        before = entry.read_bytes()
        self.cli("index", "--checkpoint", checkpoint, "--task-id", "second", "--title", "Second", expected=2)
        self.cli("index", "--checkpoint", checkpoint, "--task-id", "second", "--title", "Second",
                 "--expect-entry-sha256", "0" * 64, expected=2)
        lock = self.repo / ".HANDOFF.md.checkpoint-lock"
        lock.write_text("writer active")
        self.cli("index", "--checkpoint", checkpoint, "--task-id", "second", "--title", "Second",
                 "--expect-entry-sha256", first["entry"]["sha256"], expected=2)
        self.assertTrue(lock.exists())
        self.assertEqual(before, entry.read_bytes())

    def test_index_cannot_roll_back_task_even_with_current_entry_digest(self):
        old, _ = self.seal(self.capture(navigation=True))
        first = self.cli("index", "--checkpoint", old, "--task-id", "one", "--title", "One")
        newer, _ = self.seal(self.capture(navigation=True))
        latest = self.cli("index", "--checkpoint", newer, "--task-id", "one", "--title", "One",
                          "--expect-entry-sha256", first["entry"]["sha256"])
        entry = self.repo / "HANDOFF.md"
        before = entry.read_bytes()
        self.resumed(old, "MATCH")
        result = self.cli("index", "--checkpoint", old, "--task-id", "one", "--title", "One",
                          "--expect-entry-sha256", latest["entry"]["sha256"], expected=2)
        self.assertIn("not newer", result["error"])
        self.source.write_text("VALUE = 7\n", encoding="utf-8")
        registered = self.cli("index", "--checkpoint", old, "--task-id", "one", "--title", "One",
                              "--expect-entry-sha256", latest["entry"]["sha256"], "--register-existing", expected=2)
        self.assertIn("not newer", registered["error"])
        self.source.write_text("VALUE = 1\n", encoding="utf-8")
        self.assertEqual(entry.read_bytes(), before)
        peer, _ = self.seal(self.capture(navigation=True))
        document = self.read(peer)
        document["created_at"] = self.read(newer)["created_at"]
        peer.write_text(json.dumps(document))
        self.cli("index", "--checkpoint", peer, "--task-id", "one", "--title", "One",
                 "--expect-entry-sha256", latest["entry"]["sha256"], expected=2)
        self.assertEqual(entry.read_bytes(), before)
        self.cli("index", "--checkpoint", newer, "--task-id", "one", "--title", "One",
                 "--expect-entry-sha256", latest["entry"]["sha256"])
        self.assertEqual(entry.read_bytes(), before)

    def test_index_refuses_unmanaged_symlink_and_input_overlap(self):
        checkpoint, _ = self.seal(self.capture())
        entry = self.repo / "HANDOFF.md"
        entry.write_text("Existing user document\n")
        reserved = self.cli("reserve", "--repo", self.repo, "--task-id", "one", "--title", "One",
                            "--task-dir", self.evidence)
        self.assertEqual(reserved["entry_status"], "UNMANAGED")
        self.cli("index", "--checkpoint", checkpoint, "--task-id", "one", "--title", "One", expected=2)
        self.assertEqual(entry.read_text(), "Existing user document\n")
        self.cli("capture", "--repo", self.repo, "--scope", ".", "--navigation-entry", "HANDOFF.md",
                 "--output", self.path("unmanaged"), expected=2)
        entry.unlink()
        entry.symlink_to(self.source)
        self.cli("index", "--checkpoint", checkpoint, "--task-id", "one", "--title", "One", expected=2)
        self.assertTrue(entry.is_symlink())
        entry.unlink()
        whole, _ = self.seal(self.capture(scopes=(".",)))
        self.cli("index", "--checkpoint", whole, "--task-id", "one", "--title", "One", expected=2)
        self.assertFalse(entry.exists())

    def test_navigation_exclusion_is_narrow_and_cannot_hide_explicit_refs(self):
        for scopes, refs in ((('HANDOFF.md',), ()), (('src',), (self.repo / 'HANDOFF.md',))):
            args = ["capture", "--repo", self.repo, "--navigation-entry", "HANDOFF.md"]
            for scope in scopes: args += ["--scope", scope]
            for ref in refs: args += ["--ref", ref]
            self.cli(*args, "--output", self.path("invalid-exclusion"), expected=2)
        self.cli("capture", "--repo", self.repo, "--scope", "src", "--navigation-entry", "src/module.py",
                 "--output", self.path("code-exclusion"), expected=2)

    def verify(self, snapshot, code="print('check passed')", timeout=None):
        output = self.path("check")
        args = ["verify", "--snapshot", snapshot, "--output", output]
        if timeout is not None:
            args.extend(("--timeout", str(timeout)))
        self.cli(*args, "--", sys.executable, "-c", code)
        return output

    def test_verification_ignores_git_redirects_but_preserves_task_environment(self):
        other = self.root / "other-repo"
        subprocess.run(["git", "clone", "-q", str(self.repo), str(other)],
                       env=self.env, check=True, capture_output=True, timeout=15)
        self.source.write_text("VALUE = 2\n", encoding="utf-8")
        snapshot = self.capture()
        self.env.update(GIT_DIR=str(other / ".git"), GIT_WORK_TREE=str(other),
                        CHECKPOINT_TEST_CONTEXT="preserved")
        output = self.path("redirected-git-check")
        result = self.cli("verify", "--snapshot", snapshot, "--output", output,
                          "--", "git", "diff", "--exit-code", "--", "src")
        self.assertEqual(result["result"], "FAIL", "Git must check the dirty snapshot repo")
        self.assertEqual(result["exit_code"], 1)
        checkpoint, _ = self.seal(snapshot, (output,))
        resumed = self.resumed(checkpoint, "MATCH")
        self.assertEqual(resumed["checks"][0]["result"], "FAIL")
        self.assertEqual(resumed["checks"][0]["freshness"], "FRESH")
        context_check = self.verify(snapshot, "import os; assert os.environ['CHECKPOINT_TEST_CONTEXT'] == 'preserved'; assert not any(k.startswith('GIT_') for k in os.environ)")
        self.assertEqual(self.read(context_check)["result"], "PASS")

    def seal(self, snapshot, checks=()):
        handoff = self.path("handoff", ".md")
        handoff.write_text("# Task\nNext: inspect scoped code and pending requirements.\n", encoding="utf-8")
        output = self.path("checkpoint")
        args = ["seal", "--snapshot", snapshot, "--handoff", handoff]
        for check in checks:
            args.extend(("--check", check))
        self.cli(*args, "--output", output)
        return output, handoff

    def resumed(self, checkpoint, expected_status):
        result = self.cli("resume", "--checkpoint", checkpoint, "--repo", self.repo)
        self.assertEqual(result["status"], expected_status)
        if expected_status == "DRIFT":
            self.assertTrue(result["differences"])
        return result

    @staticmethod
    def read(path):
        return json.loads(path.read_text(encoding="utf-8"))

    @staticmethod
    def log_paths(receipt):
        """Resolve path+digest evidence references without fixing log labels."""
        paths = []

        def visit(value):
            if isinstance(value, dict):
                if "path" in value and "sha256" in value:
                    paths.append(Path(value["path"]))
                else:
                    for child in value.values():
                        visit(child)
            elif isinstance(value, list):
                for child in value:
                    visit(child)

        visit(receipt["logs"])
        return paths

    def test_unchanged_round_trip_does_not_invent_check_pass(self):
        snapshot = self.capture()
        checkpoint, _ = self.seal(snapshot)
        result = self.resumed(checkpoint, "MATCH")
        self.assertEqual(result["checks"], [])
        self.assertEqual(self.read(snapshot)["kind"], "snapshot")
        self.assertEqual(self.read(snapshot)["schema_version"], 1)

    def test_dirty_bytes_change_even_when_git_status_is_identical(self):
        self.source.write_text("VALUE = 2\n", encoding="utf-8")
        checkpoint, _ = self.seal(self.capture())
        before_status = self.git("status", "--porcelain")
        self.source.write_text("VALUE = 3\n", encoding="utf-8")
        self.assertEqual(self.git("status", "--porcelain"), before_status)
        self.resumed(checkpoint, "DRIFT")

    def test_staged_only_change_with_original_worktree_bytes(self):
        checkpoint, _ = self.seal(self.capture())
        self.source.write_text("VALUE = 2\n", encoding="utf-8")
        self.git("add", "src/module.py")
        self.source.write_text("VALUE = 1\n", encoding="utf-8")
        self.resumed(checkpoint, "DRIFT")

    def test_untracked_file_addition_is_drift(self):
        checkpoint, _ = self.seal(self.capture())
        (self.repo / "src" / "new.txt").write_text("new\n", encoding="utf-8")
        self.resumed(checkpoint, "DRIFT")

    def test_tracked_deletion_is_drift(self):
        checkpoint, _ = self.seal(self.capture())
        self.source.unlink()
        self.resumed(checkpoint, "DRIFT")

    def test_executable_mode_change_is_drift(self):
        self.source.chmod(0o644)
        checkpoint, _ = self.seal(self.capture())
        self.source.chmod(0o755)
        self.resumed(checkpoint, "DRIFT")

    def test_branch_and_head_change_are_drift_even_with_same_scoped_bytes(self):
        checkpoint, _ = self.seal(self.capture())
        self.git("checkout", "-qb", "test-other-branch")
        self.resumed(checkpoint, "DRIFT")
        checkpoint, _ = self.seal(self.capture())
        self.git("commit", "--allow-empty", "-qm", "new HEAD, same files")
        self.resumed(checkpoint, "DRIFT")

    def test_external_reference_content_change_is_drift(self):
        ref = self.root / "approved-spec.md"
        ref.write_text("Implement A\n", encoding="utf-8")
        checkpoint, _ = self.seal(self.capture(refs=(ref,)))
        ref.write_text("Implement B\n", encoding="utf-8")
        self.resumed(checkpoint, "DRIFT")

    def test_capture_rejects_missing_reference(self):
        output = self.path("invalid")
        self.cli("capture", "--repo", self.repo, "--scope", "src",
                 "--ref", self.root / "missing-spec.md", "--output", output, expected=2)
        self.assertFalse(output.exists())

    def test_resume_rejects_reference_that_disappeared(self):
        ref = self.root / "approved-spec.md"
        ref.write_text("Implement A\n", encoding="utf-8")
        checkpoint, _ = self.seal(self.capture(refs=(ref,)))
        ref.unlink()
        self.cli("resume", "--checkpoint", checkpoint, "--repo", self.repo, expected=2)

    def test_explicit_ignored_file_is_hashed(self):
        ignored = self.repo / "src" / "ignored.txt"
        ignored.write_text("one", encoding="utf-8")
        checkpoint, _ = self.seal(self.capture(scopes=("src/ignored.txt",)))
        ignored.write_text("two", encoding="utf-8")
        self.resumed(checkpoint, "DRIFT")

    def test_leaf_symlink_hashes_target_text_without_following_it(self):
        target = self.root / "outside.txt"
        target.write_text("outside one", encoding="utf-8")
        link = self.repo / "src" / "link"
        link.symlink_to(target)
        checkpoint, _ = self.seal(self.capture())
        target.write_text("outside two", encoding="utf-8")
        self.resumed(checkpoint, "MATCH")
        link.unlink()
        link.symlink_to(self.root / "different.txt")
        self.resumed(checkpoint, "DRIFT")

    def test_path_escape_metadata_and_absolute_scope_are_rejected(self):
        for scope in ("../outside", ".git", ".git/config", str(self.source)):
            with self.subTest(scope=scope):
                output = self.path("invalid")
                self.cli("capture", "--repo", self.repo, "--scope", scope,
                         "--output", output, expected=2)
                self.assertFalse(output.exists())

    def test_intermediate_symlink_is_rejected(self):
        outside = self.root / "outside"
        outside.mkdir()
        (outside / "secret").write_text("secret", encoding="utf-8")
        (self.repo / "src" / "link").symlink_to(outside, target_is_directory=True)
        self.cli("capture", "--repo", self.repo, "--scope", "src/link/secret",
                 "--output", self.path("invalid"), expected=2)

    @unittest.skipUnless(hasattr(os, "mkfifo"), "requires POSIX named pipes")
    def test_special_file_is_rejected_without_blocking(self):
        os.mkfifo(self.repo / "src" / "pipe")
        self.cli("capture", "--repo", self.repo, "--scope", "src",
                 "--output", self.path("invalid"), expected=2)

    def test_gitlink_is_rejected(self):
        head = self.git("rev-parse", "HEAD")
        (self.repo / "src" / "sub").mkdir()
        self.git("update-index", "--add", "--cacheinfo", f"160000,{head},src/sub")
        self.cli("capture", "--repo", self.repo, "--scope", "src",
                 "--output", self.path("invalid"), expected=2)

    def test_real_check_pass_has_logs_and_fresh_binding(self):
        snapshot = self.capture()
        check = self.verify(snapshot, "import sys; print('stdout proof'); print('stderr proof', file=sys.stderr)")
        receipt = self.read(check)
        self.assertEqual(receipt["result"], "PASS")
        self.assertEqual(receipt["before_digest"], self.read(snapshot)["state_digest"])
        self.assertEqual(receipt["after_digest"], receipt["before_digest"])
        logs = self.log_paths(receipt)
        self.assertTrue(logs)
        logged = "\n".join(path.read_text(encoding="utf-8") for path in logs)
        self.assertIn("stdout proof", logged)
        self.assertIn("stderr proof", logged)
        checkpoint, _ = self.seal(snapshot, (check,))
        result = self.resumed(checkpoint, "MATCH")
        self.assertEqual(result["checks"][0]["result"], "PASS")
        self.assertEqual(result["checks"][0]["freshness"], "FRESH")

    def test_nonzero_command_is_recorded_as_fail(self):
        snapshot = self.capture()
        check = self.verify(snapshot, "import sys; print('failure proof'); sys.exit(7)")
        self.assertEqual(self.read(check)["result"], "FAIL")
        checkpoint, _ = self.seal(snapshot, (check,))
        result = self.resumed(checkpoint, "MATCH")
        self.assertEqual(result["checks"][0]["result"], "FAIL")

    def test_timeout_is_unknown_and_terminates_owned_process_group(self):
        marker = self.root / "late-marker"
        snapshot = self.capture()
        child = f"import time; from pathlib import Path; time.sleep(1); Path({str(marker)!r}).write_text('late')"
        code = f"import subprocess, sys, time; subprocess.Popen([sys.executable, '-c', {child!r}]); time.sleep(5)"
        check = self.verify(snapshot, code, timeout=0.1)
        self.assertEqual(self.read(check)["result"], "UNKNOWN")
        time.sleep(1.05)
        self.assertFalse(marker.exists())

    def test_command_that_changes_inputs_cannot_get_pass(self):
        snapshot = self.capture()
        check = self.verify(snapshot, "from pathlib import Path; Path('src/module.py').write_text('VALUE = 9\\n')")
        receipt = self.read(check)
        self.assertEqual(receipt["result"], "UNKNOWN")
        self.assertNotEqual(receipt["before_digest"], receipt["after_digest"])

    def test_stale_snapshot_prevents_command_execution(self):
        snapshot = self.capture()
        marker = self.root / "should-not-run"
        self.source.write_text("VALUE = 9\n", encoding="utf-8")
        output = self.path("check")
        code = f"from pathlib import Path; Path({str(marker)!r}).write_text('ran')"
        self.cli("verify", "--snapshot", snapshot, "--output", output,
                 "--", sys.executable, "-c", code, expected=2)
        self.assertFalse(marker.exists())
        self.assertFalse(output.exists())

    def test_existing_outputs_are_immutable_and_verification_does_not_rerun(self):
        snapshot = self.capture()
        before = snapshot.read_bytes()
        self.cli("capture", "--repo", self.repo, "--scope", "src",
                 "--output", snapshot, expected=2)
        self.assertEqual(snapshot.read_bytes(), before)
        check = self.verify(snapshot)
        before = check.read_bytes()
        marker = self.root / "should-not-rerun"
        code = f"from pathlib import Path; Path({str(marker)!r}).write_text('ran')"
        self.cli("verify", "--snapshot", snapshot, "--output", check,
                 "--", sys.executable, "-c", code, expected=2)
        self.assertEqual(check.read_bytes(), before)
        self.assertFalse(marker.exists())
        for artifact in (snapshot, check, *self.log_paths(self.read(check))):
            self.assertEqual(stat.S_IMODE(artifact.stat().st_mode), 0o600)

    def test_changed_state_marks_previously_passing_check_stale(self):
        snapshot = self.capture()
        check = self.verify(snapshot)
        checkpoint, _ = self.seal(snapshot, (check,))
        self.source.write_text("VALUE = 8\n", encoding="utf-8")
        result = self.resumed(checkpoint, "DRIFT")
        self.assertEqual(result["checks"][0]["result"], "PASS")
        self.assertEqual(result["checks"][0]["freshness"], "STALE")

    def test_tampered_or_missing_evidence_is_not_reused(self):
        for artifact_type in ("snapshot", "receipt", "handoff", "log", "missing_log"):
            with self.subTest(artifact=artifact_type):
                snapshot = self.capture()
                check = self.verify(snapshot)
                checkpoint, handoff = self.seal(snapshot, (check,))
                target = {
                    "snapshot": snapshot, "receipt": check, "handoff": handoff,
                    "log": self.log_paths(self.read(check))[0],
                    "missing_log": self.log_paths(self.read(check))[0],
                }[artifact_type]
                if artifact_type == "missing_log":
                    target.unlink()
                else:
                    target.write_bytes(target.read_bytes() + b"\ntampered\n")
                self.cli("resume", "--checkpoint", checkpoint,
                         "--repo", self.repo, expected=2)

    def test_different_repository_identity_blocks_reuse(self):
        checkpoint, _ = self.seal(self.capture())
        other = self.root / "other-repo"
        subprocess.run(["git", "clone", "-q", "--local", str(self.repo), str(other)],
                       env=self.env, check=True, capture_output=True, timeout=15)
        self.cli("resume", "--checkpoint", checkpoint, "--repo", other, expected=2)

    def test_verification_logs_cannot_overlap_scoped_inputs(self):
        snapshot = self.capture()
        marker = self.root / "overlap-command"
        code = f"from pathlib import Path; Path({str(marker)!r}).write_text('ran')"
        self.cli("verify", "--snapshot", snapshot, "--output", self.repo / "src" / "check.json",
                 "--", sys.executable, "-c", code, expected=2)
        self.assertFalse(marker.exists())

    def test_read_only_workflow_does_not_mutate_git_metadata(self):
        def git_metadata():
            result = {}
            for path in (self.repo / ".git").rglob("*"):
                if path.is_file():
                    info = path.stat()
                    result[str(path.relative_to(self.repo))] = (
                        hashlib.sha256(path.read_bytes()).hexdigest(), info.st_mtime_ns,
                        stat.S_IMODE(info.st_mode),
                    )
            return result

        before = git_metadata()
        snapshot = self.capture()
        check = self.verify(snapshot)
        checkpoint, _ = self.seal(snapshot, (check,))
        self.resumed(checkpoint, "MATCH")
        self.assertEqual(git_metadata(), before)


if __name__ == "__main__":
    unittest.main()
