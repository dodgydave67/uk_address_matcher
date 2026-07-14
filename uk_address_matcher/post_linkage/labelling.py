"""Build the self-contained clerical-review tool used by :class:`MatchResult`."""

from __future__ import annotations

import hashlib
import json
import math
import os
import stat
from pathlib import Path
from typing import TYPE_CHECKING, Any, TypedDict
from uuid import uuid4

if TYPE_CHECKING:
    from uk_address_matcher.post_linkage.match_result.result import MatchResult


class _LabellingCandidate(TypedDict):
    unique_id: str
    address: str
    postcode: str
    match_weight: float | None
    rank: int


class _LabellingRecord(TypedDict):
    key: str
    unique_id: str
    address: str
    postcode: str
    initial_label: str | None
    candidates: list[_LabellingCandidate]


_REQUIRED_CANDIDATE_COLUMNS = {
    "unique_id_r",
    "unique_id_l",
    "ukam_address_id_r",
    "address_concat_r",
    "postcode_r",
    "original_address_concat_l",
    "postcode_l",
    "match_weight",
    "candidate_rank",
}
_EXPORT_COLUMNS = (
    "unique_id",
    "ukam_label",
    "label_status",
    "proposed_ukam_label",
    "selected_candidate_rank",
    "selected_match_weight",
    "recordset_id",
    "exported_at_utc",
)
_PAIRWISE_EXPORT_COLUMNS = (
    "id",
    "messy_address",
    "messy_postcode",
    "unique_id_l",
    "human_label",
    "canonical_address",
    "canonical_postcode",
    "match_weight",
    "candidate_rank",
    "label_decision",
    "recordset_id",
    "exported_at_utc",
)


def _as_text(value: Any) -> str:
    return "" if value is None else str(value)


def _as_finite_float(value: Any) -> float | None:
    if value is None:
        return None
    converted = float(value)
    return converted if math.isfinite(converted) else None


def _validate_max_candidates(max_candidates: int) -> None:
    if isinstance(max_candidates, bool) or not isinstance(max_candidates, int):
        raise TypeError("max_candidates must be an integer.")
    if max_candidates < 1:
        raise ValueError("max_candidates must be at least 1.")


def _normalise_messy_ids(
    messy_ids: list[str | int] | None,
) -> list[str] | None:
    if messy_ids is None:
        return None
    if not isinstance(messy_ids, list):
        raise TypeError("messy_ids must be a list of source unique_id values.")
    if not messy_ids:
        raise ValueError("messy_ids must contain at least one source unique_id.")

    requested_ids: list[str] = []
    seen_ids: set[str] = set()
    for value in messy_ids:
        if value is None:
            raise ValueError("messy_ids cannot contain null values.")
        normalised = str(value)
        if normalised not in seen_ids:
            seen_ids.add(normalised)
            requested_ids.append(normalised)
    return requested_ids


