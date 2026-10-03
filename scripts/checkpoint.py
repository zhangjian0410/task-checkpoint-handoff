#!/usr/bin/env python3
"""Scoped, local checkpoint evidence. Hashes are observations, not authorization.

Python standard library and Git only. No source files are copied, no command is
executed by resume, and Git collection uses no index/object writing commands.
"""

import argparse
import hashlib
import json
import math
import os
from pathlib import Path
import re
import selectors
import signal
import stat
import subprocess
import sys
import tempfile
import time
import uuid
from datetime import datetime, timezone
from urllib.parse import quote


VERSION = 1
MAX_FILES = 10000
MAX_FILE_BYTES = 32 * 1024 * 1024
MAX_TOTAL_BYTES = 256 * 1024 * 1024
MAX_JSON_BYTES = 16 * 1024 * 1024
MAX_GIT_BYTES = 16 * 1024 * 1024
MAX_LOG_BYTES = 2 * 1024 * 1024
COLLECT_SECONDS = 30
ENTRY_START = "<!-- task-checkpoint-handoff:index:v1\n"
ENTRY_END = "\n-->\n"
MAX_ENTRY_TASKS = 64


class CheckpointError(Exception):
    pass


def fail(message):
    raise CheckpointError(message)


def now():
    return datetime.now(timezone.utc).isoformat()


def sealed_time(value):
    if not isinstance(value, str) or len(value) > 80 or any(ord(c) < 32 for c in value):
        fail("Invalid sealed timestamp")
    stamp = datetime.fromisoformat(value)
    if stamp.utcoffset() is None:
        fail("Sealed timestamp must include a timezone")
    return stamp


def encoded(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()


def digest(value):
    return hashlib.sha256(encoded(value)).hexdigest()


def absolute(path):
    if not isinstance(path, str) or not path or "\x00" in path:
        fail("Expected a nonempty path string")
    return Path(os.path.abspath(path))


def no_symlink_parents(path):
    for parent in path.parents:
        if parent.is_symlink():
            fail("Symlink parent is unsupported: " + str(parent))


def contained(path, directory):
    return path == directory or directory in path.parents


def read_regular(path, limit=MAX_JSON_BYTES):
    no_symlink_parents(path)
    try:
        before = path.lstat()
        if not stat.S_ISREG(before.st_mode) or before.st_size > limit:
            fail("Expected bounded regular evidence file: " + str(path))
        # O_NONBLOCK prevents a file-to-FIFO race from hanging collection;
        # O_NOFOLLOW rejects a leaf that was swapped for a symlink.
        fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | getattr(os, "O_NOFOLLOW", 0))
        with os.fdopen(fd, "rb") as handle:
            opened = os.fstat(handle.fileno())
            if not stat.S_ISREG(opened.st_mode) or (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino):
                fail("Evidence file changed while opening: " + str(path))
            data = handle.read(limit + 1)
            after = os.fstat(handle.fileno())
        if len(data) > limit or identity(before) != identity(after) or identity(after) != identity(path.lstat()):
            fail("Evidence file changed during reading: " + str(path))
        return data
    except OSError as exc:
        fail("Cannot read evidence file %s: %s" % (path, exc))


def identity(info):
    return (info.st_dev, info.st_ino, info.st_mode, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def descriptor(path):
    path = absolute(str(path))
    return {"path": str(path), "sha256": hashlib.sha256(read_regular(path)).hexdigest()}


def read_bound(reference):
    if not isinstance(reference, dict) or not isinstance(reference.get("sha256"), str):
        fail("Malformed evidence reference")
    path = absolute(reference.get("path"))
    data = read_regular(path)
    if hashlib.sha256(data).hexdigest() != reference["sha256"]:
        fail("Evidence digest mismatch: " + str(path))
    return data


def parse_document(data, kind):
    try:
        value = json.loads(data)
    except (ValueError, UnicodeError, RecursionError):
        fail("Invalid JSON evidence")
    if not isinstance(value, dict) or type(value.get("schema_version")) is not int or value.get("schema_version") != VERSION or value.get("kind") != kind:
        fail("Expected schema_version 1 " + kind + " evidence")
    return value


def load_document(path, kind):
    return parse_document(read_regular(absolute(str(path))), kind)


def load_referenced(path, kind):
    path = absolute(str(path))
    data = read_regular(path)
    return parse_document(data, kind), {"path": str(path), "sha256": hashlib.sha256(data).hexdigest()}


def publish(path, data):
    """Publish a complete 0600 file with no-clobber semantics on this filesystem."""
    path = absolute(str(path))
    no_symlink_parents(path)
    if path.exists() or path.is_symlink():
        fail("Refusing to overwrite existing artifact: " + str(path))
    path.parent.mkdir(parents=True, exist_ok=True)
    no_symlink_parents(path)
    temp = None
    try:
        fd, temp = tempfile.mkstemp(prefix=".checkpoint-", dir=str(path.parent))
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.link(temp, path)
    except OSError as exc:
        fail("Cannot publish artifact %s: %s" % (path, exc))
    finally:
        if temp is not None:
            os.unlink(temp)


def publish_json(path, value):
    publish(path, encoded(value) + b"\n")
    return dict(value, artifact=str(absolute(str(path))))


def task_id(value):
    if not isinstance(value, str) or not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,95}", value):
        fail("Task ID must be a stable lowercase ASCII slug, up to 96 characters")
    return value


