# Architecture

Use a three-layer architecture for every live competition.

## Memory Layer

Memory is the source of truth. It must be stored in files that survive context
loss, process restarts, and agent handoffs.

Required files:

- `STATE.md`: current stage, best score, active run, next action, and blockers.
- `ideas_backlog.md`: candidate ideas with priority and status.
- `experiment_ledger.jsonl`: append-only experiment evidence.

Recommended directories:

- `runs/`: one isolated directory per experiment run.
- `reports/`: validation, reflection, and gate reports.
- `flags/`: small files written by detection checks.
- `submissions/`: candidate files and provenance records.

## Detection Layer

Detection turns raw events into explicit signals. Detection scripts should write
small flag files instead of taking large actions themselves.

Common flags:

- `EXPERIMENT_DRY.flag`: the current direction is no longer yielding progress.
- `SUBMIT_READY.flag`: a candidate passed automated checks and needs approval.
- `VALIDATION_FAILED.flag`: the latest run is untrustworthy.
- `RESEARCH_REFILL.flag`: the backlog needs new evidence-backed ideas.

## Decision Layer

The orchestrator reads memory and flags, chooses exactly one action, writes back
state, then stops or waits for the next wake-up.

Priority order:

1. If submission is ready, run the submission gate and request human approval.
2. If a run finished, validate it and append the ledger.
3. If the backlog has a high-priority todo idea and compute is clean, run it.
4. If the direction is dry, reflect and refill ideas from research.
5. Otherwise stay idle and record the next check.

## One-Action Rule

Execute one meaningful action per wake-up. Do not train, evaluate, research,
and submit in one uninterrupted chain. This rule makes failures inspectable and
prevents an agent from compounding bad assumptions.
