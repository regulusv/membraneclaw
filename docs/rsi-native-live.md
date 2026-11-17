# Native RSI with live Gemini and trace verification

The `live-run` command uses the existing RSI `Controller` and `Journal`, with
real Solver and Proposer components invoking `AgentRuntime -> ModelGateway ->
GeminiAdapter -> KeyDeskTransport`. Evaluation remains deterministic numeric
scoring of synthetic tasks. No fixture Solver or fixture Proposer participates.

## Run

```sh
python -m membraneclaw_runtime.rsi.cli live-run --output var/rsi/NEW-RUN
```

The credential stays in Key Desk. This command requires an external bridge
operator (such as the connected ChatGPT agent) to service the request mailbox:

1. Read each new `bridge/<request_id>.request.json` only after publication.
   Publication follows the native `attempt.started` durable commit.
2. Claim it once with a durable local marker. Call Key Desk `gemini_call` with
   the exact fields plus the saved Google credential ID. Use the same request
   ID when inspecting an uncertain call with `gemini_trace`; never send a new
   generation for an uncertain outcome.
3. Atomically install the complete receipt at
   `bridge/<request_id>.response.json` with owner-only permissions.
4. The transport checks request ID, terminal state, method, URL and exact JSON
   body equality, then captures the remote response before provider parsing.

Only single-turn text requests to `gemini-3.1-flash-lite`, one candidate, and up
to 1,024 output tokens are enabled in this bridge. The adapter's new model
profile intentionally rejects tools and reasoning options. It does not inherit
the old model's thinking-budget assumptions. Native HTTP transport, streaming
and tool continuation are not exercised by this text-only run.

The pinned limits are 30 component operations, one search round and two hours
for the search. Each model operation permits one request, no automatic retries,
and 15 minutes for the mailbox response. This longer wait includes operator
dispatch time, not just provider latency. Tokens are observed separately from
component-operation units; no global dollar/token cap is claimed.

## Trace and result association

`traces/` contains the original RSI journal. `model-traces/` contains native
runtime episodes. Before dispatch, the RSI operation stores its child episode
key; that episode's genesis pins the RSI operation ID, full request digest,
version ID, task ID, run ID and external request ID. Completed component results
pin the child episode's immutable head hash. Evaluation operations refer to the
same version, task and seed; promotion decisions retain the paired scores.

```sh
python -m membraneclaw_runtime.rsi.cli verify --run-dir var/rsi/NEW-RUN
python -m membraneclaw_runtime.rsi.cli report --run-dir var/rsi/NEW-RUN
python -m membraneclaw_runtime.rsi.cli final-evaluate \
  --run-dir var/rsi/NEW-RUN --output var/rsi/NEW-FINAL \
  --operations 2 --units 2 --wall-seconds 1800
```

Final evaluation also needs mailbox servicing. It freezes the search's selected
version and records the parent journal head. Verification checks native trace
integrity, operation/request provenance, recorded request text, answer/proposal
equality, pinned episode heads and recomputed promotion decisions. It does not
reissue model calls. Completed-run resume also dispatches nothing.

For network-free execution replay, use the existing
`membraneclaw_runtime.tracing.replay.execute_replay(store, episode)` with a
`FileTraceStore` rooted at `model-traces`. The generic immutable episode metadata
still says `replay=unsupported`; successful execution-replay evidence is recorded
separately rather than rewriting the original episode.

Started/unknown live operations block ordinary resume. `--recompute-offline` is
rejected for live runs. No automatic late-receipt reconciliation is implemented;
retain the evidence and resolve the uncertain attempt before any new dispatch.

## Observed execution: 2026-11-16 PT

Search: `var/rsi/native-gemini-20261117-02` (seven model calls).
Frozen final evaluation: `var/rsi/native-gemini-20261117-02-final` (one call).

- All eight calls returned HTTP 200 through the native runtime.
- All eight native episodes have complete capture and known outcomes.
- Baseline and candidate validation: 2/2 each. Candidate rejected for no gain.
- Frozen final test: 1/1. Total observed usage: 1,434 tokens.
- Native execution replay passed for all eight episodes without network access.
- Sealed snapshots validated; completed resume produced no new journal commits.
- Evidence: `native-verification.json` in the search directory, plus both reports.

An earlier native attempt (`native-gemini-20261117-01`) timed out waiting for the
bridge receipt. It remains failed with partial capture and unknown outcome; a
late successful receipt was retained without rewriting the failed native trace
or automatically retrying that request. Its usage is excluded from 1,434 above.

Validation: 48 RSI tests, 14 Gemini contract tests, 41 gateway tests and five
runtime replay tests passed. A broader 75-test provider suite had 10 errors from
the sandbox forbidding local TCP listeners, and three opt-in live tests skipped.
Those tests are not reported as passing.

Capture boundary: canonical submitted JSON and Key Desk's returned response
text. Local code does not observe Google's socket bytes or response headers.
The complete label applies to that declared boundary, not hidden model state.
The benchmark is tiny and synthetic; this establishes integration, not model
improvement or production readiness.
