# Proposal: replace the action-naming lint with a refusal chokepoint

> **STATUS: NOT IMPLEMENTED. Committee verdict 2026-08-27: REVIEW_FAIL.**
> Direction endorsed, this specification rejected. Author: gpt-5.6-sol
> (xhigh, 27,970 reasoning tokens). Panel: deepseek-v4-pro, luna,
> kimi-k2.7-code (glm-5.1 errored). Sol excluded as author.
>
> **The diagnosis is right and worth keeping:** do not lint free English for
> imperative verbs. Make an omitted action *unrepresentable* rather than
> *detectable*, so the test checks architecture instead of wording. Five
> versions of the lint have now failed, four of them caught by reviewers and
> never by the test.
>
> **Five defects must be fixed before any of this is built.** Two were found
> independently by two reviewers each.
>
> 1. **CRITICAL (kimi-k2.7-code), MAJOR (luna), independently.** The design
>    requires `from cli_refusal import ...`. This repo installs skills
>    individually (`install.sh --only NAME`), so a skill cannot import from a
>    sibling and helpers are COPIED byte-identical. Inlining makes the
>    chokepoint's own `sys.exit` calls look like the forbidden direct exits
>    the test bans, and trips its missing-import check. A correct build fails.
>    Fix: the test must accept inlined definitions and exempt the chokepoint
>    itself by name.
> 2. **MAJOR (luna).** `exit_status(1)` is part of the proposal's own approved
>    API, takes only an integer, and terminates without any action. So
>    omission is NOT unrepresentable through the supported API, which is the
>    proposal's central claim. Sol's residual section does not mention it:
>    a fifth gap, unstated. Fix: remove the sink, or require an action.
> 3. **MAJOR (kimi-k2.7-code).** The test code uses `ast.Str`,
>    `ast.NameConstant` and `ast.Index`, all removed in Python 3.10. The repo
>    runs 3.13 (Mac), 3.12 (lambda), 3.10 (andromeda, chimera), so it would
>    abort everywhere before checking anything. Fix: `ast.Constant`.
> 4. **MAJOR (kimi-k2.7-code).** The validator rule rejects honest
>    composition: `return child_problem(value)` is flagged even though it
>    returns an action-carrying refusal. Refusing an honest pattern is as
>    serious here as passing a dishonest one. Fix: allow delegation to
>    another `*_problem`.
> 5. **MAJOR (kimi-k2.7-code).** `argparse.error(...)` and direct stderr
>    writes stay unblocked, so the rule is violated while the architecture
>    test is green. Sol lists this as a future channel needing "coding
>    policy"; kimi's point is that argparse is already present and reachable.
>
> Sequencing note, not part of the review: this is a ~43-site migration to
> two tools whose verifier logic has been clean across three consecutive
> committees, and an actionless refusal is a usability defect rather than a
> false pass. `hanig-reproducible-result` is unstarted, so the chokepoint can
> be designed into it from the beginning instead of retrofitted. That is
> Hani's call.

---

## Recommendation

Do not lint unrestricted English for imperative verbs. That has already failed repeatedly and cannot be made reliable with the stated constraints.

Use:

1. A shared, structured `Refusal` value that can only be created through a factory requiring a keyword-only `action`.
2. Typed sinks that accept only `Refusal`.
3. An AST test that checks architecture rather than wording:
   - no direct `sys.exit`, `SystemExit`, or `os._exit` in either verifier;
   - every `*_problem` / `*_fault` non-`None` return directly constructs a `Refusal`;
   - every construction supplies `action=`;
   - non-passing reason collections also contain only `Refusal` values.

This does not make a *useless* action impossible, but it makes an *omitted* action impossible through the supported API. The test no longer needs to understand natural language.

The migration is worthwhile despite the cost: approximately 43 exit sites, plus validator returns/callers and emitted-reason collection sites. That cost is finite; maintaining an English vocabulary indefinitely is not.

---

## 1. Shared refusal representation

Put this in one shared module used by both twins, for example `cli_refusal.py`. Do not copy it into both verifiers.

