#!/usr/bin/env python3
"""Coordinator-owned execution policy and disposable SSH/Slurm verification.

Transport carries a local candidate Git bundle and coordinator harness bytes,
never candidate-selected programs. Same-UID writers and hostile tests are not
an OS isolation boundary. No forge writes or credential provisioning occur.
"""
import argparse
import base64
from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import shlex
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
import uuid

import child_environment as CE

POLICY = "verification-execution.json"
BUNDLE_REF = "refs/heads/verification-candidate"
FILES = ("candidate.bundle", "request.json", "remote_verify.py", "verify.py",
         "child_environment.py")
TERMINAL = {"COMPLETED", "FAILED", "CANCELLED", "TIMEOUT", "OUT_OF_MEMORY",
            "NODE_FAIL", "PREEMPTED", "BOOT_FAIL", "DEADLINE", "REVOKED"}


def absolute(value, label):
    if (not isinstance(value, str) or not value.startswith("/")
            or any(c in value for c in "\x00\r\n")):
        raise ValueError(label + " must be an absolute path")
    return value


def read_policy(state_dir):
    """Only the caller's already-validated external state directory is read."""
    path = Path(state_dir) / POLICY
    if path.is_symlink():
        raise ValueError("execution policy must be a coordinator file, not a symlink")
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return {}, None
    if len(raw) > 65536:
        raise ValueError("execution policy is oversized")
    policy = json.loads(raw)
    if (not isinstance(policy, dict) or type(policy.get("schema_version")) is not int
            or policy["schema_version"] != 1
            or set(policy) - {"schema_version", "local", "verification_host"}):
        raise ValueError("invalid execution policy schema")
    local = policy.get("local", {})
    if not isinstance(local, dict) or set(local) - {"python", "git"}:
        raise ValueError("invalid local executable declaration")
    for key, value in local.items():
        absolute(value, "local " + key)
    remote = policy.get("verification_host")
    if remote is not None:
        required = {"ssh_alias", "executor", "workdir_root", "python", "git"}
        if (not isinstance(remote, dict) or not required.issubset(remote)
                or set(remote) - required - {"slurm"}):
            raise ValueError("invalid remote execution declaration")
        if not isinstance(remote["ssh_alias"], str) or not re.fullmatch(
                r"[A-Za-z0-9][A-Za-z0-9_.@-]*", remote["ssh_alias"]):
            raise ValueError("invalid ssh alias")
        for key in ("python", "git", "workdir_root"):
            absolute(remote[key], "remote " + key)
        if remote["executor"] not in ("direct", "slurm"):
            raise ValueError("remote executor must be direct or slurm")
        if remote["executor"] == "slurm":
            raise ValueError("executor: slurm is disabled; enablement is tracked in ARC-1103")
        if "slurm" in remote:
            raise ValueError("direct executor cannot declare slurm options")
    return policy, hashlib.sha256(raw).hexdigest()


def resolve_executables(declaration=None, names=("python", "git")):
    """Resolve once outside candidate cwd; Git defaults deliberately ignore PATH."""
    declaration = declaration or {}
    paths = {"python": declaration.get("python", sys.executable),
             "git": declaration.get("git") or shutil.which("git", path=os.defpath)}
    result = {}
    for name in names:
        path = paths[name]
        absolute(path, name)
        path = os.path.realpath(path)
        run = subprocess.run([path, "--version"], stdin=subprocess.DEVNULL,
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                             env=CE.child_env(), cwd=os.path.dirname(__file__),
                             text=True, timeout=30, check=True)
        version = (run.stdout or run.stderr).strip()
        if not version or len(version) > 1024:
            raise ValueError("invalid " + name + " version response")
        result[name] = {"path": path, "version": version}
    return result


class GitRunner:
    def __init__(self, runner, executables):
        self.runner = runner
        self.git_program = executables["git"]["path"]

    def __call__(self, argv, **kwargs):
        if argv and argv[0] == "git":
            argv = [self.git_program] + list(argv[1:])
        return self.runner(argv, **kwargs)


