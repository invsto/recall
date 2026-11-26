# Recall decisions

One numbering for every Recall decision: the v2 spec's `D` numbers (spec section 11.1), extended for decisions
that appear only in the build plan. Where the build plan used a number that the spec also uses for a different
decision, the spec number wins and the plan's decision is renumbered `D12` and up.

## Spec decisions (canonical)

| # | Decision | Status |
| --- | --- | --- |
| D1 | Core rewrite: new model, migrated into a new file; keep digest tokens, CAS, receipts, review gate, audit | Approved (owner, 2026-11-26) |
| D2 | Python 3.12+ and SQLite 3.37+ | Default in force |
| D3 | Vector leg optional, off by default | Default in force |
| D4 | Extraction endpoints local only; cloud needs per-source `cloud_allowed` and an acknowledged manifest | Default in force |
| D5 | Private explicit fact saves auto-supersede; extracted candidates never do | Default in force |
| D6 | Teams in v2.1, via membership | Default in force |
| D7 | Backup encryption: file mode 0600 plus OS disk encryption; optional encryption in v2.1 | Default in force |
| D8 | Token estimator `bytes3-v1` | Default in force |
| D9 | Extraction labels: owner labels 200 items locally | Dropped from this round (owner direction); not prepared |
| D10 | License MIT; LICENSE file added | Done (commit `f3abe7f`) |
| D11 | Deletion keeps no content hash or key | Default in force |

## Build-plan decisions, renumbered

| Build plan | Decision | Mapped to | Status |
| --- | --- | --- | --- |
| D1 | Distribution name `recallhq` (command stays `recall`) | D12 | Accepted (owner); PyPI upload not approved |
| D2 | Length of the `openbot` alias deprecation window: one minor release | D13 | Default in force |
| D3 | Add the `note` item type: private only, 1 KiB | D14 | Default in force |
| D4 | Token estimator `ceil(bytes/4)` | Superseded by spec D8 (`bytes3-v1`) | Not used |
| D5 | Search results are references plus rank, no snippets | D15 | Default in force |
| D6 | Maintenance proposals use `origin='maintenance'` | D16 | Default in force |
| D7 | Optional ANN backend `sqlite-vec`, off by default | D17 | Open: maturity unverified |
| D8 | Retention limits 90, 365, 30, 90 days; backups and exports 30 days | D18 | Default in force (checkpoints 90 days in code) |
| D9 | Adapters in the same repo under `recall_adapters/` | D19 | Default in force |
| D10 | Telemetry: none | D20 | Default in force |

## Open

- D17 (`sqlite-vec` maturity): unverified; needed only if the vector leg is enabled.
- PyPI upload of `recallhq`: waits for explicit owner approval.
- Merge of `development` into `main`: waits for explicit owner approval.