```python
# cli_refusal.py
"""Structured user-facing refusals shared by both CLI verifiers."""

import sys
from typing import Iterable, List, NoReturn, Tuple


_TOKEN = object()


def _text(label: str, value: str) -> str:
    if not isinstance(value, str):
        raise TypeError(
            "{} must be text; pass a string".format(label))
    value = value.strip()
    if not value:
        raise ValueError(
            "{} must not be empty; pass useful text".format(label))
    return value


def _details(values: Iterable[str]) -> Tuple[str, ...]:
    if isinstance(values, str):
        raise TypeError(
            "details must be an iterable of strings, not one string")
    return tuple(_text("detail", value) for value in values)


class Refusal:
    """An immutable problem paired with an explicitly supplied action.

    Construct with refusal(...), not by calling this class.
    """

    __slots__ = ("_problem", "_action", "_details")

    def __init__(
            self,
            token: object,
            problem: str,
            action: str,
            details: Tuple[str, ...]) -> None:
        if token is not _TOKEN:
            raise TypeError(
                "construct refusals with "
                "refusal(problem, action=...)")
        object.__setattr__(self, "_problem", problem)
        object.__setattr__(self, "_action", action)
        object.__setattr__(self, "_details", details)

    def __setattr__(self, name: str, value: object) -> None:
        raise AttributeError("Refusal values are immutable")

    @property
    def problem(self) -> str:
        return self._problem

    @property
    def action(self) -> str:
        return self._action

    @property
    def details(self) -> Tuple[str, ...]:
        return self._details

    def with_details(self, *values: str) -> "Refusal":
        return refusal(
            self._problem,
            action=self._action,
            details=self._details + _details(values))

    def render(self, *, details: Iterable[str] = ()) -> str:
        all_details = self._details + _details(details)

        lines = ["error: {}".format(self._problem)]
        for detail in all_details:
            lines.append("detail: {}".format(
                detail.replace("\n", "\n  ")))
        lines.append("action: {}".format(self._action))
        return "\n".join(lines)

    def __str__(self) -> str:
        # Accidental interpolation still retains the action.
        return self.render()


def refusal(
        problem: str,
        *,
        action: str,
        details: Iterable[str] = ()) -> Refusal:
    """The only supported Refusal constructor.

    `action` deliberately has no default and is keyword-only.
    """
    return Refusal(
        _TOKEN,
        _text("problem", problem),
        _text("action", action),
        _details(details))


def _require_refusal(value: Refusal) -> Refusal:
    # Exact type prevents a subclass from overriding render().
    if type(value) is not Refusal:
        raise TypeError(
            "expected a Refusal; create one with "
            "refusal(problem, action=...)")
    return value


def exit_refusal(
        value: Refusal,
        *,
        details: Iterable[str] = ()) -> NoReturn:
    value = _require_refusal(value)
    sys.exit(value.render(details=details))


def exit_status(status: int) -> NoReturn:
    """Exit with a machine status, never with human refusal text."""
    if type(status) is not int:
        raise TypeError(
            "exit_status requires an integer; use exit_refusal for text")
    sys.exit(status)


class RefusalList:
    """Collection used for refusal reasons embedded in result documents."""

    __slots__ = ("_items",)

    def __init__(self) -> None:
        self._items = []  # type: List[Refusal]

    def append(self, value: Refusal) -> None:
        self._items.append(_require_refusal(value))

    def extend(self, values: Iterable[Refusal]) -> None:
        for value in values:
            self.append(value)

    def rendered(self) -> List[str]:
        return [value.render() for value in self._items]

    def __len__(self) -> int:
        return len(self._items)

    def __bool__(self) -> bool:
        return bool(self._items)
```

The construction chokepoint is:

```python
refusal(problem, *, action, details=())
```

There is intentionally:

- no default action;
- no overload accepting a completed string;
- no `exit_refusal("some string")` compatibility path;
- no generic fallback action such as “fix the input”.

A compatibility path accepting plain strings would recreate the original defect.

---

## 2. Migration and composition

### Direct refusal

Before:

```python
sys.exit("error: 'threshold' must be a finite number")
```

After:

```python
exit_refusal(refusal(
    "'threshold' is not a finite number",
    action="Set 'threshold' to a finite JSON number and re-run the command.",
))
```

### Validators returning strings

