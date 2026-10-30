# AI Closed-Loop Rollout

Updated: 2026-07-27

## Contract

The LLM is a strategic planner. It never proves that an in-game action happened.
The deterministic executor owns legality, queueing, and safety. A reconciler owns
completion. Long-term memory may use only reconciled evidence.

```
observation -> policy -> command -> receipt -> reconciliation -> lesson
```

Every command/receipt must carry:

- `policy_activation_id`
- `command_id`
- `action`
- `planet_id` and coordinates when planet-scoped
- one lifecycle status
- a machine-readable reason
- timestamps and the observed before/after values when available

Lifecycle statuses are:

```
planned -> accepted/queued -> completed
                    \-> stalled/failed
planned -> skipped/failed
```

`accepted` and `queued` must never be described as `completed`.

## Colony service levels

- A zero-mine colony is critical.
- A planet below 80% production is critical.
- A planet below 100% production is warning.
- A critical planet must be selected on the next account tick when its queues
  and safety constraints permit.
- No owned colony may starve for more than three account ticks.
- Solar satellites are sized from the observed energy gap and per-unit output;
  fixed batches are only lower/upper safety bounds.
- Explicit structured development has authority. An absent or empty explicit
  command falls back to the safe colony manager.

## Memory rules

- The model cannot edit the memory store directly.
- Free-form policy prose is not evidence.
- Raw receipts are immutable evidence.
- One policy activation is one episode.
- Lessons are dimension-scoped: raid outcomes can evaluate only raid controls,
  economy outcomes only economy controls.
- Defensive losses are not raid losses.
- Promotion requires three independent eligible episodes without contradictory
  evidence.
- Historical lessons produced by an older attribution formula are quarantined,
  not silently trusted.

## Model-call gate

Skipping a model call is allowed only when all of the following are true:

- the current policy is valid and fresh;
- the material state fingerprint is unchanged;
- no critical/warning colony anomaly exists;
- no new rejection, failure, loss, ambiguous result, or diplomacy event exists;
- no pending command is stalled;
- the gate itself completed successfully.

Any missing or malformed state fails open and calls the model.
A known, unchanged moon-observation gap may be tolerated only while every
moon-scoped command list is empty; adding a moon command restores fail-open
model calls. A forced model review still occurs at least once every six hours.
When a call is required, its default reasoning effort is `medium`; the gate,
rather than low-effort reasoning on every interval, controls token use.

## Rollout

1. Unit and replay tests.
2. Shadow audit across all 12 AI accounts.
3. Canary one managed AI receipt against its authoritative unit queue.
4. Expand the deterministic executor to all 12; keep model policy generation
   gated independently per account.
5. Continue observing two complete policy cycles for receipt, memory, queue,
   restore, and model-gate errors.

Rollback is process restart with the previous code; state changes must remain
backed up and receipts remain append-only.

## Baseline and acceptance

Baseline captured after the legacy colony fallback repair:

- planets: 58
- negative-energy/throttled planets: 16
- zero-mine planets: 0
- stored action outcomes: 1,917
- development outcomes: 0
- detected memory artifacts: 6

Deployment checkpoint:

- full test suite: 538 passed
- development receipts observed after deployment: 52, with coverage on all 12
  managed AI accounts
- canary reconciliation: three prior machine targets transitioned to `completed`;
  the next three activation-linked targets remained correctly `queued` while
  matching queue entries were active
- defense-only raid observations quarantined: 265
- detected memory artifacts after backed-up cleanup: 0
- live universe process restarted under the existing watchdog configuration

Old development receipts without machine-readable targets remain pending rather
than being guessed complete. A queued target becomes `stalled` only after twelve
hours with a fresh snapshot, no matching active queue, and the target still
unmet.

Acceptance:

- development receipt coverage: at least 99%
- unmatched or ambiguous completed commands: below 1%
- memory artifacts created by the new pipeline: 0
- colonies unvisited for more than three account ticks: 0
- stable-state model-call reduction: at least 70%
- effective no-op model calls: below 10%
