## Linked Issue

Closes #<N>

<!-- Mehrere Issues: `Refs #N1`, `Refs #N2`, `Closes #<primary>`.
     Kein Issue? → `/ai-review ac-waiver <reason ≥30 chars>` erforderlich. -->

---

## Summary

- <Was wurde geändert und warum?>
- <Welches Problem löst dieser PR?>
- <Relevante Architektur-Entscheidungen / Trade-offs, wenn vorhanden>

---

## Acceptance Criteria Verification

<!-- Gherkin-Szenarien aus dem verlinkten Issue kopieren. Jedes Szenario 1:1 auf einen Test mappen. -->

- [ ] Scenario: "<Titel aus Issue>"
  - Verified by: `tests/path/to/test_file.py::test_name`
- [ ] Scenario: "<Titel 2>"
  - Verified by: `tests/path/to/other_test.py::test_name`

---

## Test Plan

- [ ] TDD-Zyklus eingehalten (Red → Green → Refactor)
- [ ] Unit-Tests für alle neuen/geänderten Module
- [ ] `pytest --cov=ai_review_pipeline --cov-fail-under=80` lokal grün
- [ ] `ruff check .` ohne neue Fehler
- [ ] `scripts/smoke_cli.sh` lokal sauber durchgelaufen

---

## Checklist

- [ ] Branch-Name: `feat/<slug>-issue-<N>` / `fix/<slug>-issue-<N>` / `chore/<slug>-issue-<N>`
- [ ] Conventional Commit (`feat:`, `fix:`, `chore:`, `refactor:`, `test:`, `docs:`)
- [ ] Keine Secrets im Diff (`.env`, API-Keys, Tokens)
- [ ] AGENTS.md-Regeln eingehalten (TDD, No De-Scoping, Always-Latest)
- [ ] `[dev]`-Dependencies wenn nötig aktualisiert

---

> **Hinweis:** Dieses Repo dogfoodet ai-review-pipeline — erste 3 PRs dürfen admin-mergen
> bei Bootstrap-Blockern (Label `pipeline-bootstrap` + Waiver-Kommentar im PR).
> Ab PR #4 gilt normaler Consensus ≥ 8 ohne Ausnahme.
