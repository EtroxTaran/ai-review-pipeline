# Acceptance-Criteria-Schreibkonvention (Gherkin)

Dieses Dokument definiert den verbindlichen Standard für Acceptance Criteria (ACs) in allen
Projekten, die `ai-review-pipeline` nutzen. Stage 5 (AC-Validation) liest ACs maschinell.
ACs die nicht diesem Format entsprechen, werden von Stage 5 als nicht abgedeckt gewertet —
fail-closed.

---

## Warum Gherkin?

Stage 5 der Pipeline prüft automatisch ob jedes AC-Scenario durch einen Test abgedeckt ist.
Das erfordert eine parsbare, strukturierte Sprache. Gherkin ist:

- **Maschinenlesbar**: `issue_parser.py` im Paket extrahiert Scenarios direkt aus Issue-Bodies
- **1:1 mit Tests mappbar**: Jedes `Scenario` entspricht exakt einem Test-Case (Unit oder E2E)
- **Verständlich für Nico und Sabine**: natürliche Sprache, kein Code
- **Standard**: Cucumber/Behave/Playwright unterstützen Gherkin nativ

Das 1:1-Mapping (Rule 9 aus CLAUDE.md) ist keine Empfehlung sondern Pflicht: kein AC ohne Test,
kein Test ohne AC.

---

## Struktur im Issue-Body

Jedes Issue, das Stage 5 validiert, muss diesen Aufbau haben:

```markdown
## Acceptance Criteria

```gherkin
Feature: <kurze Feature-Beschreibung>

  Scenario: <Name des Szenarios>
    Given <Ausgangszustand>
    When <Aktion>
    Then <erwartetes Ergebnis>
```
```

Pflichtfelder:
- Die Überschrift muss exakt `## Acceptance Criteria` lauten (zwei Rauten, kein Abweichen)
- Der Code-Block muss mit ` ```gherkin ` geöffnet werden (kein `feature`, kein leerer Fence)
- Mindestens ein `Scenario`-Block

---

## Syntax-Regeln

### Scenario vs. Scenario Outline

Verwende `Scenario` für einen einzigen konkreten Fall:

```gherkin
Scenario: Angemeldeter Nutzer sieht Dashboard
  Given der Nutzer ist eingeloggt als "nico@example.com"
  When er die Startseite aufruft
  Then sieht er das persönliche Dashboard
```

Verwende `Scenario Outline` wenn dasselbe Verhalten für mehrere Inputs gelten soll:

```gherkin
Scenario Outline: Ungültige Login-Versuche werden geblockt
  Given der Nutzer gibt Passwort "<password>" ein
  When er auf "Login" klickt
  Then sieht er die Fehlermeldung "<message>"

  Examples:
    | password | message                     |
    | ""       | Passwort darf nicht leer sein |
    | "a"      | Passwort zu kurz              |
```

### Given / When / Then / And / But

| Keyword | Verwendung |
|---|---|
| `Given` | Ausgangszustand (Precondition). Einmal pro Scenario. |
| `When` | Die Aktion die der Nutzer oder das System ausführt. Einmal pro Scenario. |
| `Then` | Das erwartete, prüfbare Ergebnis. Mindestens einmal pro Scenario. |
| `And` | Fortsetzung eines Given, When oder Then (gleiche Semantik). |
| `But` | Negativabgrenzung nach einem Then ("aber nicht …"). Sparsam einsetzen. |

Beispiel mit `And` und `But`:

```gherkin
Scenario: Erfolgreicher Checkout
  Given der Warenkorb enthält 2 Artikel
  And der Nutzer ist eingeloggt
  When er "Jetzt kaufen" klickt
  Then erscheint die Bestellbestätigung
  And eine Bestätigungsmail wird versendet
  But der Warenkorb bleibt nicht gefüllt
```

---

## Verbote

Diese Muster werden von Stage 5 als ungültig gewertet und führen zu einem Fail:

**ACs ohne `Then`** — Stage 5 kann kein erwartetes Ergebnis extrahieren:
```gherkin
# FALSCH
Scenario: Nutzer loggt sich ein
  Given Nutzer ist auf Login-Seite
  When er Zugangsdaten eingibt
  # kein Then → Stage 5 schlägt fehl
