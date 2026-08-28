Your F1/F2/F4/F5 changes are mostly correct, but I found two residual issues while verifying them:

- `--profile plan` still implicitly sets `args.kind = "plan"`, so `--kind` is not literally required.
- `escalate()` still puts reviewers with no `profiles` key into every tier through `not r.get("profiles")`.

The changes below fix those too. F2’s caller-supplied round is intentionally superseded by the F6 receipt: `--round` becomes only an optional assertion against the computed round.

---

# 1. `review.py` changes

## 1.1 Imports, version floor, and constants

Change the module documentation to:

```python
Python 3.7+, standard library only.
```

Add these imports:

```python
import hashlib
import uuid
```

Add these constants after `MAX_ROUNDS`:

```python
RECEIPT_VERSION = 1
THREAT_MODEL_VERSION = 1
RECEIPT_DIRNAME = "review-gate"
```

---

## 1.2 Preserve exact plan bytes

Replace `read_text_bounded()` with these two functions:

```python
def read_bytes_bounded(path):
    """Return (bytes, error). Regular files only, size-capped."""
    fd = None
    try:
        fd = os.open(str(path), os.O_RDONLY | getattr(os, "O_NONBLOCK", 0))
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            return b"", f"not a regular file ({stat.filemode(st.st_mode)})"
        if st.st_size > MAX_FILE_READ_BYTES:
            return b"", (f"{st.st_size} bytes, above the "
                         f"{MAX_FILE_READ_BYTES}-byte read limit")
        with os.fdopen(fd, "rb", closefd=True) as fh:
            fd = None
            raw = fh.read(MAX_FILE_READ_BYTES + 1)
        if len(raw) > MAX_FILE_READ_BYTES:
            return b"", f"file grew beyond {MAX_FILE_READ_BYTES} bytes while read"
        return raw, None
    except (OSError, MemoryError) as e:
        return b"", f"unreadable: {type(e).__name__}: {e}"
    finally:
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass


def read_text_bounded(path):
    raw, err = read_bytes_bounded(path)
    if err:
        return "", err
    return raw.decode("utf-8", errors="replace"), None
```

This matters because the receipt must hash exact plan bytes. Hashing replacement-decoded text would permit different non-UTF-8 inputs to share a receipt.

---

## 1.3 Mechanically derived model family

Add this after `norm()`:

```python
def canonical_model_identity(rev):
    """Return (canonical_model, family, error).

    Family is the upstream model-maker namespace, not the transport provider.

    Direct OpenAI:
        provider=openai, model=gpt-5.2
        -> identity=openai/gpt-5.2, family=openai

    OpenRouter:
        provider=openrouter, model=openai/gpt-5.2
        -> identity=openai/gpt-5.2, family=openai

    OpenRouter route suffixes such as ':free' do not create a new identity.
    """
    provider = rev.get("provider")
    model = str(rev.get("model", "")).strip()

    if provider == "openai":
        if model.lower().startswith("openai/"):
            target = model.split("/", 1)[1]
        else:
            target = model
        if not target:
            return None, None, "empty OpenAI model id"
        return "openai/" + target.lower(), "openai", None

    if provider == "openrouter":
        route_target = model.split("?", 1)[0]
        if "/" not in route_target:
            return None, None, (
                "OpenRouter model ids used for plan review must be canonical "
                "'upstream/model' ids; an unnamespaced alias has no stable "
                "family"
            )
        family, target = route_target.split("/", 1)
        family = family.strip().lower()
        target = target.strip()
        if not family or not target:
            return None, None, "malformed OpenRouter upstream/model id"
        if family == "openrouter":
            return None, None, (
                "OpenRouter routing aliases such as openrouter/auto have no "
                "stable upstream family; pin a concrete upstream/model"
            )

        # :free, :nitro and similar route variants are transports for the same
        # upstream model, not contrasting model identities.
        if ":" in target:
            target = target.rsplit(":", 1)[0]
        return family + "/" + target.lower(), family, None

    return None, None, (
        f"provider {provider!r} has no canonical family derivation rule"
    )
```

In `load_reviewers()`, after validating each reviewer, add:

```python
        identity, family, identity_error = canonical_model_identity(r)
        r["_model_identity"] = identity
        r["_model_family"] = family
        r["_model_identity_error"] = identity_error
```

Then extend the effective plan-panel validation:

```python
        identity_errors = [
            f"{r['name']}: {r['_model_identity_error']}"
            for r in runnable if r.get("_model_identity_error")
        ]
        if identity_errors:
            config_error(
                "cannot establish plan-review model families: "
                + "; ".join(identity_errors)
                + ". Pin canonical upstream model ids rather than aliases."
            )

        identities = {r["_model_identity"] for r in runnable}
        if len(identities) != 2:
            detail = ", ".join(
                f"{r['name']}={r['_model_identity']}" for r in runnable
            )
            config_error(
                "the two plan reviewers resolve to the same underlying model "
                f"({detail}). Different API routes are not model contrast."
            )

        families = {r["_model_family"] for r in runnable}
        if len(families) != 2:
            detail = ", ".join(
                f"{r['name']}={r['_model_family']}" for r in runnable
            )
            config_error(
                "the two plan reviewers share an upstream model family "
                f"({detail}). Choose models from different upstream makers."
            )
```

Also fix the undeclared-profile escalation hole:

```python
        panel = [r for r in all_reviewers
                 if tier in (r.get("profiles") or [])
                 and r["name"] not in seen]
```

That replaces:

```python
if (not r.get("profiles") or tier in r["profiles"])
```

No `family` field is added to `reviewers.json`; therefore there is no separate family list to update when a model moves between transports.

---

## 1.4 Structured threat models and honest-run counterclaims

Add these helpers:

```python
def _reject_duplicate_json_keys(pairs):
    out = {}
    for key, value in pairs:
        if key in out:
            raise ValueError(f"duplicate JSON key {key!r}")
        out[key] = value
    return out


def load_json_document(path, label):
    text, err = read_text_bounded(Path(path))
    if err:
        config_error(f"cannot read {label} {path}: {err}")
    try:
        return json.loads(text, object_pairs_hook=_reject_duplicate_json_keys)
    except (ValueError, TypeError) as e:
        config_error(f"invalid {label} {path}: {e}")


def _string_list(obj, key, label, allow_empty=True):
    if key not in obj:
        config_error(f"{label} is missing required {key!r} array")
    value = obj[key]
    if not isinstance(value, list):
        config_error(f"{label}.{key} must be an array of strings")
    if not allow_empty and not value:
        config_error(f"{label}.{key} must not be empty")
    if not all(isinstance(x, str) and x.strip() for x in value):
        config_error(f"{label}.{key} must contain only non-empty strings")
    return [x.strip() for x in value]


def load_threat_model(path):
    obj = load_json_document(path, "threat model")
    if not isinstance(obj, dict):
        config_error("threat model must be a JSON object")
    if obj.get("version") != THREAT_MODEL_VERSION:
        config_error(
            f"threat model version must be {THREAT_MODEL_VERSION}, got "
            f"{obj.get('version')!r}"
        )

    trusted = _string_list(obj, "trusted", "threat model")
    hostile = _string_list(obj, "hostile", "threat model")
    excluded = _string_list(obj, "out_of_scope", "threat model")

    if not trusted and not hostile:
        config_error(
            "threat model must classify at least one input as trusted or hostile"
        )

    # Keep annotations rather than rejecting harmless extra keys. The required
    # fields above carry the protocol; annotations do not weaken them.
    return obj


def load_honest_runs(paths):
    runs = []
    names = set()
    for path in paths:
        obj = load_json_document(path, "honest-run case")
        if not isinstance(obj, dict):
            config_error(f"honest-run case {path} must be a JSON object")

        name = obj.get("name")
        action = obj.get("action")
        expected = obj.get("expected")
        preconditions = obj.get("preconditions")

        if not isinstance(name, str) or not name.strip():
            config_error(f"honest-run case {path} needs a non-empty 'name'")
        if name.strip() in names:
            config_error(f"duplicate honest-run case name {name.strip()!r}")
        if not isinstance(preconditions, list) or not preconditions:
            config_error(
                f"honest-run case {path}.preconditions must be a non-empty "
                f"array of strings"
            )
        if not all(isinstance(x, str) and x.strip() for x in preconditions):
            config_error(
                f"honest-run case {path}.preconditions contains an empty or "
                f"non-string value"
            )
        if not isinstance(action, str) or not action.strip():
            config_error(f"honest-run case {path} needs a non-empty 'action'")
        if not isinstance(expected, str) or not expected.strip():
            config_error(f"honest-run case {path} needs a non-empty 'expected'")

        names.add(name.strip())
        runs.append({
            "name": name.strip(),
            "preconditions": [x.strip() for x in preconditions],
            "action": action.strip(),
            "expected": expected.strip(),
        })
    return runs


def honest_run_claims(runs):
    claims = []
    for run in runs:
        claims.append(
            "HONEST-RUN COUNTER-CLAIM "
            + json.dumps(run["name"])
            + ": given "
            + json.dumps(run["preconditions"], ensure_ascii=False)
            + ", when "
            + run["action"]
            + ", then "
            + run["expected"]
            + "; this change must not newly refuse, fail, or downgrade that run."
        )
    return claims


def canonical_json_bytes(obj):
    return json.dumps(
        obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")
```

Add arguments:

```python
    ap.add_argument(
        "--plan-file",
        help="the exact reviewed plan/acceptance-criteria file. Required for "
             "plan and implementation reviews."
    )
    ap.add_argument(
        "--change",
        help="stable change receipt id emitted by a passing plan review. "
             "Required for implementation reviews."
    )
    ap.add_argument(
        "--honest-run", action="append", default=[], metavar="FILE",
        help="structured honest-run counter-claim JSON; repeatable and required"
    )
```

Replace the `--threat-model` argument with:

```python
    ap.add_argument(
        "--threat-model", metavar="FILE",
        help="required structured JSON threat model with version, trusted, "
             "hostile and out_of_scope arrays"
    )
```

The file shapes are:

```json
{
  "version": 1,
  "trusted": [
    "reviewers.json is maintained by the repository owner"
  ],
  "hostile": [
    "CLI paths and reviewed source text may be attacker-controlled"
  ],
  "out_of_scope": [
    "an attacker who can modify .git/review-gate or review.py itself"
  ]
}
```

```json
{
  "name": "annotated acceptance criterion remains valid",
  "preconditions": [
    "the contract is valid",
    "the criterion contains an unknown annotation key"
  ],
  "action": "run the normal contract validation command",
  "expected": "the annotation is preserved or ignored and the valid criterion is accepted"
}
```

After `--list` handling, require and load them:

```python
    if not args.plan_file:
        config_error("--plan-file is required for every real review")
    if not args.threat_model:
        config_error("--threat-model FILE is required for every real review")
    if not args.honest_run:
        config_error(
            "at least one --honest-run FILE is required; a free-text --claim "
            "does not satisfy the honest-run counter-claim"
        )

    plan_raw, plan_error = read_bytes_bounded(Path(args.plan_file))
    if plan_error:
        config_error(f"cannot read plan file {args.plan_file}: {plan_error}")
    plan_text = plan_raw.decode("utf-8", errors="replace")

    threat_model = load_threat_model(args.threat_model)
    honest_runs = load_honest_runs(args.honest_run)
    args.claim.extend(honest_run_claims(honest_runs))
```

For plan reviews, ensure the exact hashed plan is actually sent:

```python
    if args.kind == "plan" and args.plan_file not in args.file:
        args.file.append(args.plan_file)
```

---

## 1.5 Stable receipt and automatic round assignment

Add this code before `main()`:

```python
class ReceiptError(Exception):
    pass


def framing_identity(plan_raw, threat_model):
    plan_sha = hashlib.sha256(plan_raw).hexdigest()
    threat_sha = hashlib.sha256(canonical_json_bytes(threat_model)).hexdigest()

    digest = hashlib.sha256()
    digest.update(b"review-framing-v1\0")
    digest.update(plan_sha.encode("ascii"))
    digest.update(b"\0")
    digest.update(threat_sha.encode("ascii"))
    return digest.hexdigest(), plan_sha, threat_sha


def receipt_root():
    try:
        cp = subprocess.run(
            ["git", "rev-parse", "--git-common-dir"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            universal_newlines=True,
            timeout=15,
        )
    except (OSError, subprocess.SubprocessError) as e:
        raise ReceiptError(f"cannot locate Git common directory: {e}")

    if cp.returncode != 0 or not cp.stdout.strip():
        raise ReceiptError(
            "review receipts require a Git work tree; "
            + "git rev-parse --git-common-dir failed"
        )

    common = Path(cp.stdout.strip())
    if not common.is_absolute():
        common = Path.cwd() / common
    return common.resolve() / RECEIPT_DIRNAME


def _atomic_write(path, raw):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(
        "." + path.name + "." + uuid.uuid4().hex + ".tmp"
    )
    fd = None
    try:
        fd = os.open(
            str(temp),
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            0o600,
        )
        with os.fdopen(fd, "wb", closefd=True) as fh:
            fd = None
            fh.write(raw)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(str(temp), str(path))
    finally:
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass
        try:
            if temp.exists():
                temp.unlink()
        except OSError:
            pass


def _receipt_path(root, change_id):
    if not re.fullmatch(r"[0-9a-f]{64}", str(change_id or "")):
        raise ReceiptError(
            "change id must be the 64-hex id printed by a passing plan review"
        )
    return root / "receipts" / (change_id + ".json")


def _load_receipt_unlocked(path):
    raw, err = read_bytes_bounded(path)
    if err:
        raise ReceiptError(f"cannot read receipt {path}: {err}")
    try:
        data = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_reject_duplicate_json_keys,
        )
    except (ValueError, TypeError, UnicodeDecodeError) as e:
        raise ReceiptError(f"invalid receipt {path}: {e}")
    if not isinstance(data, dict) or data.get("version") != RECEIPT_VERSION:
        raise ReceiptError(f"unsupported or malformed receipt {path}")
    return data


def _save_receipt_unlocked(path, data):
    _atomic_write(
        path,
        json.dumps(
            data, indent=2, sort_keys=True, ensure_ascii=False
        ).encode("utf-8") + b"\n",
    )


def _acquire_receipt_lock(root):
    root.mkdir(parents=True, exist_ok=True)
    lock = root / ".lock"
    for _ in range(200):
        try:
            os.mkdir(str(lock), 0o700)
            return lock
        except FileExistsError:
            try:
                if time.time() - lock.stat().st_mtime > 300:
                    os.rmdir(str(lock))
                    continue
            except OSError:
                pass
            time.sleep(0.05)
    raise ReceiptError(
        f"could not acquire receipt lock {lock}; another review may be updating it"
    )


def _release_receipt_lock(lock):
    try:
        os.rmdir(str(lock))
    except OSError:
        pass


def validate_receipt(root, change_id, plan_sha, threat_sha):
    path = _receipt_path(root, change_id)
    if not path.exists():
        raise ReceiptError(
            f"no passing plan receipt for change {change_id}; run the plan "
            f"review first and use the id it prints"
        )
    data = _load_receipt_unlocked(path)
    if data.get("change_id") != change_id:
        raise ReceiptError("receipt change id does not match its filename")
    if data.get("plan_sha256") != plan_sha:
        raise ReceiptError(
            "the plan changed after its passing review; review the new plan "
            "and use its new change id"
        )
    if data.get("threat_model_sha256") != threat_sha:
        raise ReceiptError(
            "the threat model changed after plan review; review the new "
            "framing and use its new change id"
        )
    return data


def record_plan_pass(root, change_id, plan_sha, threat_sha,
                     plan_file, panel):
    lock = _acquire_receipt_lock(root)
    try:
        path = _receipt_path(root, change_id)
        if path.exists():
            data = _load_receipt_unlocked(path)
            if (data.get("change_id") != change_id
                    or data.get("plan_sha256") != plan_sha
                    or data.get("threat_model_sha256") != threat_sha):
                raise ReceiptError(
                    "existing receipt disagrees with the reviewed framing"
                )
        else:
            data = {
                "version": RECEIPT_VERSION,
                "change_id": change_id,
                "plan_sha256": plan_sha,
                "threat_model_sha256": threat_sha,
                "plan_file": str(plan_file),
                "created_at": now(),
                "plan_reviews": [],
                "rounds": [],
                "step_back_required": None,
            }

        data.setdefault("plan_reviews", []).append({
            "passed_at": now(),
            "panel": [{
                "name": r["name"],
                "provider": r["provider"],
                "model": r["model"],
                "model_identity": r["_model_identity"],
                "model_family": r["_model_family"],
            } for r in panel],
        })
        data["plan_reviews"] = data["plan_reviews"][-20:]
        _save_receipt_unlocked(path, data)
        return path
    finally:
        _release_receipt_lock(lock)


def reserve_round(root, change_id, evidence_sha, reviewed_body,
                  expected_round=None, stale_after=7200):
    """Reserve or reuse the round for an exact reviewed implementation body."""
    lock = _acquire_receipt_lock(root)
    try:
        path = _receipt_path(root, change_id)
        data = _load_receipt_unlocked(path)
        rounds = data.setdefault("rounds", [])
        cutoff = time.time() - max(3600, stale_after)

        # A crashed process must not lock the change forever.
        for entry in rounds:
            pending = entry.get("pending") or []
            entry["pending"] = [
                p for p in pending
                if isinstance(p, dict)
                and isinstance(p.get("at"), (int, float))
                and p["at"] >= cutoff
            ]
            if not entry.get("consumed") and not entry["pending"]:
                entry["round"] = None

        if any(entry.get("pending") for entry in rounds):
            raise ReceiptError(
                "another review of this change is still in progress; wait for "
                "it to finish rather than assigning overlapping rounds"
            )

        existing = next(
            (entry for entry in rounds
             if entry.get("evidence_sha256") == evidence_sha),
            None,
        )

        if data.get("step_back_required") and not (
                existing and existing.get("consumed")):
            reason = data["step_back_required"]
            raise ReceiptError(
                "the previous review found or could not exclude a defect "
                "introduced by the previous fix. This receipt is latched for "
                "step-back; revise and re-review the plan instead of adding "
                f"another patch. Trigger: {reason}"
            )

        consumed = [entry for entry in rounds if entry.get("consumed")]
        if existing and existing.get("consumed"):
            round_no = existing["round"]
        else:
            round_no = len(consumed) + 1
            if round_no > MAX_ROUNDS:
                raise ReceiptError(
                    f"change {change_id} already has {len(consumed)} reviewed "
                    f"implementation versions; the bound is {MAX_ROUNDS}. "
                    f"Step back, change the plan/criteria, and obtain a new "
                    f"passing plan receipt."
                )
            if existing is None:
                existing = {
                    "evidence_sha256": evidence_sha,
                    "round": round_no,
                    "consumed": False,
                    "pending": [],
                    "attempts": [],
                }
                rounds.append(existing)
            else:
                existing["round"] = round_no

        if expected_round is not None and expected_round != round_no:
            raise ReceiptError(
                f"--round {expected_round} disagrees with receipt-computed "
                f"round {round_no}; --round cannot assign or reset a round"
            )

        previous = None
        if round_no > 1:
            previous = next(
                (entry for entry in consumed
                 if entry.get("round") == round_no - 1),
                None,
            )
            if previous is None:
                raise ReceiptError(
                    f"receipt has no evidence for previous round {round_no - 1}"
                )

        token = uuid.uuid4().hex
        existing["pending"] = [{"token": token, "at": time.time()}]

        evidence_rel = (
            Path("evidence") / change_id / (evidence_sha + ".txt")
        )
        evidence_path = root / evidence_rel
        reviewed_raw = reviewed_body.encode("utf-8")
        reviewed_sha = hashlib.sha256(reviewed_raw).hexdigest()

        if evidence_path.exists():
            old, err = read_bytes_bounded(evidence_path)
            if err or hashlib.sha256(old).hexdigest() != reviewed_sha:
                raise ReceiptError(
                    f"stored review evidence {evidence_path} is missing or "
                    f"does not match its receipt"
                )
        else:
            _atomic_write(evidence_path, reviewed_raw)

        existing["evidence_file"] = str(evidence_rel)
        existing["reviewed_body_sha256"] = reviewed_sha
        _save_receipt_unlocked(path, data)

        previous_body = None
        if previous is not None:
            previous_path = root / previous["evidence_file"]
            previous_raw, err = read_bytes_bounded(previous_path)
            if err:
                raise ReceiptError(
                    f"cannot read previous-round evidence: {err}"
                )
            previous_body = previous_raw.decode("utf-8", errors="replace")

        return round_no, token, previous_body
    finally:
        _release_receipt_lock(lock)


def finish_round(root, change_id, evidence_sha, token, state,
                 completed_count, step_back_details=None):
    lock = _acquire_receipt_lock(root)
    try:
        path = _receipt_path(root, change_id)
        data = _load_receipt_unlocked(path)
        entry = next(
            (item for item in data.get("rounds", [])
             if item.get("evidence_sha256") == evidence_sha),
            None,
        )
        if entry is None:
            raise ReceiptError("reserved round disappeared from its receipt")

        entry["pending"] = [
            p for p in (entry.get("pending") or [])
            if p.get("token") != token
        ]
        entry.setdefault("attempts", []).append({
            "finished_at": now(),
            "state": state,
            "completed_reviewers": completed_count,
        })
        entry["attempts"] = entry["attempts"][-20:]

        # No model completed means no review round occurred. The same or a new
        # body may therefore take this number on the retry.
        if completed_count:
            entry["consumed"] = True
            entry["reviewed_at"] = now()
        elif not entry["pending"] and not entry.get("consumed"):
            entry["round"] = None

        if step_back_details:
            data["step_back_required"] = {
                "detected_at": now(),
                "round": entry.get("round"),
                "details": step_back_details,
            }

        _save_receipt_unlocked(path, data)
    finally:
        _release_receipt_lock(lock)
```

### Main preflight changes

Retire implicit kind inference. Replace:

```python
    if args.plan:
        ...
    if args.profile == "plan" and args.kind is None:
        args.kind = "plan"
```

with:

```python
    if args.plan:
        config_error(
            "--plan was retired; pass the explicit `--kind plan` instead"
        )
```

Keep the existing `args.kind is None` refusal.

Replace implementation-round enforcement with:

```python
    if args.kind == "implementation" and not args.change and not args.list:
        config_error(
            "--kind implementation requires --change ID from a passing plan "
            "review. The receipt, not the caller, assigns the round."
        )
    if args.kind == "plan" and args.change:
        config_error(
            "--change is emitted after a passing plan review; do not supply it "
            "while reviewing the plan"
        )
    if args.kind == "plan" and args.round is not None:
        config_error("--round applies only to implementation reviews")
    if args.round is not None and args.round < 1:
        config_error(f"--round must be 1 or more, got {args.round}")
```

Delete the caller-side `args.round > MAX_ROUNDS` check.

After loading the plan and threat model:

```python
    change_id, plan_sha, threat_sha = framing_identity(
        plan_raw, threat_model
    )
    receipts = receipt_root()

    if args.kind == "implementation":
        if args.change != change_id:
            config_error(
                "the supplied change id does not match the current plan and "
                "threat model. The plan or threat model changed, or this is "
                "the wrong receipt."
            )
        try:
            validate_receipt(
                receipts, args.change, plan_sha, threat_sha
            )
        except ReceiptError as e:
            config_error(str(e))
```