Change them to return `Optional[Refusal]`, retaining their existing names:

```python
from typing import Optional

from cli_refusal import Refusal, refusal


def criterion_problem(value: object) -> Optional[Refusal]:
    if not isinstance(value, dict):
        return refusal(
            "the criterion is not a JSON object",
            action="Use a JSON object for the criterion.",
        )

    threshold = value.get("threshold")
    if not isinstance(threshold, (int, float)):
        return refusal(
            "'threshold' is not a number",
            action="Set 'threshold' to a finite JSON number.",
        )

    return None
```

The caller does not interpolate the result:

```python
problem = criterion_problem(spec)
if problem is not None:
    exit_refusal(
        problem,
        details=("received criterion: {!r}".format(spec),))
```

This replaces unsafe wrappers such as:

```python
sys.exit(f"error: malformed: {problem}")
```

There is no wrapper exemption. Context is added as detail while the original action remains structurally present.

If a validator delegates to another validator, return a freshly constructed refusal or return the delegated result only if the AST convention is relaxed accordingly. The strictest convention is clearer:

```python
problem = child_problem(value)
if problem is not None:
    return refusal(
        problem.problem,
        action=problem.action,
        details=problem.details + ("while checking child",))
```

### Non-passing result reasons

The first existing test shows that reasons embedded in result documents are also refusals. Do not leave those on the old substring heuristic.

Before:

```python
reasons = []
reasons.append("the evidence file is missing")
result["reasons"] = reasons
```

After:

```python
from cli_refusal import RefusalList, refusal

refusals = RefusalList()
refusals.append(refusal(
    "the evidence file is missing",
    action="Record the evidence file and run verification again.",
))

result["reasons"] = refusals.rendered()
```

Using `RefusalList` means an untested branch that attempts this:

```python
refusals.append("not a JSON object")
```

fails immediately instead of emitting an actionless refusal.

---

## 3. No-bypass AST test

The AST test should enforce the representation and sinks, not inspect English. Apply the same test loop to both twins.

The following is deliberately strict and false-alarm biased. It establishes these source conventions:

- both files import the same shared API without aliases;
- direct process exits are forbidden;
- `refusal(...)` always has an explicit `action=`;
- direct `Refusal(...)` construction is forbidden;
- every non-`None` validator return is a direct `refusal(...)`;
- embedded reasons use `RefusalList`;
- output dictionary fields named `"reasons"` use `.rendered()`.

