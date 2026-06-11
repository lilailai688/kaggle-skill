# Agent Roles

Use roles to keep decisions inspectable. One Codex instance may play multiple
roles, but the role boundary should still be written down.

## Orchestrator

Reads memory and flags, chooses one next action, delegates if needed, and writes
back `STATE.md`. The orchestrator does not silently submit.

## Researcher

Finds similar competitions, writeups, notebooks, papers, and discussion clues.
Returns evidence-backed ideas, not vague inspiration.

## Experimenter

Implements one idea in an isolated run directory. Records config, command,
outputs, logs, and resource usage.

## Reviewer

Questions the plan before expensive runs and questions the result after a run.
Focus areas: CV design, leakage, metric mismatch, stale artifacts, and whether
the experiment actually tests the stated idea.

## Gatekeeper

Runs submission gates, validates provenance, and prepares the approval request.
The gatekeeper rejects missing approval by default.

## Compliance Auditor

Checks Kaggle rules, external data, internet usage, account/team constraints,
model licenses, and whether automation could violate competition expectations.

## Handoff Rule

Each role handoff should include:

- objective
- input files
- allowed actions
- forbidden actions
- expected output
- acceptance criteria
