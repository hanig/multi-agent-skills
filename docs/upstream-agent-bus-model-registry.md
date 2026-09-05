# Upstream report: model-registry failures look like absence

## Provenance and local status

`bin/bus` was vendored from Shreshth's `multi-agent-skills` archive dated
2026-08-25. The archive records commit
`a96b951e55ca632c561ff832d67ca3853cc2c62b`, but it contains no remote URL and
the upstream repository is not available to this checkout's GitHub identity.

ARC-270 therefore uses the repository's tracked-local-patch fallback. The
functional contract is pinned by `tests/test_bus_models.py`. During a future
upstream resync, do not overwrite this change silently: either retain the
patch, or replace it with an upstream version that passes those tests.

## Defect

The archived `load_models_registry` caught `OSError` and `JSONDecodeError`
together and returned an empty list. `bus models` consequently described all
of these states as a missing registry:

- the file does not exist;
- the path exists but cannot be read;
- the JSON is malformed;
- the document has the wrong shape; or
- the registry deliberately contains no models.

At import time, the command also creates the bus state directories and cache,
but it does not create `models.json`. That directory initialization is an
intentional, separately tested runtime contract; its presence is not evidence
that a registry was provisioned. The old error blurred that boundary by calling
an unreadable or corrupt file missing too.

## Local correction

The local patch reports absence, read failure, malformed JSON, invalid schema,
and an empty model list separately. It does not create `models.json`: the
registry remains hand-maintained routing input, and neither the bus nor this
repository's skill installer owns its contents.

The bus continues to initialize its owner-only runtime directories on every
invocation. It now says plainly when the separately managed registry is absent,
instead of asking callers to infer provisioning from directory presence.