```python
def test_all_refusals_use_the_shared_chokepoint(self):
    import ast

    SHARED = "cli_refusal"
    REQUIRED = {
        "Refusal",
        "RefusalList",
        "refusal",
        "exit_refusal",
        "exit_status",
    }
    VALIDATOR_SUFFIXES = ("_problem", "_fault")

    def literal_string(node):
        # ast.Str supports 3.7; ast.Constant supports newer versions.
        if isinstance(node, ast.Str):
            return node.s
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            return node.value
        return None

    def is_none(node):
        if node is None:
            return True
        if isinstance(node, ast.NameConstant):
            return node.value is None
        return isinstance(node, ast.Constant) and node.value is None

    def is_named_call(node, name):
        return (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == name
        )

    def is_refusal_call(node):
        return is_named_call(node, "refusal")

    def is_rendered_refusals(node):
        return (
            isinstance(node, ast.Call)
            and not node.args
            and not node.keywords
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "rendered"
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "refusals"
        )

    def direct_returns(fn):
        """Returns belonging to fn, excluding nested functions/classes."""
        found = []

        class Finder(ast.NodeVisitor):
            def visit_Return(self, node):
                found.append(node)

            def visit_FunctionDef(self, node):
                return

            def visit_AsyncFunctionDef(self, node):
                return

            def visit_ClassDef(self, node):
                return

        finder = Finder()
        for statement in fn.body:
            finder.visit(statement)
        return found

    def target_names(target):
        if isinstance(target, ast.Name):
            yield target.id
        elif isinstance(target, (ast.Tuple, ast.List)):
            for item in target.elts:
                for name in target_names(item):
                    yield name

    def subscript_key(node):
        if not isinstance(node, ast.Subscript):
            return None
        index = node.slice
        # Python 3.7 wraps the index in ast.Index.
        if isinstance(index, ast.Index):
            index = index.value
        return literal_string(index)

    for path in (WORKFLOW, TRAINING):
        with self.subTest(path=path.name):
            source = path.read_text(encoding="utf-8")
            tree = ast.parse(source, filename=str(path))
            errors = []

            def error(node, message):
                errors.append("{}: {}".format(
                    getattr(node, "lineno", "?"), message))

            # Require exactly the same shared primitive in both twins.
            imported = set()
            for node in tree.body:
                if not isinstance(node, ast.ImportFrom):
                    continue
                if node.module != SHARED:
                    continue
                for alias in node.names:
                    if alias.asname is not None:
                        error(node, "do not alias {}".format(alias.name))
                    else:
                        imported.add(alias.name)

            missing = REQUIRED - imported
            if missing:
                errors.append(
                    "missing shared imports: {}".format(
                        ", ".join(sorted(missing))))

            # Do not permit protected names to be imported from somewhere
            # else or rebound locally.
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom):
                    for alias in node.names:
                        bound = alias.asname or alias.name
                        if bound in REQUIRED and not (
                                node.module == SHARED
                                and alias.asname is None
                                and alias.name == bound):
                            error(
                                node,
                                "{} must come unaliased from {}".format(
                                    bound, SHARED))

                if isinstance(node, ast.Name) and \
                        isinstance(node.ctx, ast.Store) and \
                        node.id in REQUIRED:
                    error(node, "do not rebind {}".format(node.id))

                if isinstance(
                        node, (ast.FunctionDef,
                               ast.AsyncFunctionDef,
                               ast.ClassDef)):
                    if node.name in REQUIRED:
                        error(node, "do not redefine {}".format(node.name))

                if isinstance(node, (ast.FunctionDef,
                                     ast.AsyncFunctionDef)):
                    args = (
                        list(node.args.args)
                        + list(node.args.kwonlyargs)
                    )
                    if node.args.vararg is not None:
                        args.append(node.args.vararg)
                    if node.args.kwarg is not None:
                        args.append(node.args.kwarg)
                    for arg in args:
                        if arg.arg in REQUIRED:
                            error(
                                arg,
                                "do not shadow {}".format(arg.arg))

            # Discover aliases for forbidden process-termination APIs.
            sys_modules = {"sys"}
            os_modules = {"os"}
            builtins_modules = {"builtins"}
            direct_exit_names = {"exit", "quit"}
            system_exit_names = {"SystemExit"}
            os_exit_names = set()

            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        bound = alias.asname or alias.name.split(".")[0]
                        if alias.name == "sys":
                            sys_modules.add(bound)
                        elif alias.name == "os":
                            os_modules.add(bound)
                        elif alias.name == "builtins":
                            builtins_modules.add(bound)

                elif isinstance(node, ast.ImportFrom):
                    for alias in node.names:
                        bound = alias.asname or alias.name
                        if node.module == "sys" and alias.name == "exit":
                            direct_exit_names.add(bound)
                        elif node.module == "os" and \
                                alias.name == "_exit":
                            os_exit_names.add(bound)
                        elif node.module == "builtins":
                            if alias.name in ("exit", "quit"):
                                direct_exit_names.add(bound)
                            elif alias.name == "SystemExit":
                                system_exit_names.add(bound)

            factory_calls = 0
            sink_calls = 0
            validator_count = 0
            refusal_list_count = 0

            for node in ast.walk(tree):
                # Ban references as well as calls, so
                # `die = sys.exit; die(...)` is also rejected.
                if isinstance(node, ast.Attribute) and \
                        isinstance(node.value, ast.Name):
                    base = node.value.id
                    if base in sys_modules and node.attr == "exit":
                        error(node, "use exit_refusal or exit_status")
                    if base in os_modules and node.attr == "_exit":
                        error(node, "use exit_refusal or exit_status")
                    if base in builtins_modules and \
                            node.attr in ("exit", "quit", "SystemExit"):
                        error(node, "use exit_refusal or exit_status")

                if isinstance(node, ast.Name) and \
                        isinstance(node.ctx, ast.Load):
                    if node.id in direct_exit_names or \
                            node.id in system_exit_names or \
                            node.id in os_exit_names:
                        error(node, "direct process exit is forbidden")

                if not isinstance(node, ast.Call):
                    continue

                # Standardise on direct imports rather than module-qualified
                # calls or aliases.
                if isinstance(node.func, ast.Attribute) and \
                        node.func.attr in REQUIRED:
                    error(
                        node,
                        "call the unaliased shared {} function/type".format(
                            node.func.attr))

                if isinstance(node.func, ast.Name) and \
                        node.func.id == "Refusal":
                    error(
                        node,
                        "construct with refusal(problem, action=...)")

                if is_refusal_call(node):
                    factory_calls += 1
                    if any(keyword.arg is None
                           for keyword in node.keywords):
                        error(
                            node,
                            "do not hide refusal arguments in **kwargs")

                    actions = [
                        keyword for keyword in node.keywords
                        if keyword.arg == "action"
                    ]
                    if len(actions) != 1:
                        error(
                            node,
                            "refusal() requires one explicit action=")
                    else:
                        text = literal_string(actions[0].value)
                        if text is not None and not text.strip():
                            error(node, "action= must not be blank")

                if isinstance(node.func, ast.Name) and \
                        node.func.id == "exit_refusal":
                    sink_calls += 1
                    if len(node.args) != 1:
                        error(
                            node,
                            "pass exactly one Refusal to exit_refusal")
                    elif literal_string(node.args[0]) is not None or \
                            isinstance(
                                node.args[0],
                                (ast.JoinedStr, ast.BinOp)):
                        error(
                            node,
                            "exit_refusal does not accept assembled text")

                if isinstance(node.func, ast.Name) and \
                        node.func.id == "exit_status":
                    if len(node.args) != 1:
                        error(
                            node,
                            "pass exactly one integer to exit_status")

            # Every validator refusal is visibly constructed at the return.
            # This intentionally rejects `return message`, delegation, and
            # condition expressions; rewrite those as explicit branches.
            for fn in ast.walk(tree):
                if not isinstance(
                        fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                if not fn.name.endswith(VALIDATOR_SUFFIXES):
                    continue

                validator_count += 1
                for returned in direct_returns(fn):
                    value = returned.value
                    if is_none(value):
                        continue
                    if not is_refusal_call(value):
                        error(
                            returned,
                            "{} may return only None or "
                            "refusal(..., action=...)".format(fn.name))

            # Replace the old reasons.append wording lint with a typed
            # refusal collection.
            for node in ast.walk(tree):
                if isinstance(node, ast.Name) and node.id == "reasons":
                    error(
                        node,
                        "use `refusals = RefusalList()`; reserve "
                        "'reasons' for the output dictionary key")

                if isinstance(node, ast.Assign):
                    names = set()
                    for target in node.targets:
                        names.update(target_names(target))
                    if "refusals" in names:
                        if not is_named_call(node.value, "RefusalList"):
                            error(
                                node,
                                "initialize refusals with RefusalList()")
                        else:
                            refusal_list_count += 1

                    for target in node.targets:
                        if subscript_key(target) == "reasons" and \
                                not is_rendered_refusals(node.value):
                            error(
                                node,
                                "write refusal output with "
                                "refusals.rendered()")

                elif isinstance(node, ast.AnnAssign):
                    names = set(target_names(node.target))
                    if "refusals" in names:
                        if not is_named_call(node.value, "RefusalList"):
                            error(
                                node,
                                "initialize refusals with RefusalList()")
                        else:
                            refusal_list_count += 1

                    if subscript_key(node.target) == "reasons" and \
                            not is_rendered_refusals(node.value):
                        error(
                            node,
                            "write refusal output with "
                            "refusals.rendered()")

                elif isinstance(node, ast.AugAssign) and \
                        "refusals" in set(target_names(node.target)):
                    error(
                        node,
                        "do not mutate RefusalList with +=")

                elif isinstance(node, ast.Dict):
                    for key, value in zip(node.keys, node.values):
                        if literal_string(key) == "reasons" and \
                                not is_rendered_refusals(value):
                            error(
                                value,
                                "'reasons' must be refusals.rendered()")

                elif isinstance(node, ast.Call) and \
                        isinstance(node.func, ast.Attribute) and \
                        isinstance(node.func.value, ast.Name) and \
                        node.func.value.id == "refusals":
                    if node.func.attr == "append":
                        if len(node.args) != 1 or node.keywords:
                            error(
                                node,
                                "RefusalList.append takes one Refusal")
                        elif literal_string(node.args[0]) is not None or \
                                isinstance(
                                    node.args[0],
                                    (ast.JoinedStr, ast.BinOp)):
                            error(
                                node,
                                "append a Refusal, not assembled text")
                    elif node.func.attr not in (
                            "extend", "rendered"):
                        error(
                            node,
                            "unsupported RefusalList method {}".format(
                                node.func.attr))

            # These are structural sanity checks, not magic coverage floors.
            if factory_calls == 0:
                errors.append("no refusal() calls found")
            if sink_calls == 0:
                errors.append("no exit_refusal() calls found")
            if validator_count == 0:
                errors.append("no *_problem/*_fault validators found")
            if refusal_list_count == 0:
                errors.append("no RefusalList construction found")

            self.assertFalse(
                errors,
                "{} refusal architecture violations:\n  {}".format(
                    path.name, "\n  ".join(errors)))
```

