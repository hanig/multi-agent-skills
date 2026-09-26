#!/usr/bin/env python3
"""Coordinator-owned execution policy and disposable SSH/Slurm verification.

Transport carries a local candidate Git bundle and coordinator harness bytes,
never candidate-selected programs. Same-UID writers and hostile tests are not
an OS isolation boundary. No forge writes or credential provisioning occur.
"""
import argparse
import base64
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
            cfg = remote.get("slurm")
            if not isinstance(cfg, dict) or set(cfg) != {"partition", "mem", "time"}:
                raise ValueError("slurm requires partition, mem and time")
            for key, value in cfg.items():
                if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_:.-]+", value):
                    raise ValueError("invalid slurm " + key)
        elif "slurm" in remote:
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


def publish(path, record):
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(record), encoding="utf-8")
    os.replace(str(temporary), str(path))


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
            # Preserve a completed failure if a subsequent verifier/job is lost.
            publish(stage / "result.json", result)
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        result["error"] = str(exc)
    publish(stage / "result.json", result)
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
            reason = "Slurm verification did not reach terminal success"
            result["error"] = reason
            result["outcomes"] = [o if not o.get("incomplete_reason")
                                  and o.get("exit_code") not in (None, 0)
                                  else incomplete(reason) for o in result["outcomes"]]
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


def cleanup(stage):
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
            return dict(evidence, cleanup="removed")
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
        shutil.rmtree(stage, ignore_errors=False)
        evidence["cleanup"] = "removed"
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        evidence["cleanup_error"] = str(exc)[-2000:]
    finally:
        if lock is not None:
            lock.close()
    return evidence