After `gather(args)`, but before truncating:

```python
    full_body = body
    evidence_sha = hashlib.sha256(
        full_body.encode("utf-8")
    ).hexdigest()
```

Move runnable-reviewer availability resolution before round reservation. Once at least one reviewer is runnable:

```python
    round_no = None
    reservation_token = None
    previous_body = None

    if args.kind == "implementation":
        try:
            round_no, reservation_token, previous_body = reserve_round(
                receipts,
                args.change,
                evidence_sha,
                body,
                expected_round=args.round,
                stale_after=max(3600, (
                    args.watchdog if args.watchdog is not None
                    else max(1800, args.timeout * 3)
                ) * 2),
            )
        except ReceiptError as e:
            config_error(str(e))

    args._require_fix_origin = previous_body is not None
```

The receipt-computed round is the only round used in reports and prompts.

---

## 1.6 Compare each implementation with the previous round

Replace `build_prompt()` with:

```python
def build_prompt(body, claims, truncated, context, threat_model,
                 plan_text, round_no=None, previous_body=None):
    out = []

    if context:
        out.append(f"CONTEXT\n{context}\n")

    out.append(
        "THREAT MODEL — structured and reviewed as part of the framing:\n"
        + json.dumps(threat_model, indent=2, ensure_ascii=False)
        + "\n"
    )

    out.append("REVIEWED PLAN / ACCEPTANCE CRITERIA:\n")
    out.append(plan_text)
    out.append("")

    if round_no is not None:
        out.append(f"IMPLEMENTATION ROUND: {round_no}\n")

    if claims:
        out.append("CLAIMS ASSERTED ABOUT THIS WORK — assess each one:")
        for index, claim in enumerate(claims):
            out.append(f"  [{index}] {claim}")
        out.append("")
    else:
        out.append("No explicit claims were asserted.\n")

    if previous_body is not None:
        out.append(
            "PREVIOUS REVIEWED IMPLEMENTATION\n"
            "The current implementation contains a fix made after this prior "
            "review. For every finding, classify fix_origin as:\n"
            "- previous_fix: the failure is introduced by or is a defect in "
            "the delta from the previous implementation to the current one;\n"
            "- not_previous_fix: the defect already existed independently of "
            "that delta;\n"
            "- uncertain: the shown evidence does not establish causality.\n"
            "Give concrete fix_origin_why. Do not infer causality merely "
            "because this is a later round.\n\n"
            + previous_body
            + "\n"
        )

    if truncated:
        out.append(
            f"NOTE: input truncated to {MAX_CHARS} chars; you are not seeing "
            f"the whole change.\n"
        )

    out.append("CURRENT CODE UNDER REVIEW:\n")
    out.append(body)
    return "\n".join(out)
```

Call it as:

```python
    prompt = build_prompt(
        body,
        args.claim,
        truncated,
        args.context,
        threat_model,
        plan_text,
        round_no=round_no,
        previous_body=previous_body,
    )
```

Extend each finding in `SYSTEM` with:

```json
"fix_origin": "previous_fix"|"not_previous_fix"|"uncertain"|"not_applicable",
"fix_origin_why": "specific evidence for that classification"
```

Change `in_scope` to:

```json
"in_scope": true|false|null
```

`null` means the reviewer cannot establish scope from the threat model.

Extend `verdict_schema_error()`:

```python
def verdict_schema_error(v, require_claims=0, asserted=None,
                         require_fix_origin=False):
```

Inside its finding loop add:

```python
        if "in_scope" not in f or (
                f["in_scope"] is not None
                and not isinstance(f["in_scope"], bool)):
            return "finding in_scope must be true, false, or null"

        preconditions = f.get("preconditions")
        if not isinstance(preconditions, str) or not preconditions.strip():
            return "finding has no concrete preconditions"

        if require_fix_origin:
            origin = norm(f.get("fix_origin"))
            if origin not in (
                    "previous_fix", "not_previous_fix", "uncertain"):
                return (
                    "finding fix_origin must be previous_fix, "
                    "not_previous_fix, or uncertain"
                )
            why = str(f.get("fix_origin_why", "")).strip()
            words = {
                w for w in re.findall(r"[a-z0-9_]{2,}", why.lower())
            }
            if len(words) < 4:
                return "finding fix_origin_why is not substantive"
```

Thread `require_fix_origin` through `run_one()`, `_run_one()`, and both call sites:

```python
def run_one(rev, prompt, timeout, require_claims=0, asserted=None,
            require_fix_origin=False):
```

```python
def _run_one(rev, prompt, timeout, require_claims=0, asserted=None,
             require_fix_origin=False):
```

```python
why = verdict_schema_error(
    verdict, require_claims, asserted, require_fix_origin
)
```

And in reviewer calls:

```python
run_one(
    r, prompt, args.timeout, len(args.claim), args.claim,
    args._require_fix_origin
)
```

After aggregating confirmed findings:

```python
    step_back_findings = [
        f for f in confirmed
        if norm(f.get("fix_origin")) in ("previous_fix", "uncertain")
    ]
```

After deciding state, finalize the receipt:

```python
    if args.kind == "implementation":
        details = None
        if step_back_findings:
            details = [{
                "reviewer": f.get("reviewer"),
                "file": f.get("file"),
                "line": f.get("line"),
                "summary": f.get("summary"),
                "fix_origin": f.get("fix_origin"),
                "fix_origin_why": f.get("fix_origin_why"),
            } for f in step_back_findings]
        try:
            finish_round(
                receipts,
                args.change,
                evidence_sha,
                reservation_token,
                state,
                len(completed),
                step_back_details=details,
            )
        except ReceiptError as e:
            print(
                "REVIEW_ERROR — review completed but its receipt could not be "
                f"updated: {e}",
                file=sys.stderr,
            )
            sys.exit(STATES["REVIEW_ERROR"])
```

For a passing plan review:

```python
    receipt_path = None
    if args.kind == "plan" and state == "REVIEW_PASS":
        try:
            receipt_path = record_plan_pass(
                receipts,
                change_id,
                plan_sha,
                threat_sha,
                args.plan_file,
                [r for r in reviewers if r.get("enabled", True)],
            )
        except ReceiptError as e:
            print(
                "REVIEW_ERROR — plan review passed but its receipt could not "
                f"be recorded: {e}",
                file=sys.stderr,
            )
            sys.exit(STATES["REVIEW_ERROR"])
```

Add to the JSON report:

```python
        "change_id": change_id,
        "round": round_no,
        "receipt": str(receipt_path) if receipt_path else None,
        "step_back_required": bool(step_back_findings),
```

For non-JSON passing plan output:

```python
        if receipt_path:
            print(f"  Change receipt: {change_id}")
            print(f"  Stored at: {receipt_path}")
```

---

## 1.7 Symmetric uncertainty handling

Replace the opening uncertainty instruction in `SYSTEM`:

```text
If you cannot decide whether something is correct, treat it as refuted
```

with:

```text
Uncertainty is neither approval nor a demonstrated defect. Do not turn missing
evidence into an accusation. Mark a claim "unverifiable", use "in_scope": null
when scope cannot be established, and use low confidence for a concrete but
unresolved failure hypothesis. The gate treats those states as REVIEW_PARTIAL,
not as a pass and not as a demonstrated REVIEW_FAIL.
```

Add a required top-level array to the response schema:

```json
"uncertainties": [
  {
    "subject": "specific unresolved behaviour",
    "missing_evidence": "what evidence is absent",
    "failure_if_wrong": "concrete wrong behaviour if the concern is real"
  }
]
```

Change the verdict enum to:

```json
"verdict": "upheld" | "refuted" | "uncertain"
```

And specify:

```text
"verdict" is:
- "refuted" for a demonstrated in-scope critical/major defect or refuted claim;
- "uncertain" when there is no demonstrated refutation but a serious low-
  confidence/scope-unknown issue, an unverifiable claim, or an uncertainty;
- "upheld" otherwise.
```

Update schema validation:

```python
    if norm(v.get("verdict")) not in ("upheld", "refuted", "uncertain"):
        return (
            "verdict must be 'upheld', 'refuted', or 'uncertain', got "
            f"{v.get('verdict')!r}"
        )

    for key in ("findings", "claims", "uncertainties"):
        if key not in v:
            return f"missing required '{key}' array"
        if not isinstance(v[key], list):
            return f"{key} must be a list"

    for item in v["uncertainties"]:
        if not isinstance(item, dict):
            return "uncertainties must contain objects"
        for key in ("subject", "missing_evidence", "failure_if_wrong"):
            value = item.get(key)
            if not isinstance(value, str) or not value.strip():
                return f"uncertainty has no non-empty {key!r}"
```

Return it from `_run_one()`:

```python
            "uncertainties": deep_redact(
                verdict.get("uncertainties", []) or []
            ),
```

Change `is_confirmed()` so unknown scope cannot become a failure:

```python
def is_confirmed(f):
    return (
        f.get("in_scope") is True
        and norm(f.get("severity")) in ("critical", "major")
        and norm(f.get("confidence")) in ("high", "medium")
        and bool(str(f.get("failure_scenario", "")).strip())
    )
```

Add:

```python
def is_unresolved_finding(f):
    if norm(f.get("severity")) not in ("critical", "major"):
        return False
    if f.get("in_scope") is None:
        return True
    return (
        f.get("in_scope") is True
        and norm(f.get("confidence")) == "low"
    )
```

Aggregate uncertainty:

```python
    unresolved_findings = []
    unverifiable_claims = []
    explicit_uncertainties = []
    uncertain_reviewers = []

    for r in completed:
        if norm(r.get("verdict")) == "uncertain":
            uncertain_reviewers.append(r["name"])

        for f in r["findings"]:
            if is_unresolved_finding(f):
                unresolved_findings.append({**f, "reviewer": r["name"]})

        for c in r["claims"]:
            if norm(c.get("status")) == "unverifiable":
                unverifiable_claims.append({**c, "reviewer": r["name"]})

        for item in r.get("uncertainties", []):
            explicit_uncertainties.append({**item, "reviewer": r["name"]})
```

Replace `decide_state()` with:

```python
def decide_state(n_completed, n_failed, confirmed, refuted_claims,
                 rejecting, truncated, quorum, unresolved=0):
    if n_completed == 0:
        return "REVIEW_UNAVAILABLE"

    # A single model does not become a committee merely because it found
    # something. Below quorum, the result is incomplete in both directions.
    if n_completed < quorum:
        return "REVIEW_PARTIAL"

    if confirmed or refuted_claims:
        return "REVIEW_FAIL"

    if n_failed:
        return "REVIEW_PARTIAL"

    if unresolved:
        return "REVIEW_PARTIAL"

    if rejecting:
        return "REVIEW_PARTIAL"

    if truncated:
        return "REVIEW_PARTIAL"

    return "REVIEW_PASS"
```

Call it with:

```python
    unresolved_count = (
        len(unresolved_findings)
        + len(unverifiable_claims)
        + len(explicit_uncertainties)
        + len(uncertain_reviewers)
    )

    state = decide_state(
        n_completed=len(completed),
        n_failed=len(failed),
        confirmed=confirmed,
        refuted_claims=refuted_claims,
        rejecting=rejecting,
        truncated=truncated,
        quorum=args.quorum,
        unresolved=unresolved_count,
    )
```

Add all four uncertainty collections to the JSON report.

This is the important F12 correction: uncertainty can no longer silently become either `REVIEW_FAIL` or `REVIEW_PASS`. It becomes `REVIEW_PARTIAL`.

---

# 2. F10: Python 3.7 AST compatibility

Do not move the floor to 3.8 solely for this. The shown production code has no 3.8-only syntax or stdlib dependency.

Replace AST-test code using only `ast.Constant` with:

```python
def ast_string_value(node):
    # Python 3.7's parser emits ast.Str. Newer parsers emit ast.Constant.
    value = getattr(node, "value", None)
    if isinstance(value, str):
        return value

    value = getattr(node, "s", None)
    if isinstance(value, str):
        return value

    return None


def string_literals(tree):
    for node in ast.walk(tree):
        value = ast_string_value(node)
        if value is not None:
            yield value
```

For example, the floor assertion becomes:

```python
tree = ast.parse(source)
literals = list(string_literals(tree))
self.assertGreater(
    len(literals), 0,
    "test found no string literals; AST compatibility helper is broken",
)
```

Also change `review.py`’s own declaration from `Python 3.8+` to `Python 3.7+`.

The remaining process gap is that CI still does not run 3.7. Either add a 3.7 compatibility job or be explicit that 3.7 is parser-tested but not execution-tested.

---

# 3. Corrected `PROTOCOL.md`

## Exact F11 contradiction

The contradiction is in the first table:

```markdown
| panel | cheapest-first ladder |
| escalation | `--escalate`, always from `fast` |
| flag | `--kind implementation --escalate --round N` |
```

The code does not require `--escalate`; without it, it runs the selected/default fixed profile. Also, after F6, the round comes from the receipt rather than `--round`.

Replace the table with:

```markdown
| | plan review | implementation review |
|---|---|---|
| when | before code exists | after the change is written |
| panel | **two contrasting upstream model families** | selected profile, or cheapest-first ladder with `--escalate` |
| escalation | **never** | optional; `--escalate` always starts at `fast` |
| flag | `--kind plan --plan-file PLAN` | `--kind implementation --change ID --plan-file PLAN [--escalate]` |
| judged against | do these criteria hold together | does the code meet the reviewed criteria |
```

Replace:

```markdown
`--plan` fixes the panel at two and refuses `--escalate`.
```

with:

```markdown
`--kind plan` fixes the effective panel at two and refuses `--escalate`.
```

Replace the enforcement table with:

```markdown
| rule | enforcement |
|---|---|
| a review declares its kind | `--kind plan|implementation` is required |
| a plan panel is exactly two | checked after profile, `--only`, and enabled-state selection |
| plan reviewers use different transports | effective providers must differ |
| plan reviewers use different upstream families | family is derived from the canonical upstream model namespace, not reviewer names or transport routes |
| a plan review is never escalated | `--escalate` is refused |
| a plan review needs both verdicts | quorum must be exactly 2 |
| an undeclared reviewer joins no profile | missing `profiles` means no membership, including escalation tiers |
| implementation rounds belong to one reviewed framing | the plan and threat-model digests identify a receipt under `.git/review-gate` |
| at most 3 reviewed implementation versions per framing | the receipt assigns rounds; `--round` cannot assign or reset them |
| every review has a threat model | `--threat-model` must be structured JSON |
| every review asserts an honest run | at least one structured `--honest-run` case is converted into a required claim |
| uncertainty is not silently passed or failed | unresolved scope, claims, or evidence produce `REVIEW_PARTIAL` |
```

Replace the old round-gap section with:

```markdown
## Receipts and rounds

A passing plan review creates a receipt in:

`.git/review-gate/receipts/<change-id>.json`

The change id is a SHA-256 digest of the exact plan bytes and canonical structured
threat model. An implementation review must pass that id with `--change`.

The receipt, not the caller, assigns the round. Re-running the exact same
review body reuses its round. A new reviewed implementation body consumes the
next round, up to three. An invocation where no reviewer completes does not
consume a round.

Rebase and amend do not reset the receipt because commit ids are not part of
the change id. If they change the gathered implementation evidence, that
evidence is a new reviewed version and consumes the next round.

Changing any byte of the plan, or changing the canonical threat model, invalidates
the old receipt. The new framing must pass a new two-model plan review and starts
with a new receipt. This is intentional: the implementation must not continue
under criteria that the plan panel did not review.
```

Replace the step-back claim with:

```markdown
## Step-back signal

For round N+1, the tool supplies the reviewers with the exact implementation
evidence retained from round N. Every finding must classify whether it is a
defect in the delta made after round N, was independent of that delta, or cannot
be causally classified from the evidence.

A confirmed defect classified as `previous_fix`, or whose fix origin remains
`uncertain`, latches the receipt. The same evidence may be re-reviewed, but a
new implementation version is refused. Continuing requires changing and
re-reviewing the plan/criteria.

This mechanises collection and enforcement of the reviewers' causal judgment.
It does not make semantic causality objectively decidable: a reviewer can still
misattribute a defect, and a cumulative diff may not contain enough history.
```

Replace the prose-only threat/counterclaim sections with:

```markdown
## Always pass a structured threat model

`--threat-model FILE` is required. The JSON document has versioned `trusted`,
`hostile`, and `out_of_scope` arrays. A reviewer must classify finding scope as
true, false, or unknown. Unknown scope cannot buy a pass; it produces
`REVIEW_PARTIAL`. Explicitly out-of-scope findings are still reported but do
not decide the verdict.

## Always assert a structured honest-run counter-claim

At least one `--honest-run FILE` is required. Each case names concrete
preconditions, an action, and expected behaviour. The tool constructs the
counter-claim itself, so satisfying the rule is not keyword matching.

A refuted honest-run claim fails the review. An unverifiable honest-run claim
produces `REVIEW_PARTIAL`; it cannot silently pass.
```

---

# 4. Finding-by-finding result

## F3 — family contrast

**Concrete change:** Family is mechanically derived from the canonical upstream model namespace. OpenAI direct and OpenRouter `openai/...` therefore resolve to the same family and identity.

**Now refuses:**

- Same OpenAI model through OpenAI and OpenRouter.
- Two OpenAI-family models through different transports.
- OpenRouter aliases without a stable upstream namespace.
- Route variants such as `:free` being presented as separate models.

**Still cannot catch:**

- A provider silently serving a different model than its requested canonical id.
- Two ostensibly different upstream makers using shared weights.
- Undisclosed distillation or common training lineage.

**Can honest review fail?** Yes. Two genuinely different architectures from the same upstream maker are conservatively treated as one family. That is preferable to a hand-maintained exception list that silently rots.

---

## F6 — receipt-bound rounds

**Concrete change:** A passing plan review writes a receipt under `.git/review-gate`, keyed by exact plan and threat-model digests. Implementation rounds are assigned from unique reviewed bodies.

**Now refuses:**

- Repeatedly claiming `--round 1`.
- Implementation without a passing plan receipt.
- Reusing a receipt after the plan or threat model changes.
- A fourth distinct reviewed implementation version.
- Parallel invocations attempting overlapping round assignment.

**Rebase/amend:** They do not change the change id. If gathered implementation evidence remains identical, the round is reused; if it changes, the next round is consumed.

**Plan changes:** Old receipt is invalid. The new plan needs a new two-model plan review and receipt.

**Still cannot catch:**

- A user who can edit/delete `.git/review-gate`, `review.py`, or the repository config.
- Semantic identity between two differently worded plans.
- Reviewing an intentionally incomplete source selection.
- Reusing identical plans for genuinely unrelated work; those jobs need distinct plan content identifying their purpose.

**Can honest review fail?** Yes: harmless plan-byte changes require another plan review, receipt corruption blocks the gate, and a rebase that materially changes gathered evidence consumes another round.

---

## F7 — defects in the previous fix

**Concrete change:** The tool retains prior evidence, shows it alongside the current evidence, requires structured causal classification, and latches the receipt on `previous_fix` or unresolved causality for a confirmed defect.

**Now refuses:** Another patch under the same receipt after a confirmed finding is attributed, or cannot be excluded as attributable, to the previous fix.

**Still cannot catch:** Semantic causality is not objectively mechanisable. The model can misattribute the finding, and the retained evidence may not reveal the real edit history.

**Can honest review fail?** Yes. A false or uncertain causal attribution forces plan-level step-back. That is why the report preserves the exact attribution and rationale instead of pretending the tool proved causality.

---

## F8 — threat model and counterclaim

**Concrete change:** Both become required structured JSON inputs. Honest-run cases are converted into claims by the tool, assessed by every reviewer, and cannot pass when unverifiable.

**Now refuses:**

- Missing threat model.
- Free-text threat-model placeholders.
- Missing trusted/hostile/out-of-scope structure.
- A free-text claim offered in place of an honest-run case.
- Honest-run cases without concrete preconditions, action, and expected result.

**Still cannot catch:** Whether the declared threat model is truthful or complete, or whether the selected honest run is representative. Those remain judgment.

**Can honest review fail?** Yes. Malformed files fail before contact, and an honest case for which the submitted code lacks evidence makes the review partial rather than passing.

---

## F10 — AST compatibility

**Concrete change:** String extraction uses `node.value` or `node.s`, not `ast.Constant` alone. Keep the Python 3.7 floor.

**Now refuses/catches:** The test fails only if it genuinely finds no literals, rather than because it used the wrong AST node class.

**Still cannot catch:** Runtime incompatibilities unique to Python 3.7 unless CI actually executes there.

**Can honest review fail?** No additional gate refusal.

---

## F11 — documentation contradiction

**Concrete change:** Documentation now says implementation escalation is optional and that rounds come from `--change`, matching the code.

**Now refuses:** Nothing; this is documentation accuracy.

**Still cannot catch:** Future drift without a documentation/CLI consistency test.

**Can honest review fail?** No.

---

## F12 — asymmetric uncertainty

The old model had only two practical destinations:

- Uncertainty about a defect was instructed to become a refutation, causing false failures.
- Unverifiable claims, low-confidence serious findings, and unclear scope could still end in a pass, causing false passes.

The counterclaim did not repair this because an `unverifiable` counterclaim had no effect on the state.

**Concrete change:** Uncertainty is now a mechanically represented third state:

- demonstrated defect/refuted claim → `REVIEW_FAIL`;
- supported review with quorum and no unresolved evidence → `REVIEW_PASS`;
- missing evidence, unverifiable claim, low-confidence serious concern, unknown scope, reviewer error, or sub-quorum result → `REVIEW_PARTIAL`.

Quorum is checked before issuing `REVIEW_FAIL`, so one completed reviewer is not called a committee merely because it found something.

**Still cannot catch:** A confident hallucinated failure scenario can still cause a false fail, and a shared confident blind spot can still pass. No model committee eliminates those.

**Can honest review fail?** A demonstrated reviewer refutation still can. Mere uncertainty no longer produces `REVIEW_FAIL`, but it does block `REVIEW_PASS` with `REVIEW_PARTIAL`. That is the symmetric failure mode: unresolved evidence is neither acquittal nor conviction.