Also test the shared primitive itself:

```python
def test_refusal_primitive_enforces_its_contract(self):
    from cli_refusal import (
        RefusalList, exit_refusal, refusal)

    with self.assertRaises(TypeError):
        refusal("bad input")

    with self.assertRaises(ValueError):
        refusal("bad input", action="   ")

    value = refusal(
        "the input is invalid",
        action="Edit the input and run the command again.")

    rendered = value.render()
    self.assertIn("error: the input is invalid", rendered)
    self.assertIn(
        "action: Edit the input and run the command again.",
        rendered)

    with self.assertRaises(TypeError):
        exit_refusal("error: actionless")

    reasons = RefusalList()
    with self.assertRaises(TypeError):
        reasons.append("not a JSON object")

    with self.assertRaises(SystemExit) as raised:
        exit_refusal(value)
    self.assertEqual(raised.exception.code, rendered)
```

This replaces both current wording tests. There are no content filters, quote recovery, length floors, wrapper exemptions, or verb lists.

---

## 4. If a lint-only solution were retained

There is no reliable vocabulary-free rule that determines whether unrestricted English describes a genuine action. Neither AST nor regex can distinguish:

- “the program cannot use this value” from
- “use a finite value instead”

without understanding grammar and context.

The only workable vocabulary-free lint rule is a structural schema such as:

```text
error: <problem>
action: <non-empty text>
```

and an anchored check for a non-empty `action:` field. But once that field is represented separately, the factory above is strictly better than recovering it from source literals.

Therefore the appropriate lint rule is effectively:

> Every refusal expression must be `refusal(problem, action=...)`.

That is architectural lint, not natural-language lint.

---

## Residual risk

The residual must be stated narrowly:

1. **A useless but non-empty action still gets through.**

   Examples:

   ```python
   action="Do something."
   action="The value is invalid."
   action=str(exception)
   ```

   This would still violate the product rule. It is not acceptable behavior; it is merely unavoidable for automatic enforcement while `action` remains free-form natural language. Review must judge action quality.

2. **A new refusal channel can bypass the architecture.**

   Examples include writing directly to `stderr`, a new `argparse.error` path, logging followed by `exit_status(1)`, a custom `SystemExit` subclass, or deliberate indirection around the AST test. Those should be prohibited by coding policy and added to the structural denylist if introduced.

   For normal, non-adversarial repository changes, requiring both twins to use one shared module and banning their current direct channels is an acceptable engineering boundary. It is not a formal proof over all possible Python behavior.

3. **Passing the wrong dynamic value becomes a programming error.**

   For example:

   ```python
   problem = some_untyped_function()
   exit_refusal(problem)
   ```

   If `problem` is a string, the program raises `TypeError` rather than emitting an actionless refusal. That is still a defect, but it fails loudly instead of silently violating the user-message rule.

The important change is that ordinary omission becomes a test failure or runtime type failure, while poor action wording becomes explicit in an `action=` field for reviewers.