def _build_labelling_records(
    result: MatchResult,
    *,
    max_candidates: int,
    messy_ids: list[str | int] | None,
) -> list[_LabellingRecord]:
    _validate_max_candidates(max_candidates)
    requested_ids = _normalise_messy_ids(messy_ids)

    stage = result._splink_stage
    if stage is None:
        raise ValueError(
            "Creating a labelling tool requires a configured SplinkStage. "
            "Include a SplinkStage in the matching pipeline and run the matcher."
        )
    if stage.best_matches_table is None:
        raise ValueError(
            "The configured SplinkStage did not produce a retained candidates "
            "table. Run the matcher with records that reach the Splink stage "
            "before creating a labelling tool."
        )

    try:
        candidate_relation = result.con.table(stage.best_matches_table)
    except Exception as exc:
        raise ValueError(
            "The retained Splink candidates are no longer available on this "
            "DuckDB connection. Create the labelling tool while the MatchResult "
            "connection is still active."
        ) from exc

    missing_columns = sorted(
        _REQUIRED_CANDIDATE_COLUMNS.difference(candidate_relation.columns)
    )
    if missing_columns:
        raise ValueError(
            "The retained Splink candidates table is missing required columns: "
            + ", ".join(missing_columns)
        )

    initial_label_select = (
        "CAST(ukam_label_r AS VARCHAR)"
        if "ukam_label_r" in candidate_relation.columns
        else "CAST(NULL AS VARCHAR)"
    )
    candidate_filter = ""
    query_parameters: list[str] = []
    if requested_ids is not None:
        placeholders = ", ".join("?" for _ in requested_ids)
        candidate_filter = f"AND CAST(unique_id_r AS VARCHAR) IN ({placeholders})"
        query_parameters = requested_ids

    query = f"""
        SELECT
            CAST(ukam_address_id_r AS VARCHAR) AS record_key,
            CAST(unique_id_r AS VARCHAR) AS messy_id,
            CAST(address_concat_r AS VARCHAR) AS messy_address,
            CAST(postcode_r AS VARCHAR) AS messy_postcode,
            {initial_label_select} AS initial_label,
            CAST(unique_id_l AS VARCHAR) AS canonical_id,
            CAST(original_address_concat_l AS VARCHAR) AS canonical_address,
            CAST(postcode_l AS VARCHAR) AS canonical_postcode,
            CAST(match_weight AS DOUBLE) AS match_weight,
            CAST(candidate_rank AS INTEGER) AS candidate_rank
        FROM ({candidate_relation.sql_query()}) AS candidates
        WHERE (candidate_rank IS NULL OR candidate_rank <= {max_candidates})
        {candidate_filter}
        ORDER BY
            ukam_address_id_r,
            candidate_rank NULLS LAST,
            match_weight DESC NULLS LAST,
            CAST(unique_id_l AS VARCHAR)
    """

    try:
        rows = result.con.execute(query, query_parameters).fetchall()
    except Exception as exc:
        raise ValueError(
            "The retained Splink candidates could not be read from this DuckDB "
            "connection. Create the labelling tool while the MatchResult "
            "connection is still active."
        ) from exc

    records: dict[str, _LabellingRecord] = {}
    candidate_ids: dict[str, set[str]] = {}
    for (
        raw_record_key,
        raw_messy_id,
        messy_address,
        messy_postcode,
        raw_initial_label,
        raw_canonical_id,
        canonical_address,
        canonical_postcode,
        match_weight,
        candidate_rank,
    ) in rows:
        if raw_record_key is None:
            raise ValueError(
                "The retained Splink candidates contain a null ukam_address_id_r value."
            )
        if raw_messy_id is None:
            raise ValueError(
                "The retained Splink candidates contain a null source unique_id."
            )

        record_key = str(raw_record_key)
        messy_id = str(raw_messy_id)
        initial_label = None if raw_initial_label is None else str(raw_initial_label)

        record = records.get(record_key)
        if record is None:
            record = {
                "key": record_key,
                "unique_id": messy_id,
                "address": _as_text(messy_address),
                "postcode": _as_text(messy_postcode),
                "initial_label": initial_label,
                "candidates": [],
            }
            records[record_key] = record
            candidate_ids[record_key] = set()
        else:
            if record["unique_id"] != messy_id:
                raise ValueError(
                    "One internal address identifier maps to multiple source "
                    "unique_id values in the retained Splink candidates."
                )
            if record["initial_label"] != initial_label:
                raise ValueError(
                    "One source record has inconsistent pre-existing labels in "
                    "the retained Splink candidates."
                )

        if raw_canonical_id is None:
            continue
        canonical_id = str(raw_canonical_id)
        if canonical_id == "":
            raise ValueError(
                "The retained Splink candidates contain an empty canonical unique_id."
            )
        if candidate_rank is None:
            raise ValueError("A non-null canonical candidate is missing candidate_rank.")
        if canonical_id in candidate_ids[record_key]:
            continue

        candidate_ids[record_key].add(canonical_id)
        record["candidates"].append(
            {
                "unique_id": canonical_id,
                "address": _as_text(canonical_address),
                "postcode": _as_text(canonical_postcode),
                "match_weight": _as_finite_float(match_weight),
                "rank": int(candidate_rank),
            }
        )

    if requested_ids is not None:
        returned_ids = {record["unique_id"] for record in records.values()}
        missing_ids = [
            requested_id
            for requested_id in requested_ids
            if requested_id not in returned_ids
        ]
        if missing_ids:
            formatted_ids = ", ".join(repr(value) for value in missing_ids)
            raise ValueError(
                "The following source unique_id values were not found in the "
                f"retained Splink candidates: {formatted_ids}."
            )

    if not records:
        raise ValueError(
            "No source records are available in the retained Splink candidates table."
        )

    seen_source_ids: set[str] = set()
    duplicate_source_ids: set[str] = set()
    for record in records.values():
        source_id = record["unique_id"]
        if source_id in seen_source_ids:
            duplicate_source_ids.add(source_id)
        seen_source_ids.add(source_id)
    if duplicate_source_ids:
        formatted_ids = ", ".join(repr(value) for value in sorted(duplicate_source_ids))
        raise ValueError(
            "Duplicate source ID value(s) were found: "
            f"{formatted_ids}. Source IDs must be unique so exported labels can "
            "be joined unambiguously."
        )

    return list(records.values())


def _safe_json(value: Any) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return (
        encoded.replace("&", "\\u0026")
        .replace("<", "\\u003c")
        .replace(">", "\\u003e")
        .replace("\u2028", "\\u2028")
        .replace("\u2029", "\\u2029")
    )


def _render_labelling_tool(records: list[_LabellingRecord]) -> str:
    records_json = _safe_json(records)
    recordset_id = hashlib.sha256(records_json.encode("utf-8")).hexdigest()[:20]

    return (
        _HTML_TEMPLATE.replace(
            "__UKAM_STORAGE_KEY__",
            _safe_json(f"ukam-labelling-{recordset_id}"),
        )
        .replace("__UKAM_RECORDSET_ID__", _safe_json(recordset_id))
        .replace("__UKAM_EXPORT_COLUMNS__", _safe_json(_EXPORT_COLUMNS))
        .replace(
            "__UKAM_PAIRWISE_EXPORT_COLUMNS__",
            _safe_json(_PAIRWISE_EXPORT_COLUMNS),
        )
        .replace("__UKAM_RECORDS__", records_json)
    )


def create_labelling_tool(
    result: MatchResult,
    output_path: str | Path,
    *,
    max_candidates: int = 5,
    messy_ids: list[str | int] | None = None,
    overwrite: bool = False,
) -> Path:
    """Create an offline, self-contained HTML labelling tool."""
    records = _build_labelling_records(
        result,
        max_candidates=max_candidates,
        messy_ids=messy_ids,
    )
    rendered = _render_labelling_tool(records)

    path = Path(output_path).expanduser()
    if path.exists() and not overwrite:
        raise FileExistsError(f"Refusing to overwrite existing labelling tool: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)

    temporary_path = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    try:
        descriptor = os.open(
            temporary_path,
            os.O_CREAT | os.O_EXCL | os.O_WRONLY,
            stat.S_IRUSR | stat.S_IWUSR,
        )
        with os.fdopen(descriptor, "w", encoding="utf-8") as file_handle:
            file_handle.write(rendered)
        temporary_path.replace(path)
        path.chmod(stat.S_IRUSR | stat.S_IWUSR)
    finally:
        temporary_path.unlink(missing_ok=True)

    return path


_HTML_TEMPLATE = r"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta http-equiv="Content-Security-Policy"
        content="default-src 'none';
                 script-src 'unsafe-inline';
                 style-src 'unsafe-inline'">
  <title>UK Address Matcher labelling tool</title>
  <style>
    :root {
      color-scheme: light dark;
      --background: #f5f7fa;
      --surface: #ffffff;
      --text: #17202a;
      --muted: #536273;
      --border: #c9d2dc;
      --accent: #005ea5;
      --accent-text: #ffffff;
      --selected: #e8f2fb;
      --warning: #fff4ce;
      --warning-text: #8a4600;
      font-family: system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
    }
    @media (prefers-color-scheme: dark) {
      :root {
        --background: #111820;
        --surface: #1b2631;
        --text: #f3f6f8;
        --muted: #b7c3cf;
        --border: #536273;
        --accent: #5bb4f0;
        --accent-text: #07131c;
        --selected: #173b55;
        --warning: #4a3c12;
        --warning-text: #ffbf69;
      }
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      background: var(--background);
      color: var(--text);
      line-height: 1.45;
    }
    main { width: min(980px, 100%); margin: 0 auto; padding: 1.25rem; }
    h1 { font-size: 1.75rem; margin: 0 0 0.35rem; }
    h2 { font-size: 1.2rem; margin: 0 0 0.6rem; }
    p { margin: 0.35rem 0; }
    .muted { color: var(--muted); }
    .privacy {
      margin: 1rem 0;
      padding: 0.8rem 1rem;
      border-left: 0.35rem solid #f0b429;
      background: var(--warning);
    }
    .toolbar, .navigation, .actions, .manual-controls, .file-actions {
      display: flex;
      flex-wrap: wrap;
      align-items: center;
      gap: 0.6rem;
    }
    .toolbar { justify-content: space-between; margin: 1rem 0; }
    .progress { min-width: 14rem; flex: 1; }
    progress { width: 100%; height: 0.85rem; }
    button, input {
      border: 1px solid var(--border);
      border-radius: 0.3rem;
      background: var(--surface);
      color: var(--text);
      font: inherit;
      padding: 0.55rem 0.8rem;
    }
    button { cursor: pointer; font-weight: 600; }
    button:hover { border-color: var(--accent); }
    button:focus-visible, input:focus-visible {
      outline: 0.2rem solid var(--accent);
      outline-offset: 0.15rem;
    }
    button.primary { background: var(--accent); color: var(--accent-text); }
    button[disabled] { cursor: not-allowed; opacity: 0.5; }
    .panel {
      background: var(--surface);
      border: 1px solid var(--border);
      border-radius: 0.45rem;
      padding: 1rem;
      margin: 0.8rem 0;
    }
    .source-address { font-size: 1.15rem; font-weight: 700; white-space: pre-wrap; }
    .metadata { display: flex; flex-wrap: wrap; gap: 0.4rem 1rem; color: var(--muted); }
    #candidate-list { display: grid; gap: 0.7rem; margin-top: 0.7rem; }
    .candidate {
      width: 100%;
      display: grid;
      grid-template-columns: 2.4rem 1fr;
      gap: 0.75rem;
      align-items: start;
      text-align: left;
      padding: 0.85rem;
    }
    .candidate.selected {
      border: 0.18rem solid var(--accent);
      background: var(--selected);
    }
    .candidate-number {
      display: inline-grid;
      place-items: center;
      width: 2rem;
      height: 2rem;
      border-radius: 50%;
      background: var(--accent);
      color: var(--accent-text);
    }
    .candidate-address { display: block; font-weight: 700; white-space: pre-wrap; }
    .candidate-detail { display: block; color: var(--muted); margin-top: 0.2rem; }
    .empty { padding: 1rem; border: 1px dashed var(--border); color: var(--muted); }
    .manual-controls input { min-width: 16rem; flex: 1; }
    .decision { min-height: 1.6rem; margin-top: 0.8rem; font-weight: 700; }
    .storage-status { min-height: 1.5rem; color: var(--muted); }
    .storage-status.warning { color: var(--warning-text); font-weight: 700; }
    .navigation { justify-content: space-between; margin-top: 1rem; }
    .shortcuts { margin-top: 1rem; font-size: 0.9rem; color: var(--muted); }
    kbd {
      border: 1px solid var(--border);
      border-bottom-width: 2px;
      border-radius: 0.2rem;
      padding: 0.05rem 0.3rem;
      background: var(--surface);
    }
    @media (max-width: 620px) {
      main { padding: 0.8rem; }
      .candidate { grid-template-columns: 2.2rem 1fr; }
    }
  </style>
</head>
<body>
  <main>
    <h1>UK Address Matcher labelling tool</h1>
    <p class="muted">
      Review the retained candidates. No candidate is selected automatically.
    </p>
    <p class="privacy">
      <strong>Handle securely:</strong> this HTML file and the optional candidate-pair
      CSV contain address data. All exports contain identifiers or decisions and
      should remain in an approved local location.
    </p>

    <div class="toolbar">
      <div class="progress">
        <div id="progress-text" aria-live="polite"></div>
        <progress id="progress-bar" aria-label="Confirmed review progress"
                  max="1" value="0"></progress>
      </div>
      <button id="download-json" class="primary" type="button">
        Download exact labels JSON
      </button>
      <button id="download" type="button">
        Download spreadsheet-safe labels CSV
      </button>
    </div>
    <div id="storage-status" class="storage-status" aria-live="polite"></div>
    <div class="file-actions" aria-label="Progress file actions">
      <button id="download-checkpoint" type="button">Download checkpoint</button>
      <button id="import-trigger" type="button">Import checkpoint</button>
      <input id="import-checkpoint" type="file" accept="application/json,.json"
             hidden aria-hidden="true" tabindex="-1">
      <button id="clear-progress" type="button">Clear browser progress</button>
    </div>
    <details class="panel">
      <summary>Compatibility export for candidate-pair workflows</summary>
      <p class="muted">
        This optional CSV contains address text and the legacy
        <code>human_label</code> candidate-pair columns.
      </p>
      <button id="download-pairwise" type="button">
        Download candidate labels CSV
      </button>
    </details>

    <section class="panel" aria-labelledby="source-heading">
      <h2 id="source-heading" tabindex="-1">Address to label</h2>
      <div id="source-address" class="source-address"></div>
      <div class="metadata">
        <span>Postcode: <span id="source-postcode"></span></span>
        <span id="source-id"></span>
      </div>
    </section>

    <section class="panel" aria-labelledby="candidate-heading">
      <h2 id="candidate-heading">Candidate matches</h2>
      <p class="muted">Choose only when the candidate refers to the same property.</p>
      <div id="candidate-list"></div>
    </section>

    <section class="panel" aria-labelledby="manual-heading">
      <h2 id="manual-heading">Canonical ID outside this list</h2>
      <p class="muted">
        IDs outside the displayed candidates cannot be verified in this offline file.
        Record an unverified proposal, or confirm it only after checking the ID and
        address in the full canonical dataset.
      </p>
      <div class="manual-controls">
        <label for="manual-id">Canonical ID</label>
        <input id="manual-id" autocomplete="off">
        <button id="select-manual" type="button">Record unverified ID</button>
        <button id="confirm-manual" type="button">
          Confirm externally verified ID
        </button>
      </div>
    </section>

    <div class="actions" aria-label="Labelling actions">
      <button id="none-shown" type="button">None of these candidates (N)</button>
      <button id="confirmed-no-match" type="button">
        Confirm absent from canonical
      </button>
      <button id="skip" type="button">Skip / unsure (S)</button>
      <button id="clear" type="button">Clear decision</button>
    </div>
    <div id="decision" class="decision" aria-live="polite"></div>

    <div class="navigation">
      <button id="previous" type="button">Previous</button>
      <span id="position"></span>
      <button id="next-unresolved" type="button">Next unresolved</button>
      <button id="next" type="button">Next</button>
    </div>

    <p class="shortcuts">
      Keyboard: <kbd>1</kbd>–<kbd>9</kbd> choose a candidate, <kbd>N</kbd> marks
      none of the displayed candidates, <kbd>S</kbd> skips, and arrow keys move
      between records. Model scores are intentionally hidden during review.
    </p>
  </main>

  <script type="application/json" id="ukam-records">__UKAM_RECORDS__</script>
  <script type="application/json" id="ukam-export-columns">
    __UKAM_EXPORT_COLUMNS__
  </script>
  <script type="application/json" id="ukam-pairwise-export-columns">
    __UKAM_PAIRWISE_EXPORT_COLUMNS__
  </script>
  <script>
    "use strict";

    const records = JSON.parse(document.getElementById("ukam-records").textContent);
    const exportColumns = JSON.parse(
      document.getElementById("ukam-export-columns").textContent
    );
    const pairwiseExportColumns = JSON.parse(
      document.getElementById("ukam-pairwise-export-columns").textContent
    );
    const storageKey = __UKAM_STORAGE_KEY__;
    const recordsetId = __UKAM_RECORDSET_ID__;
    const stateSchemaVersion = 1;
    const elements = {
      progressText: document.getElementById("progress-text"),
      progressBar: document.getElementById("progress-bar"),
      storageStatus: document.getElementById("storage-status"),
      sourceAddress: document.getElementById("source-address"),
      sourcePostcode: document.getElementById("source-postcode"),
      sourceId: document.getElementById("source-id"),
      candidateList: document.getElementById("candidate-list"),
      manualId: document.getElementById("manual-id"),
      decision: document.getElementById("decision"),
      position: document.getElementById("position"),
      previous: document.getElementById("previous"),
      next: document.getElementById("next"),
      importCheckpoint: document.getElementById("import-checkpoint")
    };

    function initialDecisions() {
      return Object.fromEntries(
        records
          .filter(record => record.initial_label !== null)
          .map(record => [
            record.key,
            {status: "pre_existing", candidateId: record.initial_label}
          ])
      );
    }

    function initialState() {
      return {decisions: initialDecisions(), currentIndex: 0};
    }

    function normaliseDecision(record, decision) {
      if (!decision || typeof decision !== "object") return null;
      const candidateId = typeof decision.candidateId === "string"
        ? decision.candidateId : null;
      if (decision.status === "matched" && candidateId &&
          record.candidates.some(candidate => candidate.unique_id === candidateId)) {
        return {status: "matched", candidateId};
      }
      if (["manual_unverified", "matched_manual"].includes(decision.status) &&
          candidateId) {
        return {status: decision.status, candidateId};
      }
      if (decision.status === "pre_existing" && candidateId === record.initial_label) {
        return {status: "pre_existing", candidateId};
      }
      if (["confirmed_no_match", "none_of_candidates", "skipped"]
          .includes(decision.status)) {
        return {status: decision.status, candidateId: null};
      }
      return null;
    }

    function normaliseDecisions(rawDecisions, {strict = false} = {}) {
      const decisions = {};
      if (!rawDecisions || typeof rawDecisions !== "object" ||
          Array.isArray(rawDecisions)) {
        if (strict) throw new Error("Checkpoint decisions must be an object.");
        return decisions;
      }
      const recordsByKey = new Map(records.map(record => [record.key, record]));
      Object.entries(rawDecisions).forEach(([key, decision]) => {
        const record = recordsByKey.get(key);
        const normalised = record ? normaliseDecision(record, decision) : null;
        if (strict && !normalised) {
          throw new Error(`Checkpoint contains an invalid decision for record ${key}.`);
        }
        if (normalised) decisions[key] = normalised;
      });
      return decisions;
    }

    function testStorage() {
      try {
        const probeKey = `${storageKey}-probe`;
        localStorage.setItem(probeKey, "1");
        localStorage.removeItem(probeKey);
        return true;
      } catch (error) {
        console.warn("Browser draft storage is unavailable", error);
        return false;
      }
    }

    let storageAvailable = testStorage();
    let storageConflict = false;
    let persistenceMessage = storageAvailable
      ? "Browser draft autosave is active. Download checkpoints regularly."
      : "Browser draft autosave is unavailable. Download checkpoints to keep progress.";

    function loadState() {
      const fallback = initialState();
      if (!storageAvailable) return fallback;
      try {
        const raw = localStorage.getItem(storageKey);
        if (!raw) return fallback;
        const stored = JSON.parse(raw);
        if (stored && stored.schemaVersion === stateSchemaVersion &&
            stored.recordsetId === recordsetId) {
          return {
            decisions: normaliseDecisions(stored.decisions, {strict: true}),
            currentIndex: Number.isInteger(stored.currentIndex)
              ? stored.currentIndex : 0
          };
        }
        persistenceMessage =
          "An incompatible browser draft was ignored. Download a new checkpoint.";
      } catch (error) {
        console.warn("Could not restore labelling progress", error);
        persistenceMessage =
          "The browser draft could not be restored. Download a checkpoint after editing.";
      }
      return fallback;
    }

    const state = loadState();
    state.currentIndex = Math.max(0, Math.min(state.currentIndex, records.length - 1));

    function serialisedState() {
      return {
        schemaVersion: stateSchemaVersion,
        recordsetId,
        decisions: state.decisions,
        currentIndex: state.currentIndex
      };
    }

    function saveState() {
      if (!storageAvailable || storageConflict) {
        renderPersistenceStatus();
        return;
      }
      try {
        localStorage.setItem(storageKey, JSON.stringify(serialisedState()));
        persistenceMessage =
          "Browser draft saved. Download checkpoints regularly.";
      } catch (error) {
        console.warn("Could not save labelling progress", error);
        storageAvailable = false;
        persistenceMessage =
          "Browser draft autosave failed. Download a checkpoint now to keep progress.";
      }
      renderPersistenceStatus();
    }

    function currentRecord() { return records[state.currentIndex]; }
    function currentDecision() { return state.decisions[currentRecord().key] || null; }

    function setDecision(status, candidateId = null, focusCandidateIndex = null) {
      state.decisions[currentRecord().key] = {status, candidateId};
      saveState();
      render();
      if (focusCandidateIndex !== null) {
        const selected = elements.candidateList.querySelector(
          `[data-candidate-index="${focusCandidateIndex}"]`
        );
        if (selected) selected.focus({preventScroll: true});
      }
    }

    function selectCandidate(index, restoreFocus = true) {
      const candidate = currentRecord().candidates[index];
      if (candidate) {
        setDecision(
          "matched",
          candidate.unique_id,
          restoreFocus ? index : null
        );
      }
    }

    function clearDecision() {
      const record = currentRecord();
      if (record.initial_label !== null) {
        state.decisions[record.key] = {
          status: "pre_existing",
          candidateId: record.initial_label
        };
      } else {
        delete state.decisions[record.key];
      }
      saveState();
      render();
    }

    function move(offset) {
      state.currentIndex = Math.max(
        0,
        Math.min(state.currentIndex + offset, records.length - 1)
      );
      saveState();
      render();
      document.getElementById("source-heading").focus({preventScroll: true});
    }

    function moveToNextUnresolved() {
      for (let offset = 1; offset <= records.length; offset += 1) {
        const index = (state.currentIndex + offset) % records.length;
        const decision = state.decisions[records[index].key];
        if (!decision || !["matched", "matched_manual", "confirmed_no_match"]
            .includes(decision.status)) {
          state.currentIndex = index;
          saveState();
          render();
          document.getElementById("source-heading").focus({preventScroll: true});
          return;
        }
      }
      elements.decision.textContent = "Every record has a confirmed decision.";
    }

    function appendText(parent, className, text) {
      const item = document.createElement("span");
      item.className = className;
      item.textContent = text;
      parent.appendChild(item);
    }

    function renderCandidates(record, decision) {
      elements.candidateList.replaceChildren();
      if (record.candidates.length === 0) {
        const empty = document.createElement("div");
        empty.className = "empty";
        empty.textContent = "No retained candidate was generated for this address.";
        elements.candidateList.appendChild(empty);
        return;
      }

      record.candidates.forEach((candidate, index) => {
        const button = document.createElement("button");
        button.type = "button";
        button.className = "candidate";
        button.dataset.candidateIndex = String(index);
        const selected = decision && decision.status === "matched" &&
          decision.candidateId === candidate.unique_id;
        if (selected) button.classList.add("selected");
        button.setAttribute("aria-pressed", selected ? "true" : "false");
        button.setAttribute(
          "aria-label",
          `Candidate ${index + 1}: ${candidate.address || "address unavailable"}, ` +
          `postcode ${candidate.postcode || "unavailable"}, ID ${candidate.unique_id}`
        );

        appendText(button, "candidate-number", String(index + 1));
        const description = document.createElement("span");
        appendText(
          description,
          "candidate-address",
          candidate.address || "Address unavailable"
        );
        appendText(
          description,
          "candidate-detail",
          `${candidate.postcode || "No postcode"} · ID ${candidate.unique_id}`
        );
        button.appendChild(description);
        button.addEventListener("click", () => selectCandidate(index));
        elements.candidateList.appendChild(button);
      });
    }

    function renderProgress() {
      const decisions = records.map(record => state.decisions[record.key] || null);
      const confirmed = decisions.filter(decision => decision &&
        ["matched", "matched_manual", "confirmed_no_match"]
          .includes(decision.status)).length;
      const skipped = decisions.filter(decision =>
        decision && decision.status === "skipped").length;
      const unreviewed = decisions.filter(decision => !decision).length;
      const needsVerification = records.length - confirmed - skipped - unreviewed;
      elements.progressBar.max = records.length;
      elements.progressBar.value = confirmed;
      elements.progressText.textContent =
        `${confirmed} confirmed, ${needsVerification} need verification, ` +
        `${skipped} skipped, ${unreviewed} unreviewed, ${records.length} total`;
    }

    function renderPersistenceStatus() {
      if (elements.storageStatus.textContent !== persistenceMessage) {
        elements.storageStatus.textContent = persistenceMessage;
      }
      elements.storageStatus.classList.toggle(
        "warning",
        !storageAvailable || storageConflict
      );
    }

    function renderDecision(decision) {
      if (!decision) {
        elements.decision.textContent = "No decision recorded.";
      } else if (decision.status === "matched") {
        elements.decision.textContent =
          `Decision: matched to canonical ID ${decision.candidateId}.`;
      } else if (decision.status === "matched_manual") {
        elements.decision.textContent =
          `Decision: externally verified canonical ID ${decision.candidateId}.`;
      } else if (decision.status === "confirmed_no_match") {
        elements.decision.textContent =
          "Decision: confirmed absent after checking the full canonical dataset.";
      } else if (decision.status === "none_of_candidates") {
        elements.decision.textContent =
          "Decision: none of the displayed candidates. Further lookup is required.";
      } else if (decision.status === "manual_unverified") {
        elements.decision.textContent =
          `Unverified proposal: canonical ID ${decision.candidateId}. Validate it ` +
          "against the canonical dataset before treating it as ground truth.";
      } else if (decision.status === "pre_existing") {
        elements.decision.textContent =
          `Pre-existing label ${decision.candidateId}; review and confirm or change it.`;
      } else {
        elements.decision.textContent = "Decision: skipped / unsure.";
      }
    }

    function render() {
      const record = currentRecord();
      const decision = currentDecision();
      elements.sourceAddress.textContent = record.address || "Address unavailable";
      elements.sourcePostcode.textContent = record.postcode || "unavailable";
      elements.sourceId.textContent = `Source ID ${record.unique_id}`;
      elements.position.textContent = `${state.currentIndex + 1} of ${records.length}`;
      elements.previous.disabled = state.currentIndex === 0;
      elements.next.disabled = state.currentIndex === records.length - 1;
      elements.manualId.value = decision &&
        ["manual_unverified", "matched_manual", "pre_existing"]
          .includes(decision.status) &&
        !record.candidates.some(candidate => candidate.unique_id === decision.candidateId)
        ? decision.candidateId : "";
      renderCandidates(record, decision);
      renderDecision(decision);
      renderProgress();
      renderPersistenceStatus();
    }

    function chooseManualId() {
      const candidateId = elements.manualId.value.trim();
      if (!candidateId) {
        elements.decision.textContent = "Enter a canonical ID before selecting it.";
        elements.manualId.focus();
        return;
      }
      const candidateIndex = currentRecord().candidates.findIndex(
        candidate => candidate.unique_id === candidateId
      );
      if (candidateIndex >= 0) {
        selectCandidate(candidateIndex);
        return;
      }
      if (window.confirm(
        "This ID is not one of the displayed candidates and cannot be checked " +
        "offline. Record it as an unverified proposal?"
      )) {
        setDecision("manual_unverified", candidateId);
      }
    }

    function confirmManualId() {
      const candidateId = elements.manualId.value.trim();
      if (!candidateId) {
        elements.decision.textContent =
          "Enter an externally verified canonical ID before confirming it.";
        elements.manualId.focus();
        return;
      }
      const candidateIndex = currentRecord().candidates.findIndex(
        candidate => candidate.unique_id === candidateId
      );
      if (candidateIndex >= 0) {
        selectCandidate(candidateIndex);
        return;
      }
      if (window.confirm(
        "Confirm only after looking up this ID in the full canonical dataset and " +
        "checking its address. Record it as an adjudicated manual match?"
      )) {
        setDecision("matched_manual", candidateId);
      }
    }

    function confirmNoCanonicalMatch() {
      if (window.confirm(
        "Only confirm this after searching the full canonical dataset, not just " +
        "the displayed candidates. Confirm that no canonical property exists?"
      )) {
        setDecision("confirmed_no_match");
      }
    }

    function csvCell(value) {
      if (value === null || value === undefined) return "";
      if (typeof value === "number") {
        return Number.isFinite(value) ? String(value) : "";
      }
      const text = String(value);
      const formulaLike = /^[\t\r\n]|^\s*[=+\-@]/.test(text);
      const spreadsheetSafe = formulaLike ? `'${text}` : text;
      return `"${spreadsheetSafe.replaceAll('"', '""')}"`;
    }

    function exportRecords(exportedAt) {
      return records.map(record => {
        const decision = state.decisions[record.key] || null;
        const status = decision ? decision.status : "unreviewed";
        const selected = decision && decision.status === "matched"
          ? record.candidates.find(
              candidate => candidate.unique_id === decision.candidateId
            ) || null
          : null;
        const confirmedManual = decision && decision.status === "matched_manual"
          ? decision.candidateId : null;
        const proposed = decision &&
          ["manual_unverified", "pre_existing"].includes(decision.status)
          ? decision.candidateId : null;
        return {
          unique_id: record.unique_id,
          ukam_label: selected ? selected.unique_id : confirmedManual,
          label_status: status,
          proposed_ukam_label: proposed,
          selected_candidate_rank: selected ? selected.rank : null,
          selected_match_weight: selected ? selected.match_weight : null,
          recordset_id: recordsetId,
          exported_at_utc: exportedAt
        };
      });
    }

    function exportRows(exportedAt) {
      return exportRecords(exportedAt).map(record =>
        exportColumns.map(column => record[column] ?? "")
      );
    }

    function pairwiseExportRows(exportedAt) {
      const rows = [];
      records.forEach(record => {
        const decision = state.decisions[record.key] || null;
        const candidates = record.candidates.slice();
        if (decision && decision.status === "matched_manual" &&
            !candidates.some(candidate => candidate.unique_id === decision.candidateId)) {
          candidates.push({
            unique_id: decision.candidateId,
            address: "",
            postcode: "",
            match_weight: null,
            rank: ""
          });
        }
        if (candidates.length === 0) {
          candidates.push({
            unique_id: "",
            address: "",
            postcode: "",
            match_weight: null,
            rank: ""
          });
        }

        candidates.forEach(candidate => {
          let humanLabel = "";
          const labelDecision = decision ? decision.status : "unreviewed";
          if (decision) {
            if (["matched", "matched_manual"].includes(decision.status)) {
              humanLabel = candidate.unique_id === decision.candidateId ? "1" : "0";
            } else if (["none_of_candidates", "confirmed_no_match"]
                .includes(decision.status) && candidate.unique_id) {
              humanLabel = "0";
            }
          }
          rows.push([
            record.unique_id,
            record.address,
            record.postcode,
            candidate.unique_id,
            humanLabel,
            candidate.address,
            candidate.postcode,
            candidate.match_weight,
            candidate.rank,
            labelDecision,
            recordsetId,
            exportedAt
          ]);
        });
      });
      return rows;
    }

    function timestampSlug(exportedAt) {
      return exportedAt.replaceAll(/[-:]/g, "").replace(".000", "");
    }

    function downloadBlob(contents, type, filename) {
      const blob = new Blob(contents, {type});
      const url = URL.createObjectURL(blob);
      const link = document.createElement("a");
      link.href = url;
      link.download = filename;
      document.body.appendChild(link);
      link.click();
      link.remove();
      window.setTimeout(() => URL.revokeObjectURL(url), 1000);
    }

    function unresolvedCount() {
      return records.filter(record => {
        const decision = state.decisions[record.key];
        return !decision || !["matched", "matched_manual", "confirmed_no_match"]
          .includes(decision.status);
      }).length;
    }

    function confirmDraftExport() {
      const unresolved = unresolvedCount();
      return unresolved === 0 || window.confirm(
        `${unresolved} record(s) are unresolved. Download this as a draft anyway?`
      );
    }

    function downloadJson() {
      if (!confirmDraftExport()) return;
      const exportedAt = new Date().toISOString();
      const payload = {
        schema_version: 1,
        recordset_id: recordsetId,
        exported_at_utc: exportedAt,
        labels: exportRecords(exportedAt)
      };
      downloadBlob(
        [JSON.stringify(payload, null, 2), "\n"],
        "application/json;charset=utf-8",
        `ukam_labels_${recordsetId}_${timestampSlug(exportedAt)}.json`
      );
    }

    function downloadCsv() {
      if (!confirmDraftExport()) return;

      const exportedAt = new Date().toISOString();
      const csv = [exportColumns, ...exportRows(exportedAt)]
        .map(row => row.map(csvCell).join(","))
        .join("\r\n");
      downloadBlob(
        ["\ufeff", csv],
        "text/csv;charset=utf-8",
        `ukam_labels_${recordsetId}_${timestampSlug(exportedAt)}.csv`
      );
    }

    function downloadPairwiseCsv() {
      if (!window.confirm(
        "The candidate-pair CSV contains address text. Keep it in an approved " +
        "location and use only adjudicated human_label values. Continue?"
      )) return;
      const exportedAt = new Date().toISOString();
      const csv = [pairwiseExportColumns, ...pairwiseExportRows(exportedAt)]
        .map(row => row.map(csvCell).join(","))
        .join("\r\n");
      downloadBlob(
        ["\ufeff", csv],
        "text/csv;charset=utf-8",
        `ukam_candidate_labels_${recordsetId}_${timestampSlug(exportedAt)}.csv`
      );
    }

    function downloadCheckpoint() {
      const savedAt = new Date().toISOString();
      const checkpoint = {...serialisedState(), savedAt};
      downloadBlob(
        [JSON.stringify(checkpoint, null, 2), "\n"],
        "application/json;charset=utf-8",
        `ukam_checkpoint_${recordsetId}_${timestampSlug(savedAt)}.json`
      );
    }

    async function importCheckpoint() {
      const file = elements.importCheckpoint.files[0];
      elements.importCheckpoint.value = "";
      if (!file) return;
      try {
        const checkpoint = JSON.parse(await file.text());
        if (checkpoint.schemaVersion !== stateSchemaVersion ||
            checkpoint.recordsetId !== recordsetId) {
          throw new Error("This checkpoint belongs to a different record set.");
        }
        const decisions = normaliseDecisions(checkpoint.decisions, {strict: true});
        const savedAt = typeof checkpoint.savedAt === "string"
          ? checkpoint.savedAt : "an unknown time";
        const currentCount = Object.keys(state.decisions).length;
        const replacementCount = Object.keys(decisions).length;
        if (!window.confirm(
          `Replace ${currentCount} current decision(s) with ` +
          `${replacementCount} decision(s) saved at ${savedAt}?`
        )) return;
        state.decisions = decisions;
        state.currentIndex = Number.isInteger(checkpoint.currentIndex)
          ? Math.max(0, Math.min(checkpoint.currentIndex, records.length - 1)) : 0;
        saveState();
        render();
        elements.decision.textContent = "Checkpoint imported successfully.";
      } catch (error) {
        console.warn("Could not import checkpoint", error);
        elements.decision.textContent =
          `Checkpoint not imported: ${error instanceof Error ? error.message : error}`;
      }
    }

    function clearProgress() {
      if (storageConflict) {
        elements.decision.textContent =
          "Reload this file before clearing progress changed in another tab.";
        return;
      }
      if (!window.confirm(
        "Clear all decisions saved in this browser for this record set?"
      )) return;
      state.decisions = initialDecisions();
      state.currentIndex = 0;
      if (storageAvailable) {
        try {
          localStorage.removeItem(storageKey);
        } catch (error) {
          console.warn("Could not clear browser draft", error);
          storageAvailable = false;
          persistenceMessage =
            "Browser draft could not be cleared. Close this file before reopening it.";
        }
      }
      persistenceMessage = storageAvailable
        ? "Browser draft cleared. Pre-existing labels remain flagged for review."
        : persistenceMessage;
      render();
    }

    elements.previous.addEventListener("click", () => move(-1));
    elements.next.addEventListener("click", () => move(1));
    document.getElementById("next-unresolved").addEventListener(
      "click",
      moveToNextUnresolved
    );
    document.getElementById("none-shown").addEventListener(
      "click",
      () => setDecision("none_of_candidates")
    );
    document.getElementById("confirmed-no-match").addEventListener(
      "click",
      confirmNoCanonicalMatch
    );
    document.getElementById("skip").addEventListener(
      "click",
      () => setDecision("skipped")
    );
    document.getElementById("clear").addEventListener("click", clearDecision);
    document.getElementById("select-manual").addEventListener("click", chooseManualId);
    document.getElementById("confirm-manual").addEventListener(
      "click",
      confirmManualId
    );
    document.getElementById("download").addEventListener("click", downloadCsv);
    document.getElementById("download-json").addEventListener(
      "click",
      downloadJson
    );
    document.getElementById("download-pairwise").addEventListener(
      "click",
      downloadPairwiseCsv
    );
    document.getElementById("download-checkpoint").addEventListener(
      "click",
      downloadCheckpoint
    );
    elements.importCheckpoint.addEventListener("change", importCheckpoint);
    document.getElementById("import-trigger").addEventListener(
      "click",
      () => elements.importCheckpoint.click()
    );
    document.getElementById("clear-progress").addEventListener(
      "click",
      clearProgress
    );
    elements.manualId.addEventListener("keydown", event => {
      if (event.key === "Enter") chooseManualId();
    });
    document.addEventListener("keydown", event => {
      const modified = event.ctrlKey || event.metaKey || event.altKey;
      if (modified || event.defaultPrevented) return;
      if (event.target instanceof Element &&
          event.target.closest("button, a, input, textarea, select, label")) return;
      let handled = false;
      if (/^[1-9]$/.test(event.key)) {
        const candidateIndex = Number(event.key) - 1;
        if (currentRecord().candidates[candidateIndex]) {
          selectCandidate(candidateIndex, false);
          handled = true;
        }
      } else if (event.key.toLowerCase() === "n") {
        setDecision("none_of_candidates");
        handled = true;
      } else if (event.key.toLowerCase() === "s") {
        setDecision("skipped");
        handled = true;
      } else if (event.key === "ArrowLeft") {
        move(-1);
        handled = true;
      } else if (event.key === "ArrowRight") {
        move(1);
        handled = true;
      }
      if (handled) event.preventDefault();
    });

    window.addEventListener("storage", event => {
      if (event.key === storageKey) {
        storageConflict = true;
        persistenceMessage =
          "This browser draft changed in another tab. Reload before continuing.";
        elements.storageStatus.classList.add("warning");
        elements.storageStatus.textContent = persistenceMessage;
      }
    });

    render();
  </script>
</body>
</html>
"""
