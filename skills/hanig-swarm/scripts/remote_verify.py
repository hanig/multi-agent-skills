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
import inspect
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
                if (not isinstance(value, str) or not re.fullmatch(
                        r"[A-Za-z0-9][A-Za-z0-9_.:-]*", value)):
                    raise ValueError("slurm " + key + " must be a plain token")
        elif "slurm" in remote:
            raise ValueError("direct executor cannot declare slurm options")
    return policy, hashlib.sha256(raw).hexdigest()


def resolve_executables(declaration=None, names=("python", "git"), excluded_roots=()):
    """Prefer system Git; allow external operator PATH only when it is absent."""
    declaration = declaration or {}
    paths = {"python": declaration.get("python", sys.executable),
             "git": declaration.get("git") or coordinator_program("git", excluded_roots, os.defpath)
                    or coordinator_program("git", excluded_roots)}
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
        result[name] = {"path": path, "version": version, "declared": name in declaration}
        if name == "python":
            result[name]["role"] = "configured-interpreter" if name in declaration else "launcher"
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


def launch_witness(unit, basis, run):
    return dict(unit=unit, basis=basis, **{key: run[key] for key in (
        "launch_id", "stage", "verification_host", "request")})


def publish_launch_witness(path, unit, basis, run):
    directory = path.with_suffix(".launches")
    directory.mkdir(exist_ok=True)
    publish(directory / (run["launch_id"] + ".json"),
            launch_witness(unit, basis, run), once=True)
    # publish fsyncs the witness directory; persist its own directory entry too.
    fd = os.open(str(directory.parent), os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def load_ledger(path, unit, basis, journal_entries=None):
    """Missing state is fresh only without surviving launch evidence.

    Witnesses and coordinator journal rows are negative evidence of a launch,
    never proof of reconciliation. A restored ledger must account for every
    surviving launch, including launches omitted by an older backup.
    """
    value = (json.loads(path.read_text()) if path.exists()
             else {"unit": unit, "basis": basis, "runs": []})
    if (not isinstance(value, dict) or value.get("unit") != unit
            or value.get("basis") != basis or not isinstance(value.get("runs"), list)):
        raise ValueError("invalid remote evidence ledger at " + str(path))
    runs = {}
    for run in value["runs"]:
        if (not isinstance(run, dict) or not isinstance(run.get("launch_id"), str)
                or not re.fullmatch(r"[0-9a-f]{32}", run["launch_id"])
                or run["launch_id"] in runs):
            raise ValueError("invalid remote launch identity at " + str(path))
        runs[run["launch_id"]] = run
    witnesses = {}
    for marker in path.with_suffix(".launches").glob("*.json"):
        witness = json.loads(marker.read_text())
        if (not isinstance(witness, dict) or witness.get("unit") != unit
                or witness.get("basis") != basis
                or marker.name != str(witness.get("launch_id")) + ".json"):
            raise ValueError("invalid remote launch witness at " + str(marker))
        run = runs.get(witness["launch_id"])
        if run is None or launch_witness(unit, basis, run) != witness:
            raise ValueError(unresolved_message(witness) + "; restore the matching coordinator ledger")
        witnesses[run["launch_id"]] = witness
    for launch_id, run in runs.items():
        if run.get("witness_required") and launch_id not in witnesses:
            raise ValueError(unresolved_message(run) + "; restore the required launch witness")
        if run.get("recovery_authority") is not None:
            recovered(run)
        elif run.get("execution", {}).get("recovery_authority_sha256"):
            raise ValueError("restore the coordinator recovery authority")
    for row in journal_entries or ():
        execution = row.get("execution", {})
        if (row.get("unit") != unit or any(row.get(k) != v for k, v in basis.items())
                or not isinstance(execution, dict) or execution.get("location") != "remote"
                or not execution.get("launch_id")):
            continue
        run = runs.get(execution["launch_id"])
        if (run is None or execution.get("stage") != run.get("stage")
                or execution.get("ssh_alias") != run.get("verification_host", {}).get("ssh_alias")):
            raise ValueError("unresolved remote evidence at {}:{}; retrieve or resolve it first; "
                             "restore the matching coordinator ledger".format(
                                 execution.get("ssh_alias"), execution.get("stage")))
    return value


def unresolved(run):
    return (not run.get("reconciled") or not run.get("published")
            or run.get("execution", {}).get("quiescent") is False)


def revoke_negative_observation(run):
    """Repair mutable flags, never receipts; callers persist under binding_lock."""
    if run.get("execution", {}).get("quiescent") is False:
        run.update(reconciled=False, published=False)
        run.pop("ack", None)
        run["execution"]["evidence_reconciled"] = False
        return True
    return False


def unresolved_message(run):
    return "unresolved remote evidence at {}:{}; retrieve or resolve it first".format(
        run["verification_host"]["ssh_alias"], run["stage"])


def pending_problem(state_dir, unit, basis, journal_entries):
    if state_dir is None:
        return None
    if journal_entries is None:
        return "coordinator verification journal is required for remote evidence checks"
    path = ledger_path(state_dir, unit, basis)
    try:
        ledger = load_ledger(path, unit, basis, journal_entries)
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
        return str(exc)
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


def recovery_binding(run):
    return {"launch_id": run["launch_id"], "stage": run["stage"],
            "request_sha256": record_digest(run["request"]),
            "receipts_sha256": record_digest(run["receipts"])}


def validate_recovery_attestation(attestation, run):
    """Explicit operator testimony, never a probe or inferred liveness result."""
    if (not isinstance(attestation, dict)
            or set(attestation) != {"binding", "supervisor", "jobs", "privileged_requeue"}
            or attestation["binding"] != recovery_binding(run)):
        raise ValueError("recovery attestation does not bind the exact launch and completed receipts")
    for subject in ("supervisor", "jobs", "privileged_requeue"):
        proof = attestation[subject]
        if (not isinstance(proof, dict) or set(proof) != {"status", "evidence"}
                or proof["status"] != "fenced"
                or not isinstance(proof["evidence"], str) or not proof["evidence"].strip()):
            raise ValueError("recovery requires attested fencing of " + subject
                             + "; unreachable or missing evidence is not proof")


def observe_recovery_evidence(run, evidence, source, fresh=False):
    """Keep contrary observations monotonic; cached pre-probe bytes are stale."""
    digest = record_digest(evidence)
    proof = run.get("probe_terminal_success")
    if proof and (fresh or digest != proof["preceding_sources"].get(source)):
        jobs = set(proof["known_job_ids"])
        incoming = list(evidence.get("job_ids", [])) + list(evidence.get("queued_job_ids", []))
        if evidence.get("job_id") is not None:
            incoming.append(evidence["job_id"])
        states = evidence.get("cleanup_sacct_states", {})
        incoming.extend(states)
        jobs.update(j for j in incoming if isinstance(j, str) and re.fullmatch(r"[0-9]+", j))
        proof["known_job_ids"] = sorted(jobs)
        contrary = (jobs != set(proof["observation"]["job_ids"])
                    or bool(evidence.get("queued_job_ids"))
                    or any(state is not None and tuple(state) != ("COMPLETED", "0:0")
                           for state in states.values())
                    or (evidence.get("sacct_state") is not None and
                        (evidence.get("sacct_state"), evidence.get("sacct_exit_code")) != ("COMPLETED", "0:0")))
        if contrary:
            proof["contradiction"] = True
    run.setdefault("recovery_source_digests", {})[source] = digest


def record_recovery_success(run, observation):
    """Persist a whole successful snapshot, covering every previously known job."""
    if (not slurm_quiescent(observation, run["request"])
            or any(tuple(state) != ("COMPLETED", "0:0")
                   for state in observation["cleanup_sacct_states"].values())):
        return False
    previous = run.get("probe_terminal_success", {})
    if not set(previous.get("known_job_ids", [])).issubset(observation["job_ids"]):
        return False
    run["probe_terminal_success"] = dict(binding=recovery_binding(run),
        observation=observation, known_job_ids=list(observation["job_ids"]),
        preceding_sources=dict(run.get("recovery_source_digests", {})), contradiction=False)
    return True


def recovery_success(run):
    """Historical execution evidence is usable only with explicit fencing."""
    proof = run.get("probe_terminal_success")
    if proof is None:
        return {}
    observation = proof.get("observation", {})
    if (proof.get("binding") != recovery_binding(run) or proof.get("contradiction") is not False
            or not slurm_quiescent(observation, run["request"])
            or set(proof.get("known_job_ids", [])) != set(observation["job_ids"])
            or any(tuple(state) != ("COMPLETED", "0:0")
                   for state in observation["cleanup_sacct_states"].values())):
        raise ValueError("coordinator terminal-success observation is invalid or superseded")
    return dict(job_id=observation["job_ids"][0], sacct_state="COMPLETED", sacct_exit_code="0:0")


def recovery_complete(run):
    """Revalidate existing bytes; authority never creates a completion receipt."""
    success = recovery_success(run)
    checks = run["request"]["checks"]
    if (not checks or set(run["receipts"]) != {str(i) for i in range(len(checks))}
            or run.get("error")):
        raise ValueError("recovery requires every complete bound receipt without evidence conflicts")
    for index in range(len(checks)):
        receipt = run["receipts"][str(index)]
        validate_receipt(receipt, run["request"], index)
        # An attestation supplies lifecycle authority, never Slurm success.
        if receipt["outcome"]["exit_code"] == 0:
            execution = dict(receipt["execution"])
            for key in ("job_id", "sacct_state", "sacct_exit_code"):
                if key in run["execution"]:
                    execution[key] = run["execution"][key]
            execution.update(success)
            problem = execution_problem(dict(result="pass", exit_code=0,
                candidate_tree=run["request"]["basis"]["candidate_tree"], execution=execution))
            if problem:
                raise ValueError(problem)


def recovered(run):
    proof = run.get("recovery_authority")
    if proof is None:
        return False
    if (not isinstance(proof, dict) or proof.get("evidence_class") != "attested"
            or not isinstance(proof.get("by"), str) or not proof["by"].strip()
            or not isinstance(proof.get("at"), str) or not proof["at"]):
        raise ValueError("invalid coordinator recovery authority")
    validate_recovery_attestation(proof.get("attestation"), run)
    recovery_complete(run)
    return True


def apply_recovery(run):
    # Called only after the authority has been fsynced in the coordinator ledger.
    run["execution"].update(recovery_success(run))
    run["reconciled"] = True
    run.pop("ack", None)  # No remote cleanup acknowledgment is minted.
    run["execution"].update(quiescent=True, evidence_reconciled=True,
                            recovery_evidence_class="attested",
                            recovery_authority_sha256=record_digest(run["recovery_authority"]))


def acknowledge(state_dir, unit, basis, launch_id, digest):
    """Called only after the operator's observation fence and receipt fsync."""
    with binding_lock(state_dir, unit, basis) as path:
        ledger = load_ledger(path, unit, basis)
        for run in ledger["runs"]:
            if run["launch_id"] == launch_id:
                if publication_digest(run) != digest:
                    raise ValueError("remote evidence changed before publication acknowledgment; retrieve it again")
                run["published"] = True
                revoke_negative_observation(run)
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
            "sbatch", "--parsable", "--no-requeue", "--job-name=" + slurm_job_name(request),
            "--partition=" + cfg["partition"],
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


def coordinator_program(name, roots, search_path=None):
    """Select absolute operator tools outside both lexical and resolved roots."""
    roots = [os.path.realpath(str(path)) for path in roots if path is not None]

    def excluded(path):
        return any(os.path.commonpath([root, candidate]) == root
                   for root in roots
                   for candidate in (os.path.abspath(path), os.path.realpath(path)))

    directories = os.get_exec_path() if search_path is None else search_path.split(os.pathsep)
    for directory in directories:
        if not os.path.isabs(directory) or excluded(directory):
            continue
        program = shutil.which(name, path=directory)
        if program and not excluded(program):
            return os.path.realpath(program)
    return None


def coordinator_ssh(repo, tree):
    """Keep operator PATH wrappers, excluding both operated Git trees."""
    return coordinator_program("ssh", (repo, tree))


def cancellation_tail(text):
    # Diagnostic rendering only, after the unmodified status decides the result.
    return text.encode("utf-8", "replace")[-1000:].decode("utf-8", "replace")


def publish_cleanup(path, evidence):
    if len(json.dumps(evidence).encode("utf-8")) > 65536:
        raise ValueError("cancellation history exceeds 64 KiB; stage retained")
    publish(path, evidence)


def slurm_job_name(request):
    """Stable across loss of the supervisor's job-id file; no host information."""
    return "verify-" + hashlib.sha256(request["launch_id"].encode("utf-8")).hexdigest()


def discover_slurm_jobs(request, known=()):
    """Union live and historical launch identities; failed queries never mean empty.

    Accounting starts at the epoch rather than sacct's implicit midnight. Names
    are returned wide enough for an exact comparison; no rendered identity is
    used for a decision. The live query also guards against requeued jobs whose
    older accounting row is terminal. No jobs is unresolved after a launch.
    """
    name = slurm_job_name(request)
    jobs, queued, errors = set(known), set(), []
    # Widthless -o fields use the full value without padding (squeue(1));
    # adding display widths or normalizing returned identities would lose this.
    # sacct -P is parsable2 (no trailing delimiter), unlike lowercase -p.
    queries = [
        ("sacct", ["sacct", "-n", "-P", "-X", "--name", name,
                   "--starttime", "1970-01-01", "--format=JobIDRaw,JobName%80"]),
        ("squeue", ["squeue", "-h", "--name", name, "--format=%i|%j"])]
    for source, argv in queries:
        try:
            rc, out, err = _command(argv)
            if rc:
                raise ValueError(source + " launch discovery unavailable")
            for line in out.splitlines():
                fields = line.split("|")
                if len(fields) != 2 or fields[1] != name or not re.fullmatch(r"[0-9]+", fields[0]):
                    raise ValueError(source + " returned ambiguous launch identity")
                jobs.add(fields[0])
                if source == "squeue":
                    queued.add(fields[0])
        except (OSError, ValueError, subprocess.SubprocessError) as exc:
            errors.append(source + ": " + str(exc))
    return {"job_name": name, "job_ids": sorted(jobs), "queued_job_ids": sorted(queued),
            "scheduler_confirmed": not errors, "discovery_error": "; ".join(errors),
            "scheduler_observed_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}


def slurm_quiescent(evidence, request):
    jobs, states = evidence.get("job_ids"), evidence.get("cleanup_sacct_states")
    return bool(evidence.get("launch_id") == request["launch_id"]
        and evidence.get("job_name") == slurm_job_name(request)
        and evidence.get("scheduler_confirmed") is True
        and evidence.get("queued_job_ids") == []
        and isinstance(jobs, list) and jobs
        and all(isinstance(job, str) and re.fullmatch(r"[0-9]+", job) for job in jobs)
        and isinstance(states, dict) and set(states) == set(jobs)
        and all(isinstance(state, (list, tuple)) and len(state) == 2
                and state[0] in TERMINAL for state in states.values()))


def observe_slurm_success(request, known=()):
    """Read-only execution evidence; never grants launch quiescence."""
    evidence = discover_slurm_jobs(request, known)
    states = {j: scheduler_state(j) for j in evidence["job_ids"]} if evidence["scheduler_confirmed"] else {}
    evidence.update(discover_slurm_jobs(request, evidence["job_ids"]))
    evidence.update(launch_id=request["launch_id"], cleanup_sacct_states=states)
    if (slurm_quiescent(evidence, request)
            and all(tuple(state) == ("COMPLETED", "0:0") for state in states.values())):
        evidence.update(job_id=evidence["job_ids"][0], sacct_state="COMPLETED", sacct_exit_code="0:0")
    return evidence


def slurm_success_probe(run):
    # Send current coordinator code without rewriting an older staged harness.
    known = list(run["execution"].get("job_ids", []))
    known.extend(run.get("probe_terminal_success", {}).get("known_job_ids", []))
    if run["execution"].get("job_id") is not None:
        known.append(run["execution"]["job_id"])
    if any(not isinstance(j, str) or not re.fullmatch(r"[0-9]+", j) for j in known):
        raise ValueError("invalid saved Slurm job identity")
    code = ("import hashlib, json, re, subprocess, sys, time\n"
            "sys.path.insert(0, %r)\nimport child_environment as CE\n"
            "TERMINAL = %r\n") % (run["stage"], TERMINAL)
    for function in (_command, scheduler_state, slurm_job_name, discover_slurm_jobs,
                     slurm_quiescent, observe_slurm_success):
        code += inspect.getsource(function) + "\n"
    return code + "print(json.dumps(observe_slurm_success(%r, %r)))\n" % ({"launch_id": run["launch_id"]}, known)


def cleanup_slurm(stage, request, reconciled):
    """Require every launch job terminal, supervision finished, and an exact ack.

    The mutable cleanup journal is a lifecycle observation, never a replacement
    for the write-once claim or completion receipts. It also retains identities
    discovered on earlier calls, so accounting expiry cannot erase a known job.
    """
    evidence = {"cleanup": "unconfirmed", "stage": str(stage),
                "launch_id": request["launch_id"], "quiescent": False}
    history, lock = stage / "cleanup.json", None
    removal_history = stage.with_name(stage.name + ".cleanup.json")
    removal_lock, removal_locked = None, False
    retained_cleanup = None
    try:
        # This lock survives removal of the stage and its legacy lock file.
        removal_lock = stage.with_name(stage.name + ".cleanup.lock").open("a")
        fcntl.flock(removal_lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        removal_locked = True
        lock = (stage / "cleanup.lock").open("a")
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        known, job = set(), None
        try:
            raw_job = (stage / "job-id").read_text()
            if re.fullmatch(r"[0-9]+", raw_job):
                job = raw_job
                known.add(job)
        except (OSError, UnicodeError):
            pass
        finished = None
        try:
            finished = json.loads((stage / "supervision-finished").read_text())
            if not isinstance(finished, dict) or finished.get("launch_id") != request["launch_id"]:
                raise ValueError("invalid supervision marker; stage retained")
            marker_job = finished.get("job_id")
            if marker_job is not None:
                if not isinstance(marker_job, str) or not re.fullmatch(r"[0-9]+", marker_job):
                    raise ValueError("invalid supervision job identity; stage retained")
                if job and marker_job != job:
                    raise ValueError("conflicting job identities; stage retained")
                job = marker_job
                known.add(job)
        except FileNotFoundError:
            pass
        attempts = []
        if history.exists():
            with history.open("rb") as handle:
                raw = handle.read(65537)
            if len(raw) > 65536:
                raise ValueError("cancellation history exceeds 64 KiB; stage retained")
            previous = json.loads(raw)
            attempts = previous.get("cancellation_attempts", [])
            previous_jobs = previous.get("job_ids", [])
            if (not isinstance(attempts, list) or len(attempts) > 4
                    or any(not isinstance(a, dict) for a in attempts)
                    or not isinstance(previous_jobs, list)
                    or any(not isinstance(j, str) or not re.fullmatch(r"[0-9]+", j)
                           for j in previous_jobs)):
                raise ValueError("invalid cancellation history; stage retained")
            known.update(previous_jobs)
        evidence["cancellation_attempts"] = attempts
        evidence.update(discover_slurm_jobs(request, known))
        jobs = evidence["job_ids"]
        evidence["job_id"] = job or (jobs[0] if jobs else None)
        if not evidence["scheduler_confirmed"]:
            raise ValueError(evidence["discovery_error"])
        if not finished:
            raise ValueError("supervision may still submit or publish a job ID; stage retained")
        if not jobs:
            raise ValueError("Slurm job identity unresolved; stage retained")
        states = {}
        for job in jobs:
            state = scheduler_state(job)
            # Resolve a changing snapshot before cancelling a just-finished job.
            # A still-live row overrides terminal accounting (e.g. requeue).
            if state is not None and job in evidence["queued_job_ids"]:
                evidence.update(discover_slurm_jobs(request, evidence["job_ids"]))
                if not evidence["scheduler_confirmed"]:
                    raise ValueError(evidence["discovery_error"])
                state = scheduler_state(job)
            if state is None or job in evidence["queued_job_ids"]:
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
                # Even a failed scancel can race with normal job completion.
                # Its exit never substitutes for fresh terminal accounting.
                state = scheduler_state(job)
            if state is not None:
                states[job] = list(state)
        # Finite observation contract: accounting by name, then the live queue,
        # always after all individual accounting/cancellation calls. New IDs
        # retain the stage for the next retrieval; do not query them after this
        # snapshot. A privileged requeue after it remains possible: --no-requeue
        # and worker-started protect receipts, not an atomic scheduler fence.
        evidence.update(discover_slurm_jobs(request, evidence["job_ids"]))
        evidence["cleanup_sacct_states"] = states
        evidence["quiescent"] = slurm_quiescent(evidence, request)
        if not evidence["quiescent"]:
            raise ValueError("job termination unconfirmed; stage retained")
        evidence.setdefault("cancellation", "not-required")
        # All jobs must succeed to supply passing execution evidence. Completed
        # verifier FAIL remains FAIL independently of this lifecycle summary.
        state = next((states[j] for j in jobs if states[j] != ["COMPLETED", "0:0"]),
                     states[evidence["job_id"]])
        evidence["cleanup_sacct_state"] = state[0]
        collected = collect(stage)
        if collected.get("final"):
            evidence["sacct_state"], evidence["sacct_exit_code"] = state
        if (not reconciled or reconciled != evidence_digest(collected)
                or not collected.get("final") or not collected.get("supervision")):
            raise ValueError("remote evidence is not quiescent and reconciled; stage retained")
        # Save the final observation outside the tree BEFORE deleting any of
        # its evidence. A failed final rmdir must not repopulate the stage.
        publish_cleanup(removal_history, evidence)
        retained_cleanup = dict(evidence)
        history = removal_history
        # Keep the external lock until completion, but close the in-stage file
        # before unlinking it (an open unlinked file can retain a network stage).
        lock.close()
        lock = None
        shutil.rmtree(stage, ignore_errors=False)
        if (stage.exists() or stage.is_symlink()):
            raise ValueError("stage removal unconfirmed")
        evidence["cleanup"] = "removed"
    except (OSError, ValueError, TypeError, AttributeError, subprocess.SubprocessError) as exc:
        evidence["cleanup_error"] = str(exc)[-2000:]
    finally:
        # A lock refusal must not overwrite another cleanup's journal.
        if ((history == removal_history or (stage.exists() or stage.is_symlink()))
                and evidence.get("cancellation_attempts") is not None):
            try:
                publish_cleanup(history, evidence)
            except (OSError, ValueError) as exc:
                if history == removal_history and not (stage.exists() or stage.is_symlink()):
                    # Failed audit publication is not a negative scheduler
                    # observation. Preserve the exact pre-removal snapshot so
                    # the coordinator can acknowledge it after saving this fact.
                    evidence.update(cleanup="removed", retained_cleanup=retained_cleanup,
                                    cleanup_error=str(exc)[-2000:])
                else:
                    # Audit failure supplies no new scheduler observation,
                    # including when rmdir left an empty directory behind.
                    evidence.update(cleanup="unconfirmed", cleanup_error=str(exc)[-2000:])
                    if history == removal_history:
                        evidence["retained_cleanup"] = retained_cleanup
        if removal_locked and history != removal_history:
            # The legacy lock still guards a retained stage. No deletion began,
            # so no external lock or removal evidence needs to survive this call.
            try:
                stage.with_name(stage.name + ".cleanup.lock").unlink()
            except OSError:
                pass
        if lock is not None:
            lock.close()
        if removal_lock is not None:
            removal_lock.close()
    return evidence


def cleanup(stage, reconciled=None):
    """Serialize recovery; preserve evidence until supervision and job are done.

    Four attempts and 64 KiB bound the journal. An intent is published before
    scancel so a lost process leaves an unknown outcome, never invented success.
    A zero scancel status confirms a request; only terminal accounting allows
    removal. Unknown supervision, journal failure or exhaustion retains the stage.
    """
    evidence = {"cleanup": "unconfirmed", "stage": str(stage)}
    if not (stage.exists() or stage.is_symlink()):
        return dict(evidence, cleanup="removed" if reconciled else "unconfirmed")
    try:
        request = json.loads((stage / "request.json").read_text())
        executor = request["verification_host"]["executor"]
        if executor not in ("direct", "slurm"):
            raise ValueError("invalid cleanup executor")
    except (OSError, ValueError, KeyError, TypeError) as exc:
        # Unknown policy must never route a Slurm stage through direct cleanup.
        return dict(evidence, cleanup_error="cannot read cleanup request: " + str(exc)[-2000:])
    if executor == "slurm":
        return cleanup_slurm(stage, request, reconciled)
    lock = None
    try:
        if not (stage.exists() or stage.is_symlink()):
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


def can_finish_empty_stage(run):
    """Use ordinary coordinator authority, never an auxiliary cleanup snapshot."""
    request, execution = run["request"], run.get("execution", {})
    ack = run.get("ack")
    return bool(run.get("reconciled") is True and isinstance(ack, str)
        and re.fullmatch(r"[0-9a-f]{64}", ack)
        and request.get("launch_id") == run["launch_id"]
        and request["verification_host"]["executor"] == "slurm"
        and execution.get("stage") == run["stage"]
        and execution.get("launch_id") == run["launch_id"]
        and execution.get("quiescent") is not False
        and slurm_quiescent(execution, request))


def finish_empty_stage(stage, launch_id, snapshot_sha256=None):
    """Coordinator-emitted completion; delete no contents or lifecycle evidence.

    The caller establishes durable ordinary authority before sending this code.
    Return None for a nonempty stage, which still needs normal guarded cleanup.
    Without a pinned digest, return the snapshot for durable acknowledgment;
    a second call validates it before removing the empty directory.
    """
    history = stage.with_name(stage.name + ".cleanup.json")
    lock = stage.with_name(stage.name + ".cleanup.lock")
    with lock.open("a") as guard:
        fcntl.flock(guard.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        if (stage.exists() or stage.is_symlink()) and any(stage.iterdir()):
            return None
        result = {"stage": str(stage), "launch_id": launch_id, "cleanup": "unconfirmed"}
        try:
            with history.open("rb") as handle:
                raw = handle.read(65537)
            if len(raw) > 65536:
                raise ValueError("retained cleanup snapshot exceeds 64 KiB")
            snapshot = json.loads(raw)
            if (not isinstance(snapshot, dict) or snapshot.get("stage") != str(stage)
                    or snapshot.get("launch_id") != launch_id):
                raise ValueError("retained cleanup snapshot binding mismatch")
            digest = hashlib.sha256(json.dumps(snapshot, sort_keys=True,
                separators=(",", ":")).encode("utf-8")).hexdigest()
            if snapshot_sha256 is not None and digest != snapshot_sha256:
                raise ValueError("retained cleanup snapshot digest mismatch")
            result["retained_cleanup"] = snapshot
        except (OSError, ValueError) as exc:
            result["cleanup_evidence_error"] = str(exc)[-2000:]
            return result
        if snapshot_sha256 is None:
            return dict(result, cleanup_snapshot_pending=True)
        if (stage.exists() or stage.is_symlink()):
            # The kernel refuses a late child even after the empty listing.
            stage.rmdir()
        if (stage.exists() or stage.is_symlink()):
            return dict(result, cleanup_error="stage removal unconfirmed")
        result["cleanup"] = "removed"
        return result


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
        lifecycle = response.get("lifecycle", {})
        run["reconciled"] = bool(run["reconciled"] and isinstance(lifecycle, dict)
                                 and lifecycle.get("stage") == run["stage"]
                                 and slurm_quiescent(lifecycle, request))
    if isinstance(supervision, dict):
        observe_recovery_evidence(run, supervision, "supervision")
        for key in ("job_id", "sacct_state", "sacct_exit_code"):
            if key in supervision:
                run["execution"][key] = supervision[key]
    lifecycle = response.get("lifecycle")
    if isinstance(lifecycle, dict) and lifecycle.get("stage") == run["stage"]:
        observe_recovery_evidence(run, lifecycle, "lifecycle")
        run["execution"].update(lifecycle)
        revoke_negative_observation(run)
    if run["reconciled"]:
        run["ack"] = evidence_digest(response)
    if problems:
        run["error"] = "; ".join(problems)
    return run["reconciled"]


def remote_call(prefix, remote, code, timeout=45):
    return subprocess.run(prefix + [shlex.join([remote["python"], "-c", code])],
                          stdin=subprocess.DEVNULL, capture_output=True, text=True,
                          env=CE.child_env(), timeout=timeout)


# This reader is sent by the current coordinator. Old stages need no harness
# upgrade, and their immutable evidence is never rewritten to fit a response.
READ_FRAME = r'''
import base64, hashlib, json
from pathlib import Path
stage, name, offset, limit, request_limit = SPEC
try:
    source = 'request.json' if name == 'request-header' else name
    with (Path(stage) / source).open('rb') as handle:
        data = handle.read((request_limit if name == 'request-header' else limit) + 1)
    if len(data) > (request_limit if name == 'request-header' else limit):
        raise ValueError('remote evidence exceeds file budget')
    if name == 'request-header':
        request = json.loads(data)
        data = json.dumps({'basis': request['basis'], 'launch_id': request['launch_id'],
                           'check_count': len(request['checks'])}, sort_keys=True,
                          separators=(',', ':')).encode('utf-8')
    if len(data) > limit:
        raise ValueError('remote evidence exceeds file budget')
    frame = {'name': name, 'offset': offset, 'size': len(data),
             'sha256': hashlib.sha256(data).hexdigest(),
             'data': base64.b64encode(data[offset:offset + 32768]).decode('ascii')}
except FileNotFoundError:
    frame = {'name': name, 'offset': offset, 'missing': True}
except (OSError, ValueError, KeyError, TypeError) as error:
    frame = {'name': name, 'offset': offset, 'error': str(error)}
print(json.dumps(frame))
'''
MISSING = object()


def read_remote_record(call, stage, name, limit, request_limit, budget):
    """Bound every frame and total transfer; verify original bytes before JSON."""
    data, identity = bytearray(), None
    while True:
        budget['calls'] -= 1
        if budget['calls'] < 0:
            raise ValueError('remote evidence call budget exceeded')
        spec = (stage, name, len(data), limit, request_limit)
        raw = call(READ_FRAME.replace('SPEC', repr(spec), 1))
        if len(raw) > 100000:
            raise ValueError('oversized remote response')
        frame = json.loads(raw)
        if (not isinstance(frame, dict) or frame.get('name') != name
                or type(frame.get('offset')) is not int or frame['offset'] != len(data)):
            raise ValueError('remote evidence frame identity mismatch')
        if frame.get('missing') is True and not data:
            return MISSING
        if frame.get('error'):
            raise ValueError(str(frame['error']))
        size, digest = frame.get('size'), frame.get('sha256')
        if (type(size) is not int or not 0 <= size <= limit
                or not isinstance(digest, str) or not re.fullmatch(r'[0-9a-f]{64}', digest)
                or not isinstance(frame.get('data'), str)):
            raise ValueError('invalid remote evidence frame bounds')
        if identity is not None and identity != (size, digest):
            raise ValueError('remote evidence changed between frames')
        identity = size, digest
        chunk = base64.b64decode(frame['data'], validate=True)
        if len(chunk) != min(32768, size - len(data)):
            raise ValueError('invalid remote evidence chunk length')
        budget['bytes'] -= len(chunk)
        if budget['bytes'] < 0:
            raise ValueError('remote evidence aggregate budget exceeded')
        data.extend(chunk)
        if len(data) == size:
            if hashlib.sha256(data).hexdigest() != digest:
                raise ValueError('remote evidence digest mismatch')
            return json.loads(data)


def retrieve_records(run, call, save):
    """Ingest each claim before later reads; reconcile only the complete shape."""
    request = run['request']
    count, request_size = len(request['checks']), len(encoded(request))
    # JSON can expand one astral codepoint to two six-byte escapes. Existing
    # immutable 4000/2000-character diagnostic tails must remain retrievable.
    claim_limit = request_size + 12 * (4000 + 2000) + 65536
    final_limit = request_size + 65536 + count * (12 * (4000 + 2000) + 8192)
    request_limit = len(json.dumps(request).encode('utf-8')) + 65536
    limits = [claim_limit] * count + [request_size + 65536, final_limit, 65536, 65536]
    budget = {'bytes': sum(limits), 'calls': sum(max(1, (n + 32767) // 32768) for n in limits)}
    response = {'basis': request['basis'], 'launch_id': run['launch_id'],
                'receipts': {}, 'final': None, 'supervision': None}

    def fetch(name, limit, optional=False):
        try:
            return read_remote_record(call, run['stage'], name, limit, request_limit, budget)
        except (OSError, ValueError, subprocess.SubprocessError) as exc:
            if not optional:
                response.setdefault('errors', []).append(str(exc))
            return MISSING

    for index in range(count):
        receipt = fetch('claim-{}.json'.format(index), claim_limit)
        if receipt is not MISSING:
            response['receipts'][str(index)] = receipt
        ingest(run, response)
        save()
    header = fetch('request-header', limits[count])
    if header != dict(basis=request['basis'], launch_id=run['launch_id'], check_count=count):
        response.setdefault('errors', []).append('remote staged request header mismatch or missing')
    for field, name, limit in (('final', 'worker-complete', final_limit),
                               ('supervision', 'supervision-finished', 65536),
                               ('lifecycle', 'cleanup.json', 65536)):
        value = fetch(name, limit, optional=field == 'lifecycle')
        if value is not MISSING:
            response[field] = value
    ingest(run, response)
    save()


def run_remote(runner, tree, basis, checks, remote, timeout, repo, state_dir, unit,
               journal_entries, retrieve_only=False, recovery=None):
    """Persist intent before launch and ingest completed receipts monotonically.

    A pending invocation only retrieves its original stage, even if the caller
    changes the execution host. The coordinator ledger survives publication
    fence failures and crashes. It is separate from agent-writable audit files.
    """
    if state_dir is None:
        raise ValueError("remote verification requires coordinator evidence storage")
    if journal_entries is None:
        raise ValueError("remote verification requires coordinator journal entries")
    with binding_lock(state_dir, unit, basis) as path:
        ledger = load_ledger(path, unit, basis, journal_entries)
        if recovery is not None and not retrieve_only:
            raise ValueError("recovery requires retrieval of an existing launch")
        for previous_run in ledger["runs"]:
            if revoke_negative_observation(previous_run):
                publish(path, ledger)
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
                   "request": request, "receipts": {}, "reconciled": False, "published": False,
                   "witness_required": True}
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
        if recovery is not None:
            attestation, approver = recovery
            if not isinstance(approver, str) or not approver.strip():
                raise ValueError("recovery requires a named operator")
            validate_recovery_attestation(attestation, run)
            recovery_complete(run)
            if run.get("recovery_authority") is not None:
                if run["recovery_authority"]["attestation"] != attestation:
                    raise ValueError("conflicting recovery authority")
            else:
                run["recovery_authority"] = dict(evidence_class="attested", by=approver,
                    at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                    attestation=attestation)
                run["published"] = False
                publish(path, ledger)  # Authority durable BEFORE changing disposition.
        recovery_active = recovered(run)
        if recovery_active:
            apply_recovery(run)
            publish(path, ledger)
        if not retry and not ssh:
            # No transport was attempted. A missing local client cannot create
            # unresolved remote evidence or require retrieval from a fake stage.
            execution.pop("launch_id")
            execution.update(stage=None, cleanup="not-created")
            return [incomplete("ssh is unavailable") for _ in checks], execution
        prefix = ([ssh, "-oBatchMode=yes", "-oConnectTimeout=15", remote["ssh_alias"]]
                  if ssh and not recovery_active else None)
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

        def receive_timeout(exc):
            # communicate() may have captured a complete JSON response before
            # timing out on the transport. Those bytes are still evidence.
            if isinstance(exc, subprocess.TimeoutExpired) and exc.stdout:
                captured = exc.stdout
                if isinstance(captured, bytes):
                    captured = captured.decode("utf-8", "replace")
                try:
                    receive(subprocess.CompletedProcess([], 255, captured, "transport capture timed out"))
                except (OSError, ValueError):
                    pass

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
                    publish_launch_witness(path, unit, basis, run)
                    try:
                        if not prefix:
                            raise ValueError("ssh is unavailable")
                        command = shlex.join([remote["python"], "-c", BOOTSTRAP, stage])
                        proc = subprocess.run(prefix + [command], stdin=transfer,
                            capture_output=True, text=True, env=CE.child_env(),
                            timeout=timeout * len(checks) + 120)
                        receive(proc)
                    except (OSError, ValueError, subprocess.SubprocessError) as exc:
                        receive_timeout(exc)
                        error = "remote transport incomplete: " + str(exc)
        # Bounded retrieval also works when the original aggregate or staged
        # harness is unavailable. Never rerun a verifier to recover its result.
        def frame_call(code):
            nonlocal error
            try:
                proc = remote_call(prefix, remote, code)
            except subprocess.TimeoutExpired as exc:
                if not exc.stdout:
                    raise
                captured = exc.stdout
                if isinstance(captured, bytes):
                    captured = captured.decode("utf-8", "replace")
                proc = subprocess.CompletedProcess([], 255, captured, "transport capture timed out")
            if proc.returncode:
                error = "ssh/remote transport exited {}: {}".format(proc.returncode, proc.stderr[-2000:])
            return proc.stdout
        if not run["reconciled"] and prefix:
            retrieve_records(run, frame_call, lambda: publish(path, ledger))
        if (prefix and remote["executor"] == "slurm" and checks
                and len(run["receipts"]) == len(checks)
                and ((execution.get("sacct_state"), execution.get("sacct_exit_code")) != ("COMPLETED", "0:0")
                     or run.get("probe_terminal_success"))):
            # Old supervisors can die before saving terminal success. Observe it
            # using current coordinator code; neither this probe nor its absence
            # can reconcile the launch. Ordinary cleanup still observes last.
            try:
                proc = remote_call(prefix, remote, slurm_success_probe(run))
                if proc.returncode or len(proc.stdout) > 100000:
                    raise ValueError("terminal-success observation unavailable")
                observed = json.loads(proc.stdout)
                observe_recovery_evidence(run, observed, "probe", fresh=True)
                if record_recovery_success(run, observed):
                    execution.update(job_id=observed["job_ids"][0], sacct_state="COMPLETED",
                        sacct_exit_code="0:0", recovery_success_observed_at=observed["scheduler_observed_at"])
                    publish(path, ledger)
            except (OSError, ValueError, TypeError, AttributeError, KeyError, subprocess.SubprocessError) as exc:
                execution["recovery_observation_error"] = str(exc)[-2000:]
        if error:
            run["transport_error"] = error
        # Raw evidence is durable before authorizing remote removal. This is
        # reconciliation, not publication under the operator observation fence.
        publish(path, ledger)
        # One observation can unlock reconciliation; at most one acknowledged
        # cleanup follows it, all under the existing binding lock.
        for cleanup_pass in range(2):
            if execution.get("cleanup") == "removed" or not prefix:
                break
            was_reconciled = run["reconciled"]
            code = ("import json, pathlib, sys; p=pathlib.Path(%r); "
                    "sys.path.insert(0, str(p)); from remote_verify import cleanup; "
                    "print(json.dumps(cleanup(p, %r)))") % (stage, run.get("ack") if run["reconciled"] else None)
            if can_finish_empty_stage(run):
                # The ordinary ack and positive lifecycle are durable BEFORE
                # any empty-directory completion, including a lost response.
                publish(path, ledger)
                receipt = run.get("cleanup_receipt", {})
                snapshot = receipt.get("retained_cleanup", receipt)
                snapshot_sha256 = (record_digest(snapshot) if
                    snapshot.get("stage") == stage
                    and snapshot.get("launch_id") == run["launch_id"]
                    and snapshot.get("quiescent") is True else None)
                code = ("import fcntl, hashlib, json\nfrom pathlib import Path\n"
                        + inspect.getsource(finish_empty_stage)
                        + ("\np = Path(%r)\nfinished = finish_empty_stage(p, %r, %r)\n"
                           "if finished is None:\n    exec(%r)\n"
                           "else:\n    print(json.dumps(finished))\n") % (
                               stage, run["launch_id"], snapshot_sha256, code))
            elif run["reconciled"] and remote["executor"] == "direct":
                code = ("import json, pathlib; p=pathlib.Path(%r)\n"
                        "if not (p.exists() or p.is_symlink()): print(json.dumps({'stage': str(p), 'cleanup': 'removed'}))\n"
                        "else:\n    exec(%r)\n") % (stage, code)
            try:
                proc = remote_call(prefix, remote, code)
                if proc.returncode:
                    raise ValueError("cleanup transport exited {}: {}".format(proc.returncode, proc.stderr[-2000:]))
                cleaned = json.loads(proc.stdout)
                if (not isinstance(cleaned, dict) or cleaned.get("stage") != stage
                        or cleaned.get("cleanup") not in ("removed", "unconfirmed")):
                    raise ValueError("invalid remote cleanup response")
                observe_recovery_evidence(run, cleaned, "lifecycle", fresh=True)
                # A refused snapshot must not replace the receipt that pins it.
                if "cleanup_evidence_error" not in cleaned:
                    run["cleanup_receipt"] = cleaned
                execution.update(cleaned)
                if revoke_negative_observation(run):
                    publish(path, ledger)
            except (OSError, ValueError, subprocess.SubprocessError) as exc:
                execution["cleanup_error"] = str(exc)[-2000:]
                break
            publish(path, ledger)
            if cleaned.get("cleanup_snapshot_pending") is True:
                # Lost responses have no saved digest. Persist the bound audit
                # snapshot, then validate that same snapshot before mutation.
                continue
            if (cleanup_pass or was_reconciled or remote["executor"] != "slurm"
                    or cleaned.get("quiescent") is not True
                    or not slurm_quiescent(cleaned, run["request"])):
                break
            # Re-read the immutable receipts and supervision marker along with
            # cleanup's newly persisted lifecycle observation. No receipt or
            # acknowledgment is inferred from the scheduler result alone.
            retrieve_records(run, frame_call, lambda: publish(path, ledger))
            if not run["reconciled"]:
                break
        if error:
            run["transport_error"] = error
        if (prefix and remote["executor"] == "slurm"
                and execution.get("cleanup") == "removed"):
            # The stage is gone. Retire its auxiliary evidence only after the
            # exact cleanup receipt is durable in coordinator state. This uses
            # coordinator code because the staged harness has been removed.
            publish(path, ledger)
            receipt = run.get("cleanup_receipt", {})
            retained = receipt.get("retained_cleanup", receipt)
            code = ("import fcntl, hashlib, json, pathlib\n"
                    "p = pathlib.Path(%r)\n"
                    "history = p.with_name(p.name + '.cleanup.json')\n"
                    "lock = p.with_name(p.name + '.cleanup.lock')\n"
                    "if not (p.exists() or p.is_symlink()):\n"
                    "    with lock.open('a') as guard:\n"
                    "        fcntl.flock(guard.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)\n"
                    "        if (p.exists() or p.is_symlink()): raise ValueError('stage reappeared; auxiliary evidence retained')\n"
                    "        if history.exists():\n"
                    "            data = json.loads(history.read_text())\n"
                    "            digest = hashlib.sha256(json.dumps(data, sort_keys=True, separators=(',', ':')).encode('utf-8')).hexdigest()\n"
                    "            if digest != %r: raise ValueError('cleanup snapshot changed; retained')\n"
                    "            history.unlink()\n"
                    "        lock.unlink()\n") % (stage, record_digest(retained))
            try:
                proc = remote_call(prefix, remote, code)
                if proc.returncode:
                    raise ValueError("cleanup evidence retirement transport exited {}".format(proc.returncode))
            except (OSError, ValueError, subprocess.SubprocessError) as exc:
                execution["cleanup_evidence_error"] = str(exc)[-2000:]
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
        if recovery_active:
            execution.update(recovery_success(run))
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
        # No acknowledgment is available yet: cancellation cannot remove evidence.
        cleanup(stage)
    # Direct stages await durable coordinator reconciliation before cleanup.
    print(json.dumps(collect(stage)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