def task_title(value):
    if not isinstance(value, str) or not value.strip() or len(value) > 160 or any(ord(c) < 32 for c in value):
        fail("Task title must be a nonempty single line, up to 160 characters")
    return value


def entry_bytes(value):
    metadata = encoded(value).decode().replace("<", "\\u003c").replace(">", "\\u003e")
    lines = ["# Project handoffs", "", ENTRY_START + metadata + ENTRY_END,
             "Navigation only. Read the referenced handoff and run resume before continuing.", ""]
    for key, row in sorted(value["entries"].items()):
        title = re.sub(r"([\\`*_{}\[\]()#+.!<>|])", r"\\\1", row["title"])
        lines += ["## " + title, "", "Task ID: `" + key + "`",
                  "Sealed at: " + row["sealed_at"],
                  "[Read handoff](" + quote(row["handoff"]["path"], safe="/") + ")",
                  "Checkpoint: `" + row["checkpoint"]["path"].replace("`", "\\`") + "`", "",
                  "Resume: ask $task-checkpoint-handoff to read this task's handoff and checkpoint; "
                  "respect the current project rules and requested execution scope.", ""]
    return ("\n".join(lines) + "\n").encode()


def read_entry(path, repo):
    data = read_regular(path, 256 * 1024)
    try:
        text = data.decode("utf-8")
        if text.count(ENTRY_START) != 1:
            fail("Existing HANDOFF entry is unmanaged; preserve it and use its project convention")
        metadata = text.split(ENTRY_START, 1)[1].split(ENTRY_END, 1)[0]
        value = json.loads(metadata)
        if type(value.get("schema_version")) is not int or value["schema_version"] != VERSION or value.get("kind") != "handoff-index" or value.get("repo") != repo:
            fail("Invalid HANDOFF entry identity")
        entries = value.get("entries")
        if not isinstance(entries, dict) or not 0 < len(entries) <= MAX_ENTRY_TASKS:
            fail("Invalid HANDOFF task list")
        for key, row in entries.items():
            task_id(key)
            task_title(row["title"])
            sealed_time(row.get("sealed_at"))
            for name in ("checkpoint", "handoff"):
                reference = row[name]
                if not isinstance(reference, dict) or not Path(reference["path"]).is_absolute() or not re.fullmatch(r"[0-9a-f]{64}", reference["sha256"]):
                    fail("Invalid HANDOFF evidence reference")
        if data != entry_bytes(value):
            fail("HANDOFF entry has manual changes; preserve it instead of overwriting")
        return value, {"path": str(path), "sha256": hashlib.sha256(data).hexdigest()}
    except (UnicodeError, ValueError, KeyError, TypeError, AttributeError) as exc:
        fail("Malformed HANDOFF navigation entry: " + str(exc))


def private_directories(path):
    no_symlink_parents(path / "probe")
    missing = []
    current = path
    while not current.exists():
        missing.append(current)
        current = current.parent
    for directory in reversed(missing):
        try:
            directory.mkdir(mode=0o700)
        except FileExistsError:
            if not directory.is_dir() or directory.is_symlink():
                fail("Directory changed during reservation")
    no_symlink_parents(path / "probe")


