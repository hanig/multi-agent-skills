# D5 U3b store integration test index

Methods are in `test_d5_store_gate.py` unless a module is named.

| Group / name fragment | Contract and mutation |
| --- | --- |
| `S1_U3_M1`, `S2_U3_M2`, `S3_other`, `named_acquisition_order`, `check_retains_start` | C1: START before STATE and through invalidation; S1–S3 |
| `read_progresses`, `state_only`, `nested_named`, `threads_share` | C1: STATE-only read and same-record reentrancy |
| `claim_replace_wait`, `revoke_and_check_do_not_wait` | C1: exclusive DRAIN outside START for claim/replace; store T1/T2 |
| `fake_lock`, `store_preserves`, `symlinked_record` | C2 exact refusals and C2-FS cause |
| `nested_liveness`, `read_only_liveness`, `host_liveness`, `callback_bytes`, `review_callback_restore`, `review_inplace` | C2(b) observation guard; CONTENT-ONLY-GUARD, NO-CTIME |
| `pre_u3b_differential`, `C3_refused_files`, `review_long_path`, `initial_drain` | C3 successes and refused file sets; divergence 1 |
| `C3_declared_delta_0400`, `review_privileged_open` | Divergence 2: owner-mode repair, including privileged oracle |
| `C0_permission_matrix`, `C0_foreign_owner`, `C0_precedes_create` | E/C0, divergence 3; SKIP-ENVELOPE |
| `review_swapped_foreign`, `pin_verifies_identity` | OD14: checked identity and E verified after pin, before locks; SKIP-PIN-VERIFY |
| `review_C0_precedes_unavailable` | OD16: either required refusal; no lock creation or record change |
| `review_low_descriptor`, `record_descriptors` | C3: snapshot closes before callback/reopen; HOLD-SNAPSHOT-FD |
| `C2FS_denied_lock_creation` | C2-FS: original acquisition denial; FALLBACK-ENOENT |
| `lock_refusal_survives`, `post_open_error`, `test_d5_authority_store.py` cleanup/durability tests | C4 and C2-FS: refusal/cause survives cleanup |
| `test_d5_locks.py::test_DISCARD_CLEANUP_ERROR_preserves_post_open_refusal` | C4: suppressed diagnostics retained; DISCARD-CLEANUP-ERROR |
| `review_read_with_two_descriptor_slots`, `review_symlink_with_invalid_record` | Preserved failing C3(i) and C2(c) regressions; NOT READY |

Outside E: permission/foreign-owner/nondirectory fixtures pin C0.
Pin swaps/metadata changes require OOE categories 1/2 (OD14).
`review_C0_precedes_unavailable` is category 3: OD16 accepts either refusal.
Callback writes exercise the retained unsupported-callback guard.
