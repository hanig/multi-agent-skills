# ARC-720 live positive evidence

Captured from the owner-supplied ARC-1097 artifacts on 2026-09-27:

- `context.txt` is the `--context` argument of `r3b-tests-retry.sh`, extracted
  with Python's `shlex.split(..., comments=True)`, plus a terminal newline.
- `dispositions.json` is a byte-for-byte copy of
  `r3b-tests-retry-dispositions.json`.

These are factual allegations and reproduction evidence, not a prior panel's
decision. Tests consume these fixtures without depending on the source
scratch directory or executing the commands in the context.

Captured SHA-256 values (provenance checks, not authority about the findings):

```text
b2270cbac931c798aeceef430b27798ca4ce9977cbee34171e0f90a8fe653308  context.txt
1bb5301769b27af44f90471b0afc35fd492896ef7b074834d90d9fa236aa4e1b  dispositions.json
```

The quoted offender from ARC-720 is `OFFENDER` in
`tests/test_arc720_contamination.py`. Every acceptance test first executes a
contaminated removal control. The live shell invocation itself is not run or installed.

`bom-annotation.txt` and `bom-receipt.json` are synthetic prefix-regression
fixtures, each with a literal UTF-8 BOM. The receipt is not an actual review
result; its indistinguishable typed envelope is deliberately stripped. Prefix
tests also cover all Python whitespace characters and mixed/repeated BOMs.

`past-limit-annotation.json` and `past-limit-receipt.json` specify generated
fixtures: newline padding through one byte beyond the read limit, followed
by the corresponding BOM fixture. Tests materialize both as real files,
including growth after the descriptor's size check. A 1 MiB test cap avoids
shipping or repeatedly allocating 64 MiB fixtures; the scratch reproduction
also measures the actual 64 MiB limit. Context cases inject argv in-process
and do not claim the host permits an exec argument that large.
