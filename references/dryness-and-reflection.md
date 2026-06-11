# Dryness And Reflection

Dryness means a direction is no longer producing enough evidence of progress to
justify more compute without new thinking.

## Dry Signals

Treat a direction as dry when any condition holds:

- The last `patience` valid experiments improved the best metric by less than
  `eps`.
- The best score is stuck near a known ceiling.
- The backlog has no high-priority todo idea.
- Failures repeat with the same root cause.
- New experiments change many variables but produce no interpretable signal.

## What To Do When Dry

Do not keep tuning small parameters blindly. Switch to reflection:

1. Summarize the last valid experiments and failed experiments.
2. Identify which assumptions survived.
3. Identify which assumptions were falsified.
4. Search for similar competitions, writeups, notebooks, papers, and forum
   signals.
5. Refill `ideas_backlog.md` with evidence-backed, testable ideas.
6. Clear the dry flag only after at least one high-priority idea is available.

## Reflection Template

```markdown
## Reflection <date>

Best result:
Dry signal:
Likely bottleneck:
What failed:
What still looks promising:
New evidence:
Next high-priority ideas:
Stop conditions:
```

## Research Refill Quality Bar

Every new idea should include:

- source or analogy
- expected signal
- validation check
- failure mode
- estimated cost
- stop condition

Ideas without validation logic should remain low priority.
