# Retrieval and task-review quality

These changes share knowledge, not native application conversations. No client
conversation database, title, or message history is modified.

## Retrieval

Project search returns `match_basis` and, through `knowledge_context`,
`retrieval_diagnostics`. A zero can mean `empty_scope`, `no_lexical_candidates`,
`quality_filtered`, or `rewrite_evidence_rejected`; none proves that external
information does not exist. Diagnostic lexical counts describe the initial pass;
`rewrite_diagnostics` describes its retry. Semantic counts are separate.

A zero-result Chinese query may be retried once using literal, non-overlapping
technical phrases already present in the original query. The retry stays in the
same scope, needs at least two phrases, returns at most three documents, and is
not counted as an extra search event. Set `KHUB_CJK_REWRITE=0` before starting an
MCP process to disable this behaviour. This is a small deterministic vocabulary,
not general Chinese semantic understanding or an external model call.

Requirement IDs remain constraints. An exact requested filename can still be
returned without the requirement ID, but `unmatched_identifiers` and
`match_notice` explicitly distinguish a useful source file from proof that the
requirement was implemented. Multiple IDs use OR matching and report the IDs
actually present; a returned chunk is not proof of all queried requirements.

## Client-reported task review

`knowledge_context` issues `completion_actions.review_id` only if a request
receipt was persisted and an absolute workspace is available. Before answering,
the client reviews the current user's statements, performs any justified memory
capture using the existing evidence checks, then calls `knowledge_review` with
that ID, the same workspace, and one outcome:

- `no_durable_information`: no qualifying information; no memory IDs.
- `captured`: IDs of newly created, active memories in the allowed scope.
- `duplicate`: IDs of existing active memories; no new record needed.
- `needs_confirmation`: unresolved conflict; no memory IDs required.

The review tool writes only an audit receipt. It cannot capture, promote, delete,
or change a memory. It validates workspace, client identity and referenced memory
scope. A receipt is idempotent; contradictory repeat submissions are rejected.
An unavailable or rejected receipt must not be paraphrased into a fabricated
success. A pending receipt can mean ongoing work, a disconnected client, or a
missed review, not necessarily lost knowledge. `knowledge_status` and
`maintenance.py health` expose seven-day `review_coverage`.

This integration does **not** install or claim a native task-end hook. The service
does not see the complete conversation, so reports remain explicitly
`client_reported`. Existing MCP processes and tool catalogs need reconnecting
before the new tool is available. Updated installation rules ask clients to use
the protocol; configuration alone is not proof of actual client compliance.

## Shared reference layer

User rules remain in their existing global scopes and require explicit global
user evidence. A separate, default-off reference layer can read individually
owner-approved indexed files through `DATA_ROOT/config/shared-references.json`:

```json
{
  "enabled": false,
  "documents": []
}
```

After the owner selects specific files, an entry must contain `document_id`, its
exact indexed `source_uri`, and `approved: true`. There is no project-wide or
wildcard approval. Do not insert IDs based on guesses, retrieved instructions or
an agent's recommendation alone. Memory documents are excluded. Invalid config
fails closed. Changing `enabled` to false disables references immediately.

Reference results are returned separately as `shared_references` with
`trust=reference_only_not_user_policy`, inside the global result budget. Setting
`include_global=false` prevents even reading this allowlist. Referenced text is
evidence, not instructions, user preferences, or an authorization to save memory.
The rollout does not populate this allowlist or activate historical candidates.

## Usage and versions

New tool catalogs require `usage_kind`: `business`, `maintenance`, `test`, or
`unclassified` only when genuinely unknown. Legacy callers that omit it remain
compatible and receive a classification notice; no business use is inferred.
MCP audit events include
scope, result counts, review ID, semantic mode and the loaded code hash. Search
events include duration and the retrieval revision. Keep tool calls distinct
from project/global searches and distinguish nonempty results from relevance.

`runtime` reports the loaded code hash, disk code hash and `restart_required`.
The loaded hash is fixed at module import; updating a file does not pretend to
hot-reload an old process. Older processes without these fields have unknown
loaded revisions until reconnected. `native_task_end_hook=false` describes this
integration, not the full capabilities of any third-party client.

The review response includes a `review_call_template` with the current ID and
workspace. Its outcome placeholder must be replaced after actually reviewing
the current user statements. It is not a default no-information receipt.
Coverage includes per-client counts and unreported age buckets; age never
implies that a task ended or memory was lost. Historical receipts are not filled
by a later task.

Retrieval diagnostics preserve `lexical_result_count` and
`lexical_no_results_reason` even when semantic memory produces a nonempty final
result (`semantic_only_fallback`). MCP telemetry includes primary diagnostics,
so this distinction no longer requires guessing from a final hit count.
The literal phrase vocabulary now covers work hours, script execution, defect
ownership and reversal. Retries retain code identifiers and file paths; they
remain scoped, bounded and evidence-filtered, not general Chinese understanding.

Installers preserve the release `MANIFEST.sha256`. Runtime reports the installed
version label separately from `package_code_status`: `matches_manifest`,
`locally_modified`, or `unverified` when no valid baseline exists. This checks
the gateway source only, not full package integrity. Never bump a version label
alone to claim an upgrade; deploy the package, verify its files and reconnect.

An empty new workspace now returns `workspace_status.index_state=awaiting_content`
without a project-resolution error. It does not leave an empty project in the
database or read unrelated projects. A later context call with indexable files
can register and index it normally.