```

**Prosa-Bullets ohne Gherkin-Block** — werden nicht geparsed:
```markdown
# FALSCH
## Acceptance Criteria
- Der Nutzer kann sich einloggen
- Das Dashboard zeigt die letzten 5 Transaktionen
```

**Leerer Fence ohne `gherkin`-Sprach-Tag** — wird nicht als Gherkin erkannt:
````markdown
# FALSCH
```
Scenario: ...
```
````

**Generische Scenarios ohne konkreten Zustand**:
```gherkin
# FALSCH — zu vage, kein testbares Then
Scenario: System funktioniert korrekt
  Given das System läuft
  When ein Nutzer etwas macht
  Then passiert etwas
```

---

## Multi-Issue-Coverage im PR-Body

Wenn ein PR mehrere Issues schließt oder referenziert:

```markdown
Closes #12, Fixes #15, Refs #18
```

Stage 5 löst alle `Closes #N`, `Fixes #N` und `Refs #N` auf und holt die ACs aus jedem
verknüpften Issue. Die Gesamt-Coverage wird über alle Scenarios aus allen Issues berechnet.

`min_coverage` in `.ai-review/config.yaml` (Standard: `1.0`) bedeutet: alle Scenarios
aus allen verknüpften Issues müssen durch Tests abgedeckt sein.

---

## Beispiel-Issue-Body

```markdown
## Feature: Transaktions-Export

Nutzer soll monatliche Transaktionen als CSV exportieren können.

## Acceptance Criteria

```gherkin
Feature: Transaktions-Export

  Scenario: Erfolgreicher CSV-Export für aktuellen Monat
    Given der Nutzer ist eingeloggt
    And der aktuelle Monat hat mindestens eine Transaktion
    When er auf "Export CSV" klickt
    Then wird eine CSV-Datei mit allen Transaktionen des Monats heruntergeladen
    And die erste Zeile enthält die Header "Datum,Beschreibung,Betrag,Kategorie"

  Scenario: Export-Button ist für leere Monate deaktiviert
    Given der Nutzer ist eingeloggt
    And der aktuelle Monat hat keine Transaktionen
    When er die Transaktions-Seite aufruft
    Then ist der "Export CSV"-Button deaktiviert
    And ein Tooltip erklärt "Keine Transaktionen in diesem Monat"

  Scenario Outline: Datumsformat im CSV ist korrekt
    Given eine Transaktion vom <datum>
    When der Nutzer die CSV exportiert
    Then enthält die CSV-Zeile das Datum im Format "<format>"

    Examples:
      | datum      | format     |
      | 2026-01-15 | 15.01.2026 |
      | 2026-12-01 | 01.12.2026 |
```

## Test-Plan

- [ ] `tests/test_export.py::test_csv_export_success` (Unit, Arrange-Act-Assert)
- [ ] `tests/e2e/test_export.spec.ts::export button disabled` (Playwright)
- [ ] `tests/e2e/test_export.spec.ts::csv date format` (Playwright, parametrisiert)
```

---

## AC-Waiver-Prozess

Wenn Stage 5 ein False Positive produziert (z.B. AC ist korrekt abgedeckt, aber der Parser
erkennt es nicht wegen einer ungewöhnlichen Test-Datei-Struktur):

Im PR als Kommentar posten:

```
/ai-review ac-waiver <reason mit mindestens 30 Zeichen>
```

Beispiel:

```
/ai-review ac-waiver Tests für Scenario 2 sind in integration/test_export_edge_cases.py, nicht in tests/
```

Das erzeugt automatisch einen Audit-Trail-Eintrag im PR. Der `ai-review-nachfrage.yml`-Workflow
verarbeitet den Command und setzt den Stage-5-Status auf `success (waived)`.

**Kein Label-Override.** Das `waivers.min_reason_length`-Feld in `.ai-review/config.yaml`
(Standard: 30 Zeichen) erzwingt eine minimale Begründungslänge — Dummy-Reasons wie
"false positive" (13 Zeichen) werden abgelehnt.

Waiver sind im `#ai-review-<projekt>`-Discord-Channel sichtbar und werden in
`.ai-review/metrics.jsonl` protokolliert.

---

## Referenzen

- `src/ai_review_pipeline/issue_parser.py` — Gherkin-Parser + `Closes #N`-Resolver
- `src/ai_review_pipeline/stages/ac_validation.py` — Stage-5-Validierungs-Logik
- `schema/config.schema.yaml` — `min_coverage`, `waivers.min_reason_length`
- [docs/project-adoption.md](project-adoption.md) — Branch-Protection + Secret-Setup
