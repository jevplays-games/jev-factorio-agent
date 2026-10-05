# Explicit compatible-source persistent recovery (#265)

This opt-in migration deploys a persistence/controller fix whose decision
contract is identical to the checkpoint source. It does not grant another model
evaluation, change selection floors, replay a native operation, clear a provider
circuit, or adopt native infrastructure. Changed-contract decision reevaluation
remains a separate authorization path.

The existing supervisor must retain its original process/writer lock and provide
a private immutable authorization file, pinned by its actual file SHA-256 in the
reviewed launch. This is an application of supervisor authority, not a signature
generator: the supervisor must independently review and sign its full deployment
history, verify the pinned artifact, and retain that evidence. The source does
not certify an arbitrary digest as a cryptographic signature.

The authorization contains exactly:

- `schema: 1` and an unused ASCII `authorization_id`.
- Original `session_id`, `target`, exact starting `checkpoint_sha256`.
- `previous_source` and `current_source`, each containing full commit and
  source SHA-256, and the equal `decision_contract_sha256`.
- `scope`, produced by `compatible_recovery.scope` through the exact composed
  memory loader. It binds the complete decoded checkpoint state, full paid
  background job/attempt, archive pointer/count, stalled counter, all failure
  counters, history and outcome records. The exact checkpoint-byte digest also
  binds every field. Prior state is hashed, avoiding repeated copies of the
  entire archive or recursively growing compatibility history.
- `owner_invocation` with the original `run_id` and the new supervisor-pinned
  `segment_id`/`execution_id`; `supervisor_history_sha256` binds the externally
  reviewed signed deployment history. This history must establish the original
  invocation, previous source provenance and unchanged runtime/model/budget
  settings. Source ancestry alone is never permission.
- Absolute original `lock_path`, `provider_state_sha256` (or explicit absence)
  and `provider_identity_sha256` (or no HTTP circuit). The CLI checks the actual
  retained provider state and configured circuit identity before attachment.

Launch uses `--compatible-source-authorization FILE`,
`--compatible-source-authorization-sha256 SHA`, and
`--compatible-source-lock-fd FD`, together with the existing resumed live
hierarchical Jev/persistent-recovery options. The inherited POSIX descriptor
must identify the original private writer-lock file and already retain exclusive
flock. Linux `/proc/self/fdinfo` proves this specific open-file description has
the existing native whole-file FLOCK WRITE record, including after `pass_fds`
exec. An unlocked or independently opened descriptor rejects; the validator
never acquires a new lock or releases the supervisor's lock. Missing native
descriptor evidence fails closed.
Authorization files must be regular single-link owner files with mode 0400 on
POSIX. Descriptor/path metadata and exact bytes are checked during capture.
Provider sidecars and their immediate safety directory must also belong to the
current effective user with private permissions; identity/mode/metadata drift
during capture rejects even when the supplied byte digest matches.

Before any backend attachment or observation, the source verifies clean exact
Git ancestry, equal decision-contract blobs, and an unchanged AST-bound v1
selection fingerprint protocol and limits. The full composed checkpoint and
authenticated archive are validated. Foreground pending/attempt/plan/transfer
ambiguity and half background pairs reject. Potentially billed legacy records
without exact offered-batch coverage reject; a proven no-model frontier is
distinct from missing request evidence. The migration appends a durable exact
authorization preimage to `compatible_source_recoveries`, changes only the
current recovery source, and uses the existing atomic replacement/file/directory
fsync barrier. It leaves native assets, paid receipts, counters, provider state,
archive files and gameplay history intact. Appending to bounded gameplay history
would evict an old event and manufacture a different decision fingerprint, so
the lineage itself records the authorization.

Each historical attempt retains its actual source. For the current candidate
state the controller computes a separate source-bound fingerprint for every
member of the explicitly approved equal-contract lineage. Active and verified
archived offered batches aggregate across those aliases; the same candidate
cannot be offered again and the per-state batch limit applies to the aggregate.
A source outside the retained directed chain receives no compatibility credit.
Unresolved old batches remain unresolved waits. New rows carry the real new
source, rather than pretending the old executable generated them.

A failed or ambiguous save stops before attachment. Never retry the old exact
authorization blindly. If replacement completed, the retained checkpoint holds
the consumed authorization; ordinary same-source resume uses that state and its
original run identity. If it did not, reconcile storage and obtain reviewed
authority for the unchanged checkpoint. Provider-state drift across installation
also requires explicit reconciliation of the installed result. No controller,
craft or model request is started by the migration itself.

The migration proves the inherited writer lock again immediately before the
checkpoint installation and after its durability barrier. Losing ownership
during source or archive validation prevents installation; losing it after an
installation leaves a consumed checkpoint for explicit reconciliation and stops
before backend attachment. Authorization and provider captures also compare
ownership, permissions and link count across the read, in addition to their
byte and timestamp checks.

Offline receipt fixtures are not native acceptance. Closure still requires final
signed-head CI/independent review/protected merge, one-use reviewed deployment
under the original lock, fresh useful native progress and truthful OBS Program
verification. The current bootstrap deployment and its source/attachment pins
must not be changed to exercise this separate work.

## Recovery after consumed events leave rolling history

Long campaigns retain consumed source-reevaluation rows after the corresponding
events leave the 64-event gameplay-history cache. A v1 epoch witness still requires
those events in current history when authorizing a new migration. Do not insert
old events into that cache, discard consumed rows, or alter source hashes to make
a deployment pass.

The opt-in `jev.compatible-epoch-boundary.v2` witness instead includes each exact
original gameplay-journal record and its original prepared/result receipt bytes.
The supervisor must first authenticate the retained signed source and launch
chain, then sign the complete witness with the existing enrolled key. Each edge
binds the original run, execution, session, target, source, consumed event,
checkpoint admitted for reevaluation, launcher digest and prepared/result hashes.
The current durable consumed row remains required. A conflicting current-history
event rejects even when retained evidence exists. The witness binds the terminal
checkpoint, prior compatibility lineage and exact new authorization; it cannot
authorize another campaign or another checkpoint.

Capture the original journal bytes and receipts before signing. Store the journal
record as canonical base64 of zlib-compressed bytes, with its uncompressed SHA-256;
store prepared/result bytes as canonical base64. Verification limits compressed
records to 128 KiB, decoded records to 512 KiB, each launcher receipt to 16 KiB,
aggregate decoded records to 8 MiB and the signed body to 1 MiB. Duplicate JSON
keys, nonfinite values, truncated streams and trailing compressed data reject.
Missing or oversized evidence requires investigation, not a weaker witness.

This changes evidence retention only. The same enrolled signature, inherited
writer lock, clean source proof, equal current decision contract, exact checkpoint,
provider-state checks, atomic save and aggregate paid-attempt budget still apply.
Historical changed-contract epochs do not become aliases of the new epoch. The
validator reads retained evidence without rewriting gameplay history or issuing
any model request or native action. Retain the signed witness with the deployment
receipts and verify fresh native progress separately after installation.
