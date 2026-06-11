# Submission Gates

Submitting is a privileged action. A Kaggle skill may prepare candidates, but
must not submit until all automated gates pass and a human approval record is
present.

## Automated Gates

Required gates:

- `metric_gate`: validation score meets the configured threshold.
- `format_gate`: file names, columns, ids, row count, zip structure, and dtypes
  match the competition requirements.
- `provenance_gate`: candidate artifacts come from a recorded run and recent
  outputs, not stale files.
- `compliance_gate`: competition rules, internet settings, external data, model
  licensing, and team/account limits are satisfied.
- `diversity_gate`: ensemble components are not duplicates when diversity is
  part of the strategy.

Optional gates:

- leakage check
- distribution-shift check
- notebook linkage check
- ONNX or model-file validation
- runtime and memory estimate

## Human Approval

The approval record must include:

- `approved: true`
- approver identity
- timestamp
- request id or thread id
- candidate id

If approval is missing, stale, ambiguous, or from the wrong person, reject the
submission even if all automated gates pass.

## Provenance Checklist

Before approving, inspect:

- run id and config
- validation report
- artifact timestamp
- file hash if available
- submission command or notebook version
- known rule risks

## Reject Conditions

Reject immediately when:

- any automated gate is false or missing
- the candidate file was edited outside a recorded run
- the metric improved in a way that suggests leakage or broken CV
- external data status is unknown
- the system attempts to self-approve
- Kaggle submission limits are unclear