def reserve(args):
    identifier, title = task_id(args.task_id), task_title(args.title)
    repo = absolute(args.repo).resolve(strict=True)
    if not repo.is_dir():
        fail("Repository/project directory does not exist")
    if bool(args.task_dir) == bool(args.output_root):
        fail("Specify exactly one --task-dir or --output-root from the project/workspace output policy")
    root = args.task_dir or str(Path(args.output_root) / identifier)
    if not Path(root).is_absolute() or ".." in Path(root).parts or ".git" in Path(root).parts:
        fail("Task output directory must be absolute, without traversal or Git metadata")
    parent = absolute(root) / "handoffs"
    entry = repo / "HANDOFF.md"
    expected = None
    entry_status = "ABSENT"
    if entry.exists() or entry.is_symlink():
        try:
            _, reference = read_entry(entry, str(repo))
            expected = reference["sha256"]
            entry_status = "MANAGED"
        except CheckpointError:
            # Reservation can proceed without replacing an unrelated document.
            entry_status = "UNMANAGED"
    private_directories(parent)
    for _ in range(8):
        checkpoint_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:8]
        bundle = parent / checkpoint_id
        try:
            bundle.mkdir(mode=0o700)
            break
        except FileExistsError:
            continue
    else:
        fail("Could not reserve a unique checkpoint directory")
    value = {"schema_version": VERSION, "kind": "location", "repo": str(repo),
             "task_id": identifier, "title": title, "checkpoint_id": checkpoint_id,
             "bundle_dir": str(bundle), "entry": str(entry), "expected_entry_sha256": expected,
             "entry_status": entry_status,
             "paths": {name: str(bundle / (name + suffix)) for name, suffix in
                       (("snapshot", ".json"), ("handoff", ".md"), ("checkpoint", ".json"))}}
    return publish_json(bundle / "location.json", value)