def execution_problem(receipt):
    """Legacy local records remain admissible; declared remote passes need proof."""
    execution = receipt.get("execution")
    if execution is None:
        return "missing execution evidence" if receipt.get("schema_version") == 2 else None
    if not isinstance(execution, dict):
        return "invalid execution evidence"
    if execution.get("location") not in ("local", "remote"):
        return "invalid execution location"
    if receipt.get("result") != "pass":
        return None
    if not isinstance(execution.get("executables"), dict):
        return "missing declared executable evidence"
    if type(receipt.get("exit_code")) is not int or receipt["exit_code"] != 0:
        return "passing execution evidence lacks a zero verifier exit"
    for name in ("python", "git"):
        item = execution["executables"].get(name, {})
        if (not isinstance(item, dict) or not isinstance(item.get("path"), str)
                or not item["path"].startswith("/") or not item.get("version")):
            return "missing declared executable evidence"
    if not execution.get("host_identity"):
        return "missing verification host identity"
    if execution["location"] == "remote":
        if (execution.get("verified_tree") != receipt.get("candidate_tree")
                or not execution.get("verified_tree")):
            return "remote candidate tree digest was not verified"
        if execution.get("executor") not in ("direct", "slurm"):
            return "invalid remote executor evidence"
        if execution["executor"] == "slurm" and (
                not execution.get("job_id") or execution.get("sacct_state") != "COMPLETED"
                or execution.get("sacct_exit_code") != "0:0"):
            return "remote Slurm verification lacks terminal success"
    return None


def local_execution(executables):
    return {"location": "local", "executor": "direct",
            "host_identity": platform.node(), "executables": executables}


def incomplete(reason):
    return {"exit_code": None, "stdout": "", "stderr": "",
            "incomplete_reason": str(reason)[-2000:] or "remote execution incomplete"}


def _command(argv, cwd=None, timeout=60):
    run = subprocess.run(argv, cwd=cwd, env=CE.child_env(), stdin=subprocess.DEVNULL,
                         capture_output=True, text=True, timeout=timeout)
    return run.returncode, run.stdout, run.stderr


def _git(runner, tree, *args):
    import verify as V
    rc, out, err = V._isolated_git(runner, tree, *args, timeout=300)
    if rc:
        raise ValueError("remote/bundle Git operation failed: " + err[-2000:])
    return out.strip()


def make_bundle(runner, tree, basis, destination):
    candidate = _git(runner, tree, "-c", "user.name=verification", "-c",
                     "user.email=verification@invalid", "commit-tree", basis["candidate_tree"],
                     "-p", basis["target_commit"], "-p", basis["produced_head"],
                     "-m", "Disposable verification candidate")
    _git(runner, tree, "update-ref", BUNDLE_REF, candidate)
    _git(runner, tree, "bundle", "create", str(destination), BUNDLE_REF)


def encoded(record):
    return json.dumps(record, sort_keys=True, separators=(",", ":")).encode("utf-8")


def record_digest(record):
    return hashlib.sha256(encoded(record)).hexdigest()


def publish(path, record, once=False):
    """Fsync bytes, atomically publish, then fsync the directory.

    Hard-link publication never replaces an existing completion receipt. A
    repeated identical write is idempotent; different bytes are a conflict.
    """
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    data = encoded(record)
    try:
        with temporary.open("xb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        if once:
            try:
                os.link(str(temporary), str(path))
            except FileExistsError:
                if path.read_bytes() != data:
                    raise ValueError("conflicting write-once evidence at " + str(path))
        else:
            os.replace(str(temporary), str(path))
        fd = os.open(str(path.parent), os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    finally:
        if temporary.exists():
            temporary.unlink()


def ledger_path(state_dir, unit, basis):
    return Path(state_dir) / "remote-verifications" / (record_digest(
        {"unit": unit, "basis": basis}) + ".json")


def load_ledger(path, unit, basis):
    if not path.exists():
        return {"unit": unit, "basis": basis, "runs": []}
    value = json.loads(path.read_text())
    if (not isinstance(value, dict) or value.get("unit") != unit
            or value.get("basis") != basis or not isinstance(value.get("runs"), list)):
        raise ValueError("invalid remote evidence ledger at " + str(path))
    return value


def unresolved(run):
    return not run.get("reconciled") or not run.get("published")


def unresolved_message(run):
    return "unresolved remote evidence at {}:{}; retrieve or resolve it first".format(
        run["verification_host"]["ssh_alias"], run["stage"])


def pending_problem(state_dir, unit, basis):
    if state_dir is None:
        return None
    path = ledger_path(state_dir, unit, basis)
    ledger = load_ledger(path, unit, basis)
    for run in ledger["runs"]:
        if unresolved(run):
            return unresolved_message(run)
    return None


@contextmanager
def binding_lock(state_dir, unit, basis):
    path = ledger_path(state_dir, unit, basis)
    path.parent.mkdir(exist_ok=True)
    with path.with_suffix(".lock").open("a") as lock:
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError("verification for this binding is already in progress; retrieve evidence later")
        yield path


def publication_digest(run):
    return record_digest({"receipts": run["receipts"], "final": run.get("final")})


def acknowledge(state_dir, unit, basis, launch_id, digest):
    """Called only after the operator's observation fence and receipt fsync."""
    with binding_lock(state_dir, unit, basis) as path:
        ledger = load_ledger(path, unit, basis)
        for run in ledger["runs"]:
            if run["launch_id"] == launch_id:
                if publication_digest(run) != digest:
                    raise ValueError("remote evidence changed before publication acknowledgment; retrieve it again")
                run["published"] = True
                publish(path, ledger)
                return
        raise ValueError("unknown remote evidence launch")


def collect(stage):
    """Read immutable per-claim evidence even without a worker/supervisor result."""
    request = json.loads((stage / "request.json").read_text())
    result = {"basis": request["basis"], "launch_id": request["launch_id"],
              "receipts": {}, "final": None, "supervision": None}
    for index in range(len(request["checks"])):
        try:
            result["receipts"][str(index)] = json.loads(
                (stage / ("claim-{}.json".format(index))).read_text())
        except FileNotFoundError:
            pass
        except (OSError, ValueError) as exc:
            result.setdefault("errors", []).append(str(exc))
    for field, name in (("final", "worker-complete"), ("supervision", "supervision-finished")):
        try:
            result[field] = json.loads((stage / name).read_text())
        except FileNotFoundError:
            pass
        except (OSError, ValueError) as exc:
            result.setdefault("errors", []).append(str(exc))
    try:
        result["lifecycle"] = json.loads((stage / "cleanup.json").read_text())
    except (OSError, ValueError):
        pass
    return result


def evidence_digest(response):
    # Cleanup diagnostics can evolve without changing the immutable evidence.
    return record_digest({k: v for k, v in response.items() if k != "lifecycle"})


def completion_receipt(request, check, outcome, execution):
    return {"launch_id": request["launch_id"], "basis": request["basis"],
            "claim_binding": check["binding"], "outcome": outcome,
            "completion": {"verifier_returned": True, "exit_code": outcome["exit_code"],
                           "pinned_sha256": check["digest"]},
            "execution": dict(execution)}


def worker(stage):
    """Runs inside the allocation for Slurm, including executable/version probes."""
    import verify as V
    request = json.loads((stage / "request.json").read_text())
    remote, basis = request["verification_host"], request["basis"]
    try:
        (stage / "worker-started").mkdir()
    except FileExistsError:
        # A site can force requeue despite --no-requeue. Never overwrite a
        # previous worker's completed FAIL or partially collected evidence.
        return 0
    receipts = {}
    result = {"basis": basis, "outcomes": [], "execution": {
        "location": "remote", "executor": remote["executor"],
        "ssh_alias": remote["ssh_alias"], "host_identity": platform.node()}}
    try:
        executables = resolve_executables(remote)
        result["execution"]["executables"] = executables
        runner = GitRunner(_command, executables)
        tree = stage / "tree"
        tree.mkdir()
        init = ["init", "--quiet", "--template="]
        if len(basis["candidate_tree"]) == 64:
            init.append("--object-format=sha256")
        _git(runner, tree, *init)
        _git(runner, tree, "fetch", "--no-tags", str(stage / "candidate.bundle"), BUNDLE_REF)
        _git(runner, tree, "-c", "core.hooksPath=/dev/null", "checkout", "--detach", "FETCH_HEAD")
        # Hash the checked-out bytes into a fresh index, not just HEAD metadata.
        _git(runner, tree, "add", "--all")
        actual_tree = _git(runner, tree, "write-tree")
        if actual_tree != basis["candidate_tree"]:
            raise ValueError("remote candidate tree digest mismatch")
        result["execution"]["verified_tree"] = basis["candidate_tree"]
        for index, check in enumerate(request["checks"]):
            path = stage / ("pinned-{}.py".format(index))
            path.write_bytes(base64.b64decode(check["program"], validate=True))
            outcome, error = V.run_pinned(
                runner, path, check["digest"], args=check["args"],
                timeout=request["timeout"], cwd=str(tree), observe_completion=True,
                executables=executables)
            result["outcomes"].append(outcome if not error else incomplete(error))
            if not error and not outcome.get("incomplete_reason"):
                receipt = completion_receipt(request, check, outcome, result["execution"])
                publish(stage / ("claim-{}.json".format(index)), receipt, once=True)
                receipts[str(index)] = record_digest(receipt)
            publish(stage / "result.json", result)
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        result["error"] = str(exc)
    while len(result["outcomes"]) < len(request["checks"]):
        result["outcomes"].append(incomplete(result.get("error", "worker did not complete")))
    publish(stage / "result.json", result)
    publish(stage / "worker-complete", {
        "launch_id": request["launch_id"], "basis": basis, "receipts": receipts,
        "outcomes": result["outcomes"], "execution": result["execution"]}, once=True)
    return 0


def scheduler_state(job):
    try:
        rc, out, err = _command(["sacct", "-n", "-P", "-j", job,
                                  "--format=JobIDRaw,State%40,ExitCode"])
    except (OSError, subprocess.SubprocessError):
        return None
    if rc:
        return None
    matches = [line.split("|") for line in out.splitlines()
               if line.split("|", 1)[0] == job]
    if len(matches) != 1 or len(matches[0]) < 3:
        return None
    _, state, code = matches[0][:3]
    # Slurm CANCELLED can carry ' by uid'; this is state parsing, never identity.
    state = state.split(" ", 1)[0].rstrip("+")
    return (state, code) if state in TERMINAL else None


def output_tail(path, limit):
    try:
        with path.open("rb") as handle:
            handle.seek(0, os.SEEK_END)
            handle.seek(max(0, handle.tell() - limit))
            return handle.read(limit).decode("utf-8", "replace")
    except OSError:
        return ""


def supervise(stage):
    request = json.loads((stage / "request.json").read_text())
    remote = request["verification_host"]
    deadline = time.monotonic() + request["timeout"] * len(request["checks"])
    job = None
    state = None
    if remote["executor"] == "direct":
        worker(stage)
    else:
        cfg = remote["slurm"]
        script = stage / "job.sh"
        script.write_text("#!/bin/sh\nexec " + shlex.join([
            remote["python"], str(stage / "remote_verify.py"),
            "--worker", str(stage)]) + "\n")
        rc, out, err = _command([
            "sbatch", "--parsable", "--no-requeue", "--partition=" + cfg["partition"],
            "--mem=" + cfg["mem"], "--time=" + cfg["time"],
            "--output=" + str(stage / "job.out"), "--error=" + str(stage / "job.err"),
            str(script)])
        if rc or not re.fullmatch(r"[0-9]+(?:;[A-Za-z0-9_.-]+)?\n?", out):
            raise ValueError("Slurm submission unavailable: " + err[-1000:])
        job = out.strip().split(";")[0]
        (stage / "job-id").write_text(job)
        while True:
            state = scheduler_state(job)
            if state or time.monotonic() >= deadline:
                break
            time.sleep(min(2, max(0, deadline - time.monotonic())))
    try:
        result = json.loads((stage / "result.json").read_text())
    except (OSError, ValueError):
        result = {"basis": request["basis"], "outcomes": [], "execution": {
            "location": "remote", "ssh_alias": remote["ssh_alias"],
            "executor": remote["executor"]}, "error": "remote worker did not complete"}
    if job:
        result["execution"].update(
            scheduler_stdout_tail=output_tail(stage / "job.out", 4000),
            scheduler_stderr_tail=output_tail(stage / "job.err", 2000),
            job_id=job, sacct_state=state[0] if state else None,
                                   sacct_exit_code=state[1] if state else None)
        if state != ("COMPLETED", "0:0"):
            result["error"] = "Slurm verification did not reach terminal success"
        # Scheduler status is transport/lifecycle evidence, never a verifier
        # result. Independently completed claims are collected below.
    return result


# The fixed bootstrap reads only this allowlist, never tar-supplied paths/modes.
# It executes the coordinator harness outside the transferred candidate tree.
BOOTSTRAP = """import os, pathlib, sys, tarfile
stage = pathlib.Path(sys.argv[1])
stage.mkdir(parents=False, exist_ok=False)
with tarfile.open(fileobj=sys.stdin.buffer, mode='r|') as archive:
    seen = set()
    for member in archive:
        if member.name not in %r or member.name in seen or not member.isfile():
            raise SystemExit('invalid verification transfer')
        seen.add(member.name)
        with archive.extractfile(member) as source, (stage / member.name).open('wb') as dest:
            import shutil
            shutil.copyfileobj(source, dest)
    if seen != set(%r): raise SystemExit('incomplete verification transfer')
os.execv(sys.executable, [sys.executable, str(stage / 'remote_verify.py'), '--supervise', str(stage)])
""" % (FILES, FILES)


def coordinator_ssh(repo, tree):
    """Keep operator PATH wrappers, excluding both operated Git trees."""
    roots = [os.path.realpath(str(path)) for path in (repo, tree)]

    def excluded(path):
        return any(os.path.commonpath([root, candidate]) == root
                   for root in roots
                   for candidate in (os.path.abspath(path), os.path.realpath(path)))

    for directory in os.get_exec_path():
        if not os.path.isabs(directory) or excluded(directory):
            continue
        program = shutil.which("ssh", path=directory)
        if program and not excluded(program):
            return os.path.realpath(program)
    return None


def cancellation_tail(text):
    # Diagnostic rendering only, after the unmodified status decides the result.
    return text.encode("utf-8", "replace")[-1000:].decode("utf-8", "replace")


def publish_cleanup(path, evidence):
    if len(json.dumps(evidence).encode("utf-8")) > 65536:
        raise ValueError("cancellation history exceeds 64 KiB; stage retained")
    publish(path, evidence)


def cleanup(stage, reconciled=None):
    """Serialize recovery; preserve evidence until supervision and job are done.

    Four attempts and 64 KiB bound the journal. An intent is published before
    scancel so a lost process leaves an unknown outcome, never invented success.
    A zero scancel status confirms a request; only terminal accounting allows
    removal. Unknown supervision, journal failure or exhaustion retains the stage.
    """
    evidence = {"cleanup": "unconfirmed", "stage": str(stage)}
    lock = None
    try:
        if not stage.exists():
            return dict(evidence, cleanup="removed" if reconciled else "unconfirmed")
        try:
            job = (stage / "job-id").read_text()
        except FileNotFoundError:
            job = None
        if job and re.fullmatch(r"[0-9]+", job):
            evidence["job_id"] = job
        else:
            job = None
        if not (stage / "supervision-finished").is_file():
            evidence["cleanup_error"] = "supervision may still submit or publish a job ID"
            return evidence
        lock = (stage / "cleanup.lock").open("a")
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        finished = json.loads((stage / "supervision-finished").read_text())
        if not isinstance(finished, dict):
            raise ValueError("invalid supervision marker; stage retained")
        marker_job = finished.get("job_id")
        if marker_job is not None:
            if not isinstance(marker_job, str) or not re.fullmatch(r"[0-9]+", marker_job):
                raise ValueError("invalid supervision job identity; stage retained")
            if job and marker_job != job:
                raise ValueError("conflicting job identities; stage retained")
            job = marker_job
            evidence["job_id"] = job
        history = stage / "cleanup.json"
        if history.exists():
            with history.open("rb") as handle:
                raw = handle.read(65537)
            if len(raw) > 65536:
                raise ValueError("cancellation history exceeds 64 KiB; stage retained")
            previous = json.loads(raw)
            if not isinstance(previous, dict):
                raise ValueError("invalid cancellation history; stage retained")
            attempts = previous.get("cancellation_attempts", [])
            if (previous.get("job_id") != job or not isinstance(attempts, list)
                    or len(attempts) > 4 or any(not isinstance(a, dict) for a in attempts)):
                raise ValueError("invalid cancellation history; stage retained")
            evidence["cancellation_attempts"] = attempts
        if job:
            state = None
            if (finished.get("job_id") == job
                    and finished.get("sacct_state") in TERMINAL):
                state = (finished["sacct_state"], finished.get("sacct_exit_code"))
            state = state or scheduler_state(job)
            if state is None:
                attempts = evidence.setdefault("cancellation_attempts", [])
                if len(attempts) >= 4:
                    raise ValueError("cancellation attempt limit reached; stage retained")
                attempt = {"job_id": job, "exit_code": None}
                attempts.append(attempt)
                evidence["cancellation"] = "unconfirmed"
                publish_cleanup(history, evidence)
                try:
                    rc, out, err = _command(["scancel", job], timeout=30)
                    attempt.update(exit_code=rc, stdout_tail=cancellation_tail(out),
                                   stderr_tail=cancellation_tail(err))
                except (OSError, subprocess.SubprocessError) as exc:
                    attempt["error"] = cancellation_tail(str(exc))
                evidence["cancellation"] = ("requested" if attempt["exit_code"] == 0
                                            else "unconfirmed")
                publish_cleanup(history, evidence)
                if evidence["cancellation"] == "unconfirmed":
                    return evidence
                state = scheduler_state(job)
                if state is None:
                    evidence["cleanup_error"] = "cancellation requested; job termination unconfirmed"
                    return evidence
            else:
                evidence["cancellation"] = "not-required"
            evidence["cleanup_sacct_state"] = state[0]
        collected = collect(stage)
        request = json.loads((stage / "request.json").read_text())
        if request["verification_host"]["executor"] == "slurm" and not job:
            raise ValueError("Slurm job identity unresolved; stage retained")
        if (not reconciled or reconciled != evidence_digest(collected)
                or not collected.get("final") or not collected.get("supervision")):
            raise ValueError("remote evidence is not quiescent and reconciled; stage retained")
        shutil.rmtree(stage, ignore_errors=False)
        evidence["cleanup"] = "removed"
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        evidence["cleanup_error"] = str(exc)[-2000:]
    finally:
        if lock is not None:
            lock.close()
    return evidence


def validate_receipt(receipt, request, index):
    check = request["checks"][index]
    if (not isinstance(receipt, dict) or receipt.get("launch_id") != request["launch_id"]
            or receipt.get("basis") != request["basis"]
            or receipt.get("claim_binding") != check["binding"]):
        raise ValueError("remote claim receipt binding mismatch")
    outcome = receipt.get("outcome")
    if (not isinstance(outcome, dict) or type(outcome.get("exit_code")) is not int
            or outcome.get("incomplete_reason")
            or not isinstance(outcome.get("stdout"), str) or len(outcome["stdout"]) > 4000
            or not isinstance(outcome.get("stderr"), str) or len(outcome["stderr"]) > 2000
            or receipt.get("completion") != {"verifier_returned": True,
                 "exit_code": outcome["exit_code"], "pinned_sha256": check["digest"]}):
        raise ValueError("invalid remote completion handshake")
    execution = receipt.get("execution", {})
    remote = request["verification_host"]
    if (execution.get("ssh_alias") != remote["ssh_alias"]
            or execution.get("executor") != remote["executor"]):
        raise ValueError("remote execution identity mismatch")
    # Completion is independent of scheduler/transport status, including PASS.
    # Validate the execution facts without requiring Slurm terminal success.
    checked = dict(execution, executor="direct")
    problem = execution_problem(dict(candidate_tree=request["basis"]["candidate_tree"],
                                     execution=checked, result="pass", exit_code=0))
    if problem:
        raise ValueError(problem)


def ingest(run, response):
    request = run["request"]
    if (not isinstance(response, dict) or response.get("basis") != request["basis"]
            or response.get("launch_id") != run["launch_id"]
            or not isinstance(response.get("receipts"), dict)):
        raise ValueError("remote response binding mismatch")
    problems = list(response.get("errors", []))
    for index in range(len(request["checks"])):
        key = str(index)
        if key not in response["receipts"]:
            continue
        try:
            receipt = response["receipts"][key]
            validate_receipt(receipt, request, index)
            previous = run["receipts"].get(key)
            if previous is not None and previous != receipt:
                raise ValueError("conflicting completed remote receipt")
            if previous is None:
                run["published"] = False
            run["receipts"][key] = receipt
        except (ValueError, TypeError, AttributeError) as exc:
            problems.append(str(exc))
    final, supervision = response.get("final"), response.get("supervision")
    if final is not None:
        digests = {key: record_digest(value) for key, value in run["receipts"].items()}
        if (not isinstance(final, dict) or final.get("launch_id") != run["launch_id"]
                or final.get("basis") != request["basis"] or final.get("receipts") != digests
                or not isinstance(final.get("outcomes"), list)
                or len(final["outcomes"]) != len(request["checks"])):
            problems.append("invalid remote final completion marker")
        else:
            for index, outcome in enumerate(final["outcomes"]):
                receipt = run["receipts"].get(str(index))
                if receipt is not None:
                    if outcome != receipt["outcome"]:
                        problems.append("final outcome conflicts with completed receipt")
                elif not isinstance(outcome, dict) or not outcome.get("incomplete_reason"):
                    problems.append("missing completed remote claim receipt")
            if run.get("final") is not None and run["final"] != final:
                problems.append("conflicting remote final completion marker")
            elif not problems:
                if run.get("final") is None:
                    run["published"] = False
                run["final"] = final
    run["reconciled"] = bool(not problems and run.get("final") and
        isinstance(supervision, dict) and supervision.get("launch_id") == run["launch_id"])
    if request["verification_host"]["executor"] == "slurm":
        run["reconciled"] = bool(run["reconciled"] and supervision.get("job_id")
                                 and supervision.get("sacct_state") in TERMINAL)
    if isinstance(supervision, dict):
        for key in ("job_id", "sacct_state", "sacct_exit_code"):
            if key in supervision:
                run["execution"][key] = supervision[key]
    lifecycle = response.get("lifecycle")
    if isinstance(lifecycle, dict) and lifecycle.get("stage") == run["stage"]:
        run["execution"].update(lifecycle)
    if run["reconciled"]:
        run["ack"] = evidence_digest(response)
    if problems:
        run["error"] = "; ".join(problems)
    return run["reconciled"]


def remote_call(prefix, remote, code, timeout=45):
    return subprocess.run(prefix + [shlex.join([remote["python"], "-c", code])],
                          stdin=subprocess.DEVNULL, capture_output=True, text=True,
                          env=CE.child_env(), timeout=timeout)


def run_remote(runner, tree, basis, checks, remote, timeout, repo, state_dir, unit,
               retrieve_only=False):
    """Persist intent before launch and ingest completed receipts monotonically.

    A pending invocation only retrieves its original stage, even if the caller
    changes the execution host. The coordinator ledger survives publication
    fence failures and crashes. It is separate from agent-writable audit files.
    """
    if state_dir is None:
        raise ValueError("remote verification requires coordinator evidence storage")
    with binding_lock(state_dir, unit, basis) as path:
        ledger = load_ledger(path, unit, basis)
        pending = [run for run in ledger["runs"] if unresolved(run)]
        if retrieve_only and not pending:
            if not ledger["runs"]:
                raise ValueError("no remote launch exists for this binding to retrieve")
            pending = ledger["runs"][-1:]
        retry = bool(pending)
        if pending:
            run = pending[0]
            remote = run["verification_host"]
            if run["request"]["checks"] != checks:
                # Paths are temporary; compare the content-bound declarations.
                old = [{k: v for k, v in c.items() if k != "path"}
                       for c in run["request"]["checks"]]
                new = [{k: v for k, v in c.items() if k != "path"} for c in checks]
                if old != new:
                    raise ValueError(unresolved_message(run) + "; claim binding changed")
        else:
            if remote is None:
                return None, None
            launch_id = uuid.uuid4().hex
            stage = remote["workdir_root"].rstrip("/") + "/verify-" + launch_id
            request = {"basis": basis, "checks": checks, "verification_host": remote,
                       "timeout": timeout, "launch_id": launch_id}
            run = {"launch_id": launch_id, "stage": stage, "verification_host": remote,
                   "request": request, "receipts": {}, "reconciled": False, "published": False}
            ledger["runs"].append(run)
        stage = run["stage"]
        ssh = coordinator_ssh(repo, tree)
        execution = run.setdefault("execution", {
            "location": "remote", "executor": remote["executor"],
            "ssh_alias": remote["ssh_alias"], "host_identity": None,
            "stage": stage, "job_id": None, "ssh_program": ssh,
            "launch_id": run["launch_id"], "cleanup": "unconfirmed",
            "executables": {key: {"path": remote[key], "version": None}
                            for key in ("python", "git")}})
        prefix = [ssh, "-oBatchMode=yes", "-oConnectTimeout=15", remote["ssh_alias"]] if ssh else None
        error = None

        def receive(proc):
            # Parse a complete payload before interpreting SSH's own status.
            nonlocal error
            if proc.returncode:
                error = "ssh/remote transport exited {}: {}".format(proc.returncode, proc.stderr[-2000:])
            if len(proc.stdout) > 100000:
                raise ValueError("oversized remote response")
            response = json.loads(proc.stdout)
            ingest(run, response)
            publish(path, ledger)

        if not retry:
            # Bundle/transfer construction is pre-launch and can safely fail.
            with tempfile.TemporaryDirectory(prefix="verification-transfer-") as tmp:
                tmp = Path(tmp)
                make_bundle(runner, tree, basis, tmp / "candidate.bundle")
                (tmp / "request.json").write_text(json.dumps(run["request"]))
                for name in FILES[2:]:
                    shutil.copyfile(Path(__file__).with_name(name), tmp / name)
                with tempfile.TemporaryFile() as transfer:
                    with tarfile.open(fileobj=transfer, mode="w") as archive:
                        for name in FILES:
                            archive.add(str(tmp / name), arcname=name, recursive=False)
                    transfer.seek(0)
                    publish(path, ledger)  # MUST precede the first possible remote launch.
                    try:
                        if not prefix:
                            raise ValueError("ssh is unavailable")
                        command = shlex.join([remote["python"], "-c", BOOTSTRAP, stage])
                        proc = subprocess.run(prefix + [command], stdin=transfer,
                            capture_output=True, text=True, env=CE.child_env(),
                            timeout=timeout * len(checks) + 120)
                        receive(proc)
                    except (OSError, ValueError, subprocess.SubprocessError) as exc:
                        error = "remote transport incomplete: " + str(exc)
        # Lost stdout is recoverable without executing another verifier.
        if not run["reconciled"] and prefix:
            code = ("import json, pathlib, sys; p=pathlib.Path(%r); "
                    "sys.path.insert(0, str(p)); from remote_verify import collect; "
                    "print(json.dumps(collect(p)))") % stage
            try:
                receive(remote_call(prefix, remote, code))
            except (OSError, ValueError, subprocess.SubprocessError) as exc:
                error = "remote retrieval incomplete: " + str(exc)
        if error:
            run["transport_error"] = error
        # Raw evidence is durable before authorizing remote removal. This is
        # reconciliation, not publication under the operator observation fence.
        publish(path, ledger)
        if execution.get("cleanup") != "removed" and prefix:
            code = ("import json, pathlib, sys; p=pathlib.Path(%r); "
                    "sys.path.insert(0, str(p)); from remote_verify import cleanup; "
                    "print(json.dumps(cleanup(p, %r)))") % (stage, run.get("ack") if run["reconciled"] else None)
            if run["reconciled"]:
                code = ("import json, pathlib; p=pathlib.Path(%r)\n"
                        "if not p.exists(): print(json.dumps({'stage': str(p), 'cleanup': 'removed'}))\n"
                        "else:\n    exec(%r)\n") % (stage, code)
            try:
                proc = remote_call(prefix, remote, code)
                if proc.returncode:
                    raise ValueError("cleanup transport exited {}: {}".format(proc.returncode, proc.stderr[-2000:]))
                cleaned = json.loads(proc.stdout)
                if (not isinstance(cleaned, dict) or cleaned.get("stage") != stage
                        or cleaned.get("cleanup") not in ("removed", "unconfirmed")):
                    raise ValueError("invalid remote cleanup response")
                execution.update(cleaned)
            except (OSError, ValueError, subprocess.SubprocessError) as exc:
                execution["cleanup_error"] = str(exc)[-2000:]
        outcomes = []
        final = run.get("final")
        for index in range(len(checks)):
            receipt = run["receipts"].get(str(index))
            if receipt:
                outcomes.append(dict(receipt["outcome"]))
                execution.update(receipt["execution"])
            else:
                reason = (final["outcomes"][index].get("incomplete_reason") if final else None)
                outcomes.append(incomplete(reason or run.get("error") or error or "missing remote completion"))
        if final:
            # Failure before any claim still has truthful tree/probe diagnostics.
            execution.update(final.get("execution", {}))
        execution["evidence_reconciled"] = run["reconciled"]
        execution["publication_digest"] = publication_digest(run)
        if not run["reconciled"]:
            print("WARNING: " + unresolved_message(run), file=sys.stderr)
        if execution.get("cleanup") != "removed":
            if remote["executor"] == "slurm":
                execution.setdefault("cancellation", "unconfirmed")
            print("WARNING: remote cleanup unconfirmed for job {}; recovery files {}/job-id "
                  "and {}/cleanup.json; inspect cancellation and retain the stage".format(
                      execution.get("job_id") or "unknown", stage, stage), file=sys.stderr)
        publish(path, ledger)
        return outcomes, dict(execution)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--worker")
    group.add_argument("--supervise")
    args = parser.parse_args()
    stage = Path(args.worker or args.supervise)
    if args.worker:
        return worker(stage)
    request = json.loads((stage / "request.json").read_text())
    result = {}
    try:
        result = supervise(stage)
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        result = {"error": str(exc)}
    execution = result.get("execution", {})
    publish(stage / "supervision-finished", dict(
        {key: execution.get(key) for key in ("job_id", "sacct_state", "sacct_exit_code")},
        launch_id=request["launch_id"]), once=True)
    if request["verification_host"]["executor"] == "slurm":
        # Disabled executor retains cancellation support. No acknowledgment is
        # available yet, so cleanup may cancel but cannot remove the evidence.
        cleanup(stage)
    # Direct stages await durable coordinator reconciliation before cleanup.
    print(json.dumps(collect(stage)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
