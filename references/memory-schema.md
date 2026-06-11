# Memory Schema

Keep memory simple, append-only where possible, and easy to inspect in plain
text.

## Directory Layout

```text
<workspace>/
  STATE.md
  ideas_backlog.md
  experiment_ledger.jsonl
  configs/
  data/
  flags/
  notebooks/
  reports/
  runs/
  submissions/
```

Each experiment must use its own `runs/<run_id>/` directory. Reusing a run
directory invalidates provenance unless the ledger explicitly says why.

## STATE.md

Required fields as plain Markdown bullets:

- Competition slug
- Metric
- Direction
- Current stage
- Best CV score
- Best LB score
- Active run
- Next action
- Blockers

## ideas_backlog.md

Use checkboxes or `status:` markers.

Recommended entry shape:

```markdown
- [ ] id: idea-001
  priority: high
  hypothesis: Short testable claim.
  validation: What result would support it.
  failure_mode: What would make it misleading.
```

## experiment_ledger.jsonl

Use one JSON object per line. Recommended fields:

- `run_id`: unique id, often timestamp plus short slug.
- `idea_id`: source idea.
- `status`: `planned`, `running`, `completed`, `invalid`, or `failed`.
- `primary_metric`: numeric validation metric.
- `metric_name`: metric name such as `auc`, `rmse`, or `map`.
- `direction`: `higher` or `lower`.
- `config`: short config or path to config file.
- `artifacts`: important output paths.
- `conclusion`: what changed and what to do next.
- `created_at`: ISO timestamp.

## Gate Reports

Store candidate submission gate reports as JSON in `reports/`.

Minimum shape:

```json
{
  "candidate_id": "sub_001",
  "automated_gates": {
    "metric_gate": true,
    "format_gate": true,
    "provenance_gate": true,
    "compliance_gate": true,
    "diversity_gate": true
  },
  "human_approval": {
    "approved": false,
    "approver": "",
    "approved_at": "",
    "request_id": ""
  }
}
```
