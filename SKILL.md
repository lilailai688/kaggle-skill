---
name: kaggle-skill
description: Memory-gated Kaggle competition research workflow for Codex. Use when starting or running a Kaggle competition, setting up isolated experiment workspaces, maintaining STATE/ideas/ledger memory, deciding whether a direction is dry, adding reflection and research refill loops, checking submission gates, or requiring human approval before leaderboard submission.
---

# Kaggle Skill

Use this skill to run Kaggle competitions as a disciplined research system rather
than a pile of ad hoc notebooks. The workflow is built around memory, gates,
reflection, dry-stop detection, and explicit human approval for risky actions.

## Operating Loop

1. Build a competition profile: slug, task, metric, data shape, rules,
   submission format, compute limits, and known public baselines.
2. Bootstrap an isolated workspace with `scripts/create_competition_workspace.py`.
3. Keep durable memory current:
   - `STATE.md` for current stage, best scores, active run, and next action.
   - `ideas_backlog.md` for hypotheses and research tasks.
   - `experiment_ledger.jsonl` for every experiment, result, and conclusion.
4. Execute one action per wake-up: profile, research, run experiment, validate,
   reflect, refill backlog, or request submission approval.
5. Run experiments in isolated run directories. Never reuse outputs across
   experiments unless the ledger records the dependency.
6. Validate results before trusting them. Reject runs with broken logs, missing
   outputs, NaNs, leakage risk, impossible CV/LB jumps, or stale artifacts.
7. Use `scripts/dryness_check.py` to decide whether the current direction is
   dry. When dry, stop local tinkering and switch to research/reflection.
8. Use `scripts/submission_gate_check.py` before any submission. The system must
   never submit without explicit human approval recorded in the gate report.

## Required References

Load these files only when needed:

- `references/architecture.md` for the Memory / Detection / Decision design.
- `references/memory-schema.md` for workspace files, JSONL fields, and flags.
- `references/submission-gates.md` for submission approval and compliance rules.
- `references/dryness-and-reflection.md` for dry-stop and research refill logic.
- `references/agent-roles.md` for multi-agent responsibilities and handoffs.

## Script Guide

- Create a workspace:

```bash
python scripts/create_competition_workspace.py --root workspaces --slug birdclef-2026 --metric auc --direction higher
```

- Validate memory state:

```bash
python scripts/validate_competition_state.py workspaces/birdclef-2026
```

- Check whether the current direction is dry:

```bash
python scripts/dryness_check.py workspaces/birdclef-2026 --metric auc --direction higher --patience 3 --eps 0.001
```

- Check a submission gate report:

```bash
python scripts/submission_gate_check.py workspaces/birdclef-2026/reports/submission_gate.json
```

## Hard Rules

- Keep competitions and experiments isolated by default.
- Treat local runs as debugging unless the competition rules say local results
  are authoritative.
- Do not fabricate scores, file provenance, external-data status, or approvals.
- Do not submit automatically. A human approval record is mandatory.
- If validation and leaderboard disagree, pause and investigate before making
  the next experiment decision.