def index_checkpoint(args):
    identifier, title = task_id(args.task_id), task_title(args.title)
    checkpoint_path = absolute(args.checkpoint)
    checkpoint, checkpoint_reference = load_referenced(checkpoint_path, "checkpoint")
    snapshot = parse_document(read_bound(checkpoint["snapshot"]), "snapshot")
    state = snapshot_state(snapshot)
    repo = Path(state["repo"])
    sealed_at = checkpoint.get("created_at")
    timestamp = sealed_time(sealed_at)
    entry = absolute(args.entry) if args.entry else repo / "HANDOFF.md"
    no_symlink_parents(entry)
    if entry.name != "HANDOFF.md" or not contained(entry, repo) or not entry.parent.is_dir():
        fail("Project entry must be HANDOFF.md in an existing directory of this repository")
    Collector(repo).safe_path(entry)
    exempt = entry == repo / "HANDOFF.md" and state.get("navigation_entry") == "HANDOFF.md"
    if (any(contained(entry, repo / scope) for scope in state["scope"]) and not exempt) or any(contained(entry, Path(ref)) for ref in state["refs"]):
        fail("Entry overlaps captured inputs; recapture with --navigation-entry HANDOFF.md or choose an unscoped project entry")
    report = resume(argparse.Namespace(checkpoint=str(checkpoint_path), repo=str(repo)))
    if report["status"] != "MATCH" and not args.register_existing:
        fail("Only a live MATCH checkpoint can update the project entry; use --register-existing only for explicitly requested sealed-version navigation repair")
    lock = entry.with_name("." + entry.name + ".checkpoint-lock")
    try:
        fd = os.open(lock, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
    except FileExistsError:
        fail("Project entry is locked; inspect the writer before retrying, do not delete its lock automatically")
    lock_identity = os.fstat(fd)
    os.write(fd, encoded({"pid": os.getpid(), "created_at": now(), "entry": str(entry)}))
    os.close(fd)
    temporary = None
    try:
        existing = entry.exists() or entry.is_symlink()
        if existing:
            value, previous = read_entry(entry, str(repo))
            if args.expect_entry_sha256 != previous["sha256"]:
                fail("Entry changed or expected digest is missing; reread it before retrying")
        else:
            if args.expect_entry_sha256 is not None:
                fail("Expected project entry is missing")
            value = {"schema_version": VERSION, "kind": "handoff-index", "repo": str(repo), "entries": {}}
            previous = None
        read_bound(checkpoint_reference)
        handoff_reference = checkpoint["handoff"]
        read_bound(handoff_reference)
        if identifier not in value["entries"] and len(value["entries"]) >= MAX_ENTRY_TASKS:
            fail("Project entry exceeds task limit")
        current = value["entries"].get(identifier)
        if current and current["checkpoint"] != checkpoint_reference and timestamp <= sealed_time(current["sealed_at"]):
            fail("Checkpoint is not newer than the current task entry; preserve the published version")
        value["entries"][identifier] = {"title": title, "checkpoint": checkpoint_reference,
                                         "handoff": handoff_reference, "sealed_at": sealed_at}
        data = entry_bytes(value)
        if len(data) > 256 * 1024:
            fail("Project entry exceeds byte limit")
        fd, temporary = tempfile.mkstemp(prefix=".handoff-entry-", dir=str(entry.parent))
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        if previous is None:
            os.link(temporary, entry)
        else:
            read_bound(previous)
            os.replace(temporary, entry)
            temporary = None
        return {"status": "INDEXED", "task_id": identifier, "entry": descriptor(entry),
                "checkpoint": checkpoint_reference, "workspace_status": report["status"],
                "registration_mode": "existing-sealed" if args.register_existing else "live-save",
                "notice": "Navigation only; existing sealed evidence and workspace drift are not renewed verification or execution authority."}
    finally:
        if temporary is not None:
            os.unlink(temporary)
        try:
            current_lock = lock.lstat()
            if (current_lock.st_dev, current_lock.st_ino) == (lock_identity.st_dev, lock_identity.st_ino):
                os.unlink(lock)
        except FileNotFoundError:
            pass


def run_bounded(argv, cwd, timeout, limit, env=None):
    """Drain separate streams without unbounded buffering; own process group only."""
    started = time.monotonic()
    process = subprocess.Popen(argv, cwd=cwd, env=env, stdin=subprocess.DEVNULL,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               start_new_session=True)
    outputs = {"stdout": bytearray(), "stderr": bytearray()}
    reason = None
    total = 0
    selector = selectors.DefaultSelector()
    for stream in outputs:
        pipe = getattr(process, stream)
        os.set_blocking(pipe.fileno(), False)
        selector.register(pipe, selectors.EVENT_READ, stream)
    try:
        while selector.get_map() or process.poll() is None:
            if time.monotonic() - started > timeout:
                reason = "timeout"
                break
            for key, _ in selector.select(min(0.05, max(0, timeout - (time.monotonic() - started)))):
                chunk = os.read(key.fileobj.fileno(), 65536)
                if not chunk:
                    selector.unregister(key.fileobj)
                    continue
                remaining = max(0, limit - total)
                outputs[key.data].extend(chunk[:remaining])
                total += len(chunk)
                if total > limit:
                    reason = "output_limit"
                    break
            if reason:
                break
    finally:
        if reason or process.poll() is None:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        process.wait(timeout=5)
        selector.close()
        process.stdout.close()
        process.stderr.close()
    return process.returncode, {key: bytes(value) for key, value in outputs.items()}, reason


class Collector:
    def __init__(self, repo):
        self.deadline = time.monotonic() + COLLECT_SECONDS
        self.total = 0
        self.repo = absolute(str(repo)).resolve(strict=True)
        self.environment = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
        self.environment["GIT_OPTIONAL_LOCKS"] = "0"
        actual = self.git(["rev-parse", "--show-toplevel"]).decode().strip()
        if Path(actual).resolve() != self.repo:
            fail("--repo must be the Git worktree root")
        self.git_dir = Path(os.fsdecode(self.git(["rev-parse", "--absolute-git-dir"]).rstrip(b"\n"))).resolve()
        common = os.fsdecode(self.git(["rev-parse", "--git-common-dir"]).rstrip(b"\n"))
        self.common_dir = (self.repo / common).resolve()

    def git(self, args, allow_failure=False):
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            fail("State collection time limit exceeded")
        argv = ["git", "-c", "core.fsmonitor=false", "-c", "core.untrackedCache=false",
                "-c", "status.renames=false", "-C", str(self.repo)] + args
        code, output, reason = run_bounded(argv, str(self.repo), remaining, MAX_GIT_BYTES, self.environment)
        if reason:
            fail("Git collection exceeded " + reason)
        if code and not allow_failure:
            fail("Git collection failed: " + output["stderr"].decode(errors="replace")[:1000])
        return output["stdout"] if not code else None

    def safe_path(self, path):
        no_symlink_parents(path)
        if contained(path, self.repo) and ".git" in path.relative_to(self.repo).parts:
            fail("Git metadata cannot be an input")
        if contained(path, self.git_dir) or contained(path, self.common_dir):
            fail("Git metadata cannot be an input")

    def manifest(self, path):
        self.safe_path(path)
        if time.monotonic() >= self.deadline:
            fail("State collection time limit exceeded")
        try:
            info = path.lstat()
        except FileNotFoundError:
            return {"type": "missing"}
        mode = stat.S_IMODE(info.st_mode)
        if stat.S_ISLNK(info.st_mode):
            data = os.fsencode(os.readlink(path))
            value = {"type": "symlink", "mode": mode, "sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data)}
        elif stat.S_ISREG(info.st_mode):
            if info.st_size > MAX_FILE_BYTES:
                fail("File exceeds byte limit: " + str(path))
            self.total += info.st_size
            if self.total > MAX_TOTAL_BYTES:
                fail("Inputs exceed total byte limit")
            data = read_regular(path, MAX_FILE_BYTES)
            value = {"type": "file", "mode": mode, "sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data)}
        else:
            fail("Unsupported directory, submodule, or special file: " + str(path))
        if identity(info) != identity(path.lstat()):
            fail("Input changed while collecting: " + str(path))
        return value

    def check_special_files(self, scopes, pathspec):
        # Git omits FIFOs/devices/sockets from ls-files, so check directory
        # entries too. Do not descend into ignored subtrees or leaf symlinks.
        ignored = {os.fsdecode(item).rstrip("/") for item in self.git(
            ["ls-files", "--others", "--ignored", "--exclude-standard", "--directory", "-z", "--"] + pathspec
        ).split(b"\0") if item}
        pending = [self.repo / scope for scope in scopes if (self.repo / scope).is_dir() and not (self.repo / scope).is_symlink()]
        seen = set()
        count = 0
        while pending:
            directory = pending.pop()
            if directory in seen:
                continue
            seen.add(directory)
            no_symlink_parents(directory / "probe")
            with os.scandir(directory) as entries:
                for entry in entries:
                    if time.monotonic() >= self.deadline:
                        fail("State collection time limit exceeded")
                    relative = str(Path(entry.path).relative_to(self.repo))
                    if entry.name == ".git" or relative in ignored:
                        continue
                    count += 1
                    if count > MAX_FILES:
                        fail("Directory inspection exceeds path limit")
                    mode = entry.stat(follow_symlinks=False).st_mode
                    if stat.S_ISDIR(mode):
                        pending.append(Path(entry.path))
                    elif not (stat.S_ISREG(mode) or stat.S_ISLNK(mode)):
                        fail("Unsupported special file: " + entry.path)

    def reference_manifest(self, path):
        result = self.manifest(path)
        if result["type"] == "missing":
            fail("Referenced evidence is missing: " + str(path))
        return result

    def collect(self, scopes, refs, navigation_entry=None):
        if not isinstance(scopes, list) or not scopes or len(scopes) > MAX_FILES:
            fail("An explicit nonempty scope list is required")
        normalized = []
        for item in scopes:
            if not isinstance(item, str) or not item or "\x00" in item or Path(item).is_absolute() or ".." in Path(item).parts:
                fail("Scope must be a repository-relative path without traversal")
            relative = str(Path(item))
            self.safe_path(self.repo / relative)
            normalized.append(relative)
        normalized = sorted(set(normalized))
        if not isinstance(refs, list) or len(refs) > MAX_FILES:
            fail("Invalid reference list")
        references = []
        for item in refs:
            if not isinstance(item, str) or not Path(item).is_absolute():
                fail("Reference paths must be absolute")
            references.append(str(absolute(item)))
        references = sorted(set(references))
        if navigation_entry is not None:
            if navigation_entry != "HANDOFF.md" or navigation_entry in normalized or str(self.repo / navigation_entry) in references:
                fail("Navigation exclusion is only HANDOFF.md and cannot also be an explicit input/ref")
            entry = self.repo / navigation_entry
            if entry.exists() or entry.is_symlink():
                read_entry(entry, str(self.repo))
        pathspec = [":(literal)" + scope for scope in normalized]
        # Global index mode records let a scope *inside* a submodule fail closed.
        all_index = self.git(["ls-files", "--stage", "-z"])
        for record in all_index.split(b"\0"):
            if record.startswith(b"160000 "):
                relative = os.fsdecode(record.split(b"\t", 1)[1])
                submodule = self.repo / relative
                if any(contained(self.repo / item, submodule) or contained(submodule, self.repo / item) for item in normalized):
                    fail("Selected submodules are unsupported: " + relative)
        self.check_special_files(normalized, pathspec)
        index = []
        files = set()
        for record in self.git(["ls-files", "--stage", "-z", "--"] + pathspec).split(b"\0"):
            if not record:
                continue
            metadata, filename = record.split(b"\t", 1)
            mode, object_id, stage = metadata.decode().split()
            relative = os.fsdecode(filename)
            if relative != navigation_entry:
                index.append({"path": relative, "mode": mode, "object": object_id, "stage": stage})
                files.add(relative)
        for filename in self.git(["ls-files", "--others", "--exclude-standard", "-z", "--"] + pathspec).split(b"\0"):
            if filename:
                files.add(os.fsdecode(filename))
        for item in normalized:
            path = self.repo / item
            # Explicit leaf paths include ignored and missing files; directories
            # follow Git's tracked/untracked set and do not sweep ignored data.
            if path.is_symlink() or not path.is_dir():
                files.add(item)
        files.discard(navigation_entry)
        if len(files) + len(references) > MAX_FILES:
            fail("Inputs exceed path limit")
        status_output = self.git(["status", "--porcelain=v1", "-z", "--untracked-files=all", "--"] + pathspec)
        status_entries = sorted(os.fsdecode(record) for record in status_output.split(b"\0")
                                if record and os.fsdecode(record)[3:] != navigation_entry)
        head = self.git(["rev-parse", "--verify", "HEAD"], allow_failure=True)
        branch = self.git(["symbolic-ref", "--quiet", "--short", "HEAD"], allow_failure=True)
        state = {
            "repo": str(self.repo), "git_dir": str(self.git_dir), "common_dir": str(self.common_dir),
            "head": head.decode().strip() if head else None, "branch": branch.decode().strip() if branch else None,
            "scope": normalized, "refs": references,
            "index": sorted(index, key=lambda row: (row["path"], row["stage"])), "status": status_entries,
            "files": [{"path": item, **self.manifest(self.repo / item)} for item in sorted(files)],
            "references": [{"path": item, **self.reference_manifest(Path(item))} for item in references],
        }
        if navigation_entry is not None:
            state["navigation_entry"] = navigation_entry
        return state


def observe(repo, scopes, refs, navigation_entry=None):
    first = Collector(repo).collect(scopes, refs, navigation_entry)
    second = Collector(repo).collect(scopes, refs, navigation_entry)
    if first != second:
        fail("Inputs changed between observations; retry after writers quiesce")
    return second


def snapshot_state(snapshot):
    state = snapshot.get("state")
    if not isinstance(state, dict) or digest(state) != snapshot.get("state_digest"):
        fail("Snapshot state digest mismatch")
    for key in ("repo", "git_dir", "common_dir"):
        if not isinstance(state.get(key), str) or not Path(state[key]).is_absolute():
            fail("Malformed snapshot identity")
    if not isinstance(state.get("scope"), list) or not isinstance(state.get("refs"), list):
        fail("Malformed snapshot scope")
    for key in ("files", "references"):
        if not isinstance(state.get(key), list) or len(state[key]) > MAX_FILES or not all(
            isinstance(item, dict) and isinstance(item.get("path"), str) for item in state[key]
        ):
            fail("Malformed snapshot manifest")
    return state


def safe_output(path, state):
    path = absolute(str(path))
    no_symlink_parents(path)
    if path.exists() or path.is_symlink():
        fail("Refusing to overwrite existing artifact: " + str(path))
    repo = Path(state["repo"])
    if contained(path, Path(state["git_dir"])) or contained(path, Path(state["common_dir"])) or (contained(path, repo) and ".git" in path.relative_to(repo).parts):
        fail("Artifacts cannot be written into Git metadata")
    if any(contained(path, repo / scope) for scope in state["scope"]):
        fail("Artifact overlaps selected input scope: " + str(path))
    if any(contained(path, Path(reference)) for reference in state["refs"]):
        fail("Artifact overlaps selected reference: " + str(path))
    return path


def capture(args):
    state = observe(args.repo, args.scope, args.ref, args.navigation_entry)
    safe_output(args.output, state)
    value = {"schema_version": VERSION, "kind": "snapshot", "created_at": now(),
             "state_digest": digest(state), "state": state}
    return publish_json(args.output, value)


def verify(args):
    if args.command[:1] != ["--"]:
        fail("verify requires an explicit command argv after --")
    command = args.command[1:]
    if not command or any(not isinstance(item, str) or "\x00" in item for item in command):
        fail("verify requires an explicit command argv after --")
    if not math.isfinite(args.timeout) or not 0 < args.timeout <= 3600:
        fail("Timeout must be greater than 0 and at most 3600 seconds")
    snapshot_path = absolute(args.snapshot)
    snapshot, snapshot_reference = load_referenced(snapshot_path, "snapshot")
    state = snapshot_state(snapshot)
    output = safe_output(args.output, state)
    log_paths = {stream: safe_output(str(output) + "." + stream + ".log", state) for stream in ("stdout", "stderr")}
    binding = dict(snapshot_reference, state_digest=snapshot["state_digest"])
    before = observe(state["repo"], state["scope"], state["refs"], state.get("navigation_entry"))
    if before != state:
        fail("Snapshot is stale; verification command was not executed")
    # Match collection's repository identity: inherited Git redirects must not
    # make a command check a different worktree. Keep other task environment.
    environment = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    started = now()
    try:
        code, outputs, reason = run_bounded(command, state["repo"], args.timeout, MAX_LOG_BYTES, environment)
    except OSError as exc:
        code, outputs, reason = None, {"stdout": b"", "stderr": str(exc).encode()}, "launch_error"
    finished = now()
    try:
        after_digest = digest(observe(state["repo"], state["scope"], state["refs"], state.get("navigation_entry")))
    except (CheckpointError, OSError) as exc:
        after_digest = None
        reason = "post_collection_error: " + str(exc)
    if after_digest != snapshot["state_digest"] and not reason:
        reason = "inputs_changed"
    # Retain actual command evidence even if it damaged its snapshot; this
    # receipt cannot subsequently be reused with the changed snapshot.
    try:
        read_bound(binding)
    except CheckpointError as exc:
        reason = "snapshot_changed: " + str(exc)
    logs = []
    for stream, path in log_paths.items():
        publish(path, outputs[stream])
        logs.append(dict(descriptor(path), stream=stream, bytes=len(outputs[stream])))
    value = {"schema_version": VERSION, "kind": "check", "snapshot": binding,
             "command": command, "started_at": started, "finished_at": finished,
             "exit_code": code, "result": "UNKNOWN" if reason else ("PASS" if code == 0 else "FAIL"),
             "reason": reason, "before_digest": digest(before), "after_digest": after_digest,
             "logs": logs}
    return publish_json(output, value)


def checked_receipt(reference, snapshot_reference, snapshot):
    receipt = parse_document(read_bound(reference), "check")
    binding = receipt.get("snapshot")
    if not isinstance(binding, dict) or any(binding.get(key) != snapshot_reference[key] for key in ("path", "sha256")) or binding.get("state_digest") != snapshot["state_digest"]:
        fail("Check receipt belongs to a different snapshot")
    if receipt.get("result") not in ("PASS", "FAIL", "UNKNOWN"):
        fail("Malformed check result")
    if not isinstance(receipt.get("command"), list) or not receipt["command"] or not all(isinstance(arg, str) for arg in receipt["command"]):
        fail("Malformed check command")
    logs = receipt.get("logs")
    if not isinstance(logs, list) or len(logs) != 2 or {log.get("stream") for log in logs if isinstance(log, dict)} != {"stdout", "stderr"}:
        fail("Malformed check logs")
    for log in logs:
        data = read_bound(log)
        if len(data) != log.get("bytes"):
            fail("Check log length mismatch")
    if not receipt.get("started_at") or not receipt.get("finished_at"):
        fail("Check receipt lacks timestamps")
    if receipt["result"] in ("PASS", "FAIL"):
        if receipt.get("before_digest") != snapshot["state_digest"] or receipt.get("after_digest") != snapshot["state_digest"] or receipt.get("reason") is not None:
            fail("Check result contradicts observed input state")
        if not isinstance(receipt.get("exit_code"), int) or (receipt["exit_code"] == 0) != (receipt["result"] == "PASS"):
            fail("Check result contradicts exit code")
    return receipt


def seal(args):
    snapshot_path = absolute(args.snapshot)
    snapshot, snapshot_reference = load_referenced(snapshot_path, "snapshot")
    state = snapshot_state(snapshot)
    safe_output(args.output, state)
    handoff = descriptor(absolute(args.handoff))
    checks = [descriptor(absolute(path)) for path in args.check]
    for reference in checks:
        checked_receipt(reference, snapshot_reference, snapshot)
    if observe(state["repo"], state["scope"], state["refs"], state.get("navigation_entry")) != state:
        fail("Snapshot is stale; checkpoint was not sealed")
    read_bound(snapshot_reference)
    read_bound(handoff)
    for reference in checks:
        read_bound(reference)
    value = {"schema_version": VERSION, "kind": "checkpoint", "created_at": now(),
             "snapshot": snapshot_reference, "handoff": handoff, "checks": checks}
    return publish_json(args.output, value)


def differences(expected, current):
    changes = []
    for key in ("repo", "git_dir", "common_dir", "head", "branch", "scope", "refs", "index", "status", "navigation_entry"):
        if expected.get(key) != current.get(key):
            changes.append({"field": key})
    for key in ("files", "references"):
        before = {item["path"]: item for item in expected.get(key, [])}
        after = {item["path"]: item for item in current.get(key, [])}
        for path in sorted(before.keys() | after.keys()):
            if before.get(path) != after.get(path):
                changes.append({"field": key, "path": path, "change": "added" if path not in before else "removed" if path not in after else "changed"})
    return changes


def resume(args):
    checkpoint = load_document(args.checkpoint, "checkpoint")
    snapshot_reference = checkpoint.get("snapshot")
    snapshot = parse_document(read_bound(snapshot_reference), "snapshot")
    state = snapshot_state(snapshot)
    requested_repo = str(absolute(args.repo).resolve(strict=True))
    if requested_repo != state["repo"]:
        fail("Repository identity mismatch; same-worktree recovery is required")
    read_bound(checkpoint.get("handoff"))
    checks = checkpoint.get("checks")
    if not isinstance(checks, list) or len(checks) > MAX_FILES:
        fail("Malformed checkpoint checks")
    receipts = [(reference, checked_receipt(reference, snapshot_reference, snapshot)) for reference in checks]
    current = observe(args.repo, state["scope"], state["refs"], state.get("navigation_entry"))
    if any(current[key] != state[key] for key in ("repo", "git_dir", "common_dir")):
        fail("Git worktree identity mismatch")
    changes = differences(state, current)
    state_digest = digest(current)
    return {"schema_version": VERSION, "status": "DRIFT" if changes else "MATCH", "differences": changes,
            "state_digest": state_digest, "checks": [
                {"path": reference["path"], "result": receipt["result"],
                 "freshness": "FRESH" if not changes and receipt.get("before_digest") == state_digest and receipt.get("after_digest") == state_digest else "STALE"}
                for reference, receipt in receipts],
            "notice": "Scoped byte observations only; not authorization, ownership, or full environment equivalence."}


class Parser(argparse.ArgumentParser):
    def error(self, message):
        fail(message)


def main():
    parser = Parser(description=__doc__)
    subparsers = parser.add_subparsers(dest="action", required=True)
    reserve_parser = subparsers.add_parser("reserve")
    reserve_parser.add_argument("--repo", required=True)
    reserve_parser.add_argument("--task-id", required=True)
    reserve_parser.add_argument("--title", required=True)
    reserve_parser.add_argument("--task-dir")
    reserve_parser.add_argument("--output-root")
    index_parser = subparsers.add_parser("index")
    index_parser.add_argument("--checkpoint", required=True)
    index_parser.add_argument("--task-id", required=True)
    index_parser.add_argument("--title", required=True)
    index_parser.add_argument("--entry")
    index_parser.add_argument("--expect-entry-sha256")
    index_parser.add_argument("--register-existing", action="store_true",
                              help="Explicitly register intact sealed evidence for lookup after worktree drift; does not refresh the snapshot or checks")
    capture_parser = subparsers.add_parser("capture")
    capture_parser.add_argument("--repo", required=True)
    capture_parser.add_argument("--scope", action="append", required=True)
    capture_parser.add_argument("--ref", action="append", default=[])
    capture_parser.add_argument("--navigation-entry", choices=["HANDOFF.md"])
    capture_parser.add_argument("--output", required=True)
    verify_parser = subparsers.add_parser("verify")
    verify_parser.add_argument("--snapshot", required=True)
    verify_parser.add_argument("--output", required=True)
    verify_parser.add_argument("--timeout", type=float, default=300)
    verify_parser.add_argument("command", nargs=argparse.REMAINDER)
    seal_parser = subparsers.add_parser("seal")
    seal_parser.add_argument("--snapshot", required=True)
    seal_parser.add_argument("--handoff", required=True)
    seal_parser.add_argument("--check", action="append", default=[])
    seal_parser.add_argument("--output", required=True)
    resume_parser = subparsers.add_parser("resume")
    resume_parser.add_argument("--checkpoint", required=True)
    resume_parser.add_argument("--repo", required=True)
    try:
        args = parser.parse_args()
        value = {"reserve": reserve, "index": index_checkpoint, "capture": capture,
                 "verify": verify, "seal": seal, "resume": resume}[args.action](args)
        print(json.dumps(value, ensure_ascii=True, sort_keys=True))
        return 0
    except (CheckpointError, OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError, RecursionError) as exc:
        print(json.dumps({"error": str(exc), "status": "ERROR"}, ensure_ascii=True))
        return 2


if __name__ == "__main__":
    sys.exit(main())