def run_remote(runner, tree, basis, checks, remote, timeout, repo):
    """Transport errors are incomplete, never a verifier's completed FAIL."""
    stage = remote["workdir_root"].rstrip("/") + "/verify-" + uuid.uuid4().hex
    ssh = coordinator_ssh(repo, tree)
    execution = {"location": "remote", "executor": remote["executor"],
                 "ssh_alias": remote["ssh_alias"], "host_identity": None,
                 "stage": stage, "job_id": None, "ssh_program": ssh,
                 "executables": {key: {"path": remote[key], "version": None}
                                 for key in ("python", "git")}}
    result = None
    error = None
    cleanup_error = None
    if not ssh:
        return [incomplete("ssh is unavailable") for _ in checks], execution
    prefix = [ssh, "-oBatchMode=yes", "-oConnectTimeout=15", remote["ssh_alias"]]
    command = shlex.join([remote["python"], "-c", BOOTSTRAP, stage])
    with tempfile.TemporaryDirectory(prefix="verification-transfer-") as tmp:
        tmp = Path(tmp)
        make_bundle(runner, tree, basis, tmp / "candidate.bundle")
        request = {"basis": basis, "checks": checks, "verification_host": remote, "timeout": timeout}
        (tmp / "request.json").write_text(json.dumps(request))
        for name in FILES[2:]:
            shutil.copyfile(Path(__file__).with_name(name), tmp / name)
        with tempfile.TemporaryFile() as transfer:
            with tarfile.open(fileobj=transfer, mode="w") as archive:
                for name in FILES:
                    archive.add(str(tmp / name), arcname=name, recursive=False)
            transfer.seek(0)
            try:
                proc = subprocess.run(prefix + [command], stdin=transfer,
                                      capture_output=True, text=True, env=CE.child_env(),
                                      timeout=timeout * len(checks) + 120)
                if proc.returncode:
                    raise ValueError("ssh/remote transport exited {}: {}".format(
                        proc.returncode, proc.stderr[-2000:]))
                if len(proc.stdout) > 100000:
                    raise ValueError("oversized remote response")
                result = json.loads(proc.stdout)
                if not isinstance(result, dict) or result.get("basis") != basis:
                    raise ValueError("remote response binding mismatch")
                observed = result.get("execution")
                if observed is not None:
                    if (not isinstance(observed, dict)
                            or observed.get("location") != "remote"
                            or observed.get("executor") != remote["executor"]
                            or observed.get("ssh_alias") != remote["ssh_alias"]):
                        raise ValueError("remote execution identity mismatch")
                    execution.update(observed)
            except (OSError, ValueError, subprocess.SubprocessError) as exc:
                error = "remote transport incomplete: " + str(exc)
            finally:
                # Confirmed cleanup is monotonic: an unnecessary second
                # connection cannot invalidate evidence already received.
                if error or execution.get("cleanup") != "removed":
                    # A second connection covers interrupted bootstrap/transport.
                    # Normal supervision has already removed the directory.
                    code = ("import json, pathlib, sys; p=pathlib.Path(%r); "
                            "sys.path.insert(0, str(p)); "
                            "from remote_verify import cleanup; "
                            "print(json.dumps(cleanup(p)))") % stage
                    # No harness remains after a successful first cleanup.
                    code = ("import json, pathlib; p=pathlib.Path(%r)\n"
                            "if not p.exists(): print(json.dumps({'cleanup': 'removed'}))\n"
                            "else:\n    exec(%r)\n") % (stage, code)
                    try:
                        proc = subprocess.run(prefix + [shlex.join([remote["python"], "-c", code])],
                                              stdin=subprocess.DEVNULL, capture_output=True,
                                              text=True, env=CE.child_env(), timeout=45)
                        if proc.returncode:
                            raise ValueError("cleanup transport exited {}: {}".format(
                                proc.returncode, proc.stderr[-2000:]))
                        cleaned = json.loads(proc.stdout)
                        if not isinstance(cleaned, dict) or cleaned.get("cleanup") not in (
                                "removed", "unconfirmed"):
                            raise ValueError("invalid remote cleanup response")
                        if (execution.get("job_id") and cleaned.get("job_id")
                                and execution["job_id"] != cleaned["job_id"]):
                            raise ValueError("cleanup job identity mismatch")
                        execution.update(cleaned)
                    except (OSError, ValueError, subprocess.SubprocessError) as exc:
                        execution["cleanup"] = "unconfirmed"
                        execution["cleanup_error"] = str(exc)[-2000:]
    if execution.get("cleanup") != "removed" or execution.get("cancellation") == "unconfirmed":
        cleanup_error = "remote cleanup/cancellation could not be confirmed"
        execution["cleanup"] = "unconfirmed"
        if remote["executor"] == "slurm":
            execution.setdefault("cancellation", "unconfirmed")
        print("WARNING: {} for job {}; inspect and cancel if still active; "
              "recovery files {}/job-id and {}/cleanup.json".format(
                  cleanup_error, execution.get("job_id") or "unknown", stage, stage),
              file=sys.stderr)
    if error:
        return [incomplete(error) for _ in checks], execution
    outcomes = result.get("outcomes", [])
    if not isinstance(outcomes, list) or len(outcomes) > len(checks):
        outcomes = []
        error = "invalid remote outcomes"
    while len(outcomes) < len(checks):
        outcomes.append(incomplete(error or result.get("error") or "missing remote completion"))
    for index, outcome in enumerate(outcomes):
        if (not isinstance(outcome, dict)
                or not isinstance(outcome.get("stdout"), str)
                or not isinstance(outcome.get("stderr"), str)
                or len(outcome["stdout"]) > 4000 or len(outcome["stderr"]) > 2000
                or (type(outcome.get("exit_code")) is not int
                    and not (isinstance(outcome.get("incomplete_reason"), str)
                             and outcome["incomplete_reason"]))):
            outcomes[index] = incomplete("invalid remote completion")
            continue
        problem = execution_problem(dict(candidate_tree=basis["candidate_tree"],
                                          execution=execution, result="pass", exit_code=0))
        if outcome.get("exit_code") == 0 and (problem or cleanup_error):
            outcomes[index] = incomplete(problem or cleanup_error)
    return outcomes, execution


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--worker")
    group.add_argument("--supervise")
    args = parser.parse_args()
    stage = Path(args.worker or args.supervise)
    if args.worker:
        return worker(stage)
    try:
        result = supervise(stage)
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        request = json.loads((stage / "request.json").read_text())
        result = {"basis": request["basis"], "outcomes": [], "error": str(exc)}
    finally:
        # No future submission or job-ID publication by this supervisor is
        # possible. A secondary connection without this marker retains staging.
        execution = result.setdefault("execution", {})
        try:
            publish(stage / "supervision-finished", {
                key: execution.get(key) for key in
                ("job_id", "sacct_state", "sacct_exit_code")})
            execution.update(cleanup(stage))
        except OSError as exc:
            execution.update(cleanup="unconfirmed", stage=str(stage), cleanup_error=str(exc))
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
