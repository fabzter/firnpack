# firnpack documentation

| Document | What it is |
|---|---|
| [`usage.md`](usage.md) | every command: examples, exit codes, JSON shapes |
| [`agent-contract.md`](agent-contract.md) | normative contract for automated operators: schemas, error taxonomy, safe behaviors |
| [`storage.md`](storage.md) | layer format, database schema, recovery playbook |
| [`decisions.md`](decisions.md) | decision log (one entry per settled decision) |
| [`real-world-verification.md`](real-world-verification.md) | chained user journeys, defect findings, artifact identities and honest verification limits |
| [`../README.md`](../README.md) | humane front door |
| [`../SPEC.md`](../SPEC.md) | full specification (living decision record) |
| [`../AGENTS.md`](../AGENTS.md) | repository context for contributors |

All documents ship in the sdist. `usage.md` and `agent-contract.md` are two
lenses on the same contract: usage carries per-command operator callouts;
agent-contract.md is the normative reference for automated operators.
Every JSON shape documented here is a test contract — the e2e suite pins
them (`TestJsonContractShapes`).
