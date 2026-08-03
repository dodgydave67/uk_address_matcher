from __future__ import annotations

import json
import re
import shutil
import stat
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from uk_address_matcher import AddressMatcher, SplinkStage
from uk_address_matcher.post_linkage.match_result import MatchResult


@pytest.fixture
def labelling_candidates(duck_con):
    duck_con.execute(
        r"""
        CREATE TABLE labelling_candidates AS
        SELECT *
        FROM (
            VALUES
                (
                    '90071992547409931234', '0001', 1,
                    '</script><script>alert("example")</script> '
                        || '__UKAM_STORAGE_KEY__',
                    'ZZ1 1ZZ', '1 Example Street, Exampletown', 'ZZ1 1ZZ',
                    12.5::DOUBLE, 1
                ),
                (
                    'C2', '0001', 1, 'Flat 2, 1 Example Street',
                    'ZZ1 1ZZ', '1 Example Street, Exampletown', 'ZZ1 1ZZ',
                    -8.0::DOUBLE, 2
                ),
                (
                    NULL, '0002', 2, NULL, NULL,
                    E'2 Example Road\nExampletown', NULL, NULL::DOUBLE, NULL
                )
        ) AS candidates(
            unique_id_l,
            unique_id_r,
            ukam_address_id_r,
            original_address_concat_l,
            postcode_l,
            address_concat_r,
            postcode_r,
            match_weight,
            candidate_rank
        )
        """
    )
    return duck_con.table("labelling_candidates")


def _match_result(duck_con, table_name: str = "labelling_candidates") -> MatchResult:
    return MatchResult(
        _relation=duck_con.table(table_name),
        con=duck_con,
        _splink_stage=SimpleNamespace(best_matches_table=table_name),
    )


def _application_json(html: str, element_id: str):
    match = re.search(
        rf'<script type="application/json" id="{element_id}">\s*(.*?)\s*</script>',
        html,
        flags=re.DOTALL,
    )
    assert match is not None, f"Missing embedded JSON element {element_id}"
    return json.loads(match.group(1))


def _records(path: Path) -> list[dict]:
    return _application_json(path.read_text(encoding="utf-8"), "ukam-records")


def test_create_labelling_tool_embeds_ranked_candidates(
    duck_con,
    labelling_candidates,
    tmp_path,
):
    result = _match_result(duck_con)
    output_path = tmp_path / "nested" / "labels.html"

    returned_path = result.create_labelling_tool(output_path)

    assert returned_path == output_path
    assert stat.S_IMODE(output_path.stat().st_mode) == 0o600
    records = _records(output_path)
    assert [record["unique_id"] for record in records] == ["0001", "0002"]
    assert records[0] == {
        "key": "1",
        "unique_id": "0001",
        "address": "1 Example Street, Exampletown",
        "postcode": "ZZ1 1ZZ",
        "initial_label": None,
        "candidates": [
            {
                "unique_id": "90071992547409931234",
                "address": (
                    '</script><script>alert("example")</script> __UKAM_STORAGE_KEY__'
                ),
                "postcode": "ZZ1 1ZZ",
                "match_weight": 12.5,
                "rank": 1,
            },
            {
                "unique_id": "C2",
                "address": "Flat 2, 1 Example Street",
                "postcode": "ZZ1 1ZZ",
                "match_weight": -8.0,
                "rank": 2,
            },
        ],
    }
    assert records[1]["address"] == "2 Example Road\nExampletown"
    assert records[1]["postcode"] == ""
    assert records[1]["candidates"] == []


def test_create_labelling_tool_caps_available_candidates(
    duck_con,
    labelling_candidates,
    tmp_path,
):
    output_path = _match_result(duck_con).create_labelling_tool(
        tmp_path / "labels.html",
        max_candidates=1,
    )

    records = _records(output_path)
    assert [candidate["rank"] for candidate in records[0]["candidates"]] == [1]
    assert records[1]["candidates"] == []


def test_create_labelling_tool_filters_to_requested_source_ids(
    duck_con,
    labelling_candidates,
    tmp_path,
):
    output_path = _match_result(duck_con).create_labelling_tool(
        tmp_path / "labels.html",
        messy_ids=["0001", "0001"],
    )

    records = _records(output_path)
    assert [record["unique_id"] for record in records] == ["0001"]
    assert [candidate["rank"] for candidate in records[0]["candidates"]] == [1, 2]


def test_create_labelling_tool_safely_embeds_address_text(
    duck_con,
    labelling_candidates,
    tmp_path,
):
    output_path = _match_result(duck_con).create_labelling_tool(tmp_path / "labels.html")
    html = output_path.read_text(encoding="utf-8")

    assert '</script><script>alert("example")</script>' not in html
    assert r"\u003c/script\u003e\u003cscript\u003e" in html
    assert "__UKAM_STORAGE_KEY__" in _records(output_path)[0]["candidates"][0]["address"]
    assert "Content-Security-Policy" in html
    assert "default-src 'none'" in html
    assert "textContent" in html


def test_create_labelling_tool_embeds_stable_export_columns(
    duck_con,
    labelling_candidates,
    tmp_path,
):
    output_path = _match_result(duck_con).create_labelling_tool(tmp_path / "labels.html")
    html = output_path.read_text(encoding="utf-8")

    assert _application_json(html, "ukam-export-columns") == [
        "unique_id",
        "ukam_label",
        "label_status",
        "proposed_ukam_label",
        "selected_candidate_rank",
        "selected_match_weight",
        "recordset_id",
        "exported_at_utc",
    ]
    assert _application_json(html, "ukam-pairwise-export-columns") == [
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
    ]
    for status in (
        "matched",
        "matched_manual",
        "confirmed_no_match",
        "none_of_candidates",
        "manual_unverified",
        "pre_existing",
        "skipped",
        "unreviewed",
    ):
        assert status in html


def test_create_labelling_tool_flags_existing_labels_for_review(
    duck_con,
    labelling_candidates,
    tmp_path,
):
    duck_con.execute(
        """
        CREATE TABLE labelled_candidates AS
        SELECT
            labelling_candidates.*,
            CASE WHEN unique_id_r = '0001' THEN 'C2' END AS ukam_label_r
        FROM labelling_candidates
        """
    )

    output_path = _match_result(duck_con, "labelled_candidates").create_labelling_tool(
        tmp_path / "labels.html"
    )

    records = _records(output_path)
    assert records[0]["initial_label"] == "C2"
    assert records[1]["initial_label"] is None
    assert 'status: "pre_existing"' in output_path.read_text(encoding="utf-8")


@pytest.mark.parametrize("max_candidates", [0, -1])
def test_create_labelling_tool_rejects_invalid_candidate_count(
    duck_con,
    labelling_candidates,
    tmp_path,
    max_candidates,
):
    with pytest.raises(ValueError, match="at least 1"):
        _match_result(duck_con).create_labelling_tool(
            tmp_path / "labels.html",
            max_candidates=max_candidates,
        )


@pytest.mark.parametrize("max_candidates", [True, 1.5, "2"])
def test_create_labelling_tool_requires_integer_candidate_count(
    duck_con,
    labelling_candidates,
    tmp_path,
    max_candidates,
):
    with pytest.raises(TypeError, match="must be an integer"):
        _match_result(duck_con).create_labelling_tool(
            tmp_path / "labels.html",
            max_candidates=max_candidates,
        )


@pytest.mark.parametrize("messy_ids", ["0001", ("0001",)])
def test_create_labelling_tool_requires_source_id_list(
    duck_con,
    labelling_candidates,
    tmp_path,
    messy_ids,
):
    with pytest.raises(TypeError, match="must be a list"):
        _match_result(duck_con).create_labelling_tool(
            tmp_path / "labels.html",
            messy_ids=messy_ids,
        )


@pytest.mark.parametrize("messy_ids", [[], [None]])
def test_create_labelling_tool_rejects_empty_or_null_source_ids(
    duck_con,
    labelling_candidates,
    tmp_path,
    messy_ids,
):
    with pytest.raises(ValueError, match="messy_ids"):
        _match_result(duck_con).create_labelling_tool(
            tmp_path / "labels.html",
            messy_ids=messy_ids,
        )


def test_create_labelling_tool_reports_unknown_source_ids(
    duck_con,
    labelling_candidates,
    tmp_path,
):
    with pytest.raises(ValueError, match="not found|missing"):
        _match_result(duck_con).create_labelling_tool(
            tmp_path / "labels.html",
            messy_ids=["0001", "does-not-exist"],
        )


def test_create_labelling_tool_rejects_duplicate_source_ids(
    duck_con,
    labelling_candidates,
    tmp_path,
):
    duck_con.execute(
        """
        CREATE TABLE duplicate_source_ids AS
        SELECT * FROM labelling_candidates
        UNION ALL BY NAME
        SELECT * REPLACE (3 AS ukam_address_id_r)
        FROM labelling_candidates
        WHERE ukam_address_id_r = 1
        """
    )

    with pytest.raises(ValueError, match="[Dd]uplicate.*source ID|source IDs.*unique"):
        _match_result(duck_con, "duplicate_source_ids").create_labelling_tool(
            tmp_path / "labels.html"
        )


def test_create_labelling_tool_requires_splink_candidate_data(
    duck_con,
    labelling_candidates,
    tmp_path,
):
    result = MatchResult(_relation=labelling_candidates, con=duck_con)

    with pytest.raises(ValueError, match="Splink|candidate"):
        result.create_labelling_tool(tmp_path / "labels.html")


def test_create_labelling_tool_reports_expired_candidate_data(
    duck_con,
    labelling_candidates,
    tmp_path,
):
    result = _match_result(duck_con)
    duck_con.execute("DROP TABLE labelling_candidates")

    with pytest.raises(ValueError, match="candidate|available|connection"):
        result.create_labelling_tool(tmp_path / "labels.html")


def test_create_labelling_tool_does_not_overwrite_by_default(
    duck_con,
    labelling_candidates,
    tmp_path,
):
    output_path = tmp_path / "labels.html"
    output_path.write_text("keep me", encoding="utf-8")
    result = _match_result(duck_con)

    with pytest.raises(FileExistsError):
        result.create_labelling_tool(output_path)
    assert output_path.read_text(encoding="utf-8") == "keep me"

    result.create_labelling_tool(output_path, overwrite=True)
    assert "UK Address Matcher" in output_path.read_text(encoding="utf-8")
    assert stat.S_IMODE(output_path.stat().st_mode) == 0o600


def test_create_labelling_tool_uses_address_matcher_candidates(duck_con, tmp_path):
    canonical = duck_con.sql(
        """
        SELECT * FROM (VALUES
            ('C1', '1 Example Street, Exampletown', 'ZZ1 1ZZ'),
            ('C2', '2 Example Street, Exampletown', 'ZZ1 1ZZ')
        ) AS t(unique_id, address_concat, postcode)
        """
    )
    messy = duck_con.sql(
        """
        SELECT * FROM (VALUES
            ('M1', '1 Example St, Exampletown', 'ZZ1 1ZZ'),
            ('M2', '2 Example St, Exampletown', 'ZZ1 1ZZ')
        ) AS t(unique_id, address_concat, postcode)
        """
    )
    matcher = AddressMatcher(
        canonical_addresses=canonical,
        addresses_to_match=messy,
        con=duck_con,
        stages=[
            SplinkStage(
                predict_threshold_match_weight=-50,
                improve_threshold_match_weight=-50,
                final_match_weight_threshold=-50,
                final_distinguishability_threshold=None,
            )
        ],
    )

    output_path = matcher.match().create_labelling_tool(tmp_path / "labels.html")

    records = _records(output_path)
    assert [record["unique_id"] for record in records] == ["M1", "M2"]
    assert [record["candidates"][0]["unique_id"] for record in records] == [
        "C1",
        "C2",
    ]
    assert all(record["candidates"][0]["rank"] == 1 for record in records)
    assert all(
        isinstance(record["candidates"][0]["match_weight"], float) for record in records
    )


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js is unavailable")
def test_labelling_javascript_exports_consistent_safe_rows(
    duck_con,
    labelling_candidates,
    tmp_path,
):
    output_path = _match_result(duck_con).create_labelling_tool(tmp_path / "labels.html")
    html = output_path.read_text(encoding="utf-8")
    records_json = json.dumps(_application_json(html, "ukam-records"))
    export_columns_json = json.dumps(_application_json(html, "ukam-export-columns"))
    pairwise_columns_json = json.dumps(
        _application_json(html, "ukam-pairwise-export-columns")
    )
    script_match = re.search(
        r"<script>\s*(.*?)\s*</script>\s*</body>",
        html,
        flags=re.DOTALL,
    )
    assert script_match is not None

    runner_path = tmp_path / "exercise_exports.cjs"
    result_path = tmp_path / "exports.json"
    runner = f"""
const fs = require("node:fs");

class MockElement {{
  constructor(textContent = "") {{
    this.textContent = textContent;
    this.value = "";
    this.files = [];
    this.dataset = {{}};
    this.classList = {{add() {{}}, toggle() {{}}}};
  }}
  addEventListener() {{}}
  appendChild() {{}}
  replaceChildren() {{}}
  setAttribute() {{}}
  querySelector() {{ return null; }}
  focus() {{}}
  click() {{}}
  remove() {{}}
  closest() {{ return null; }}
}}

global.Element = MockElement;
const mockElements = new Map([
  ["ukam-records", new MockElement(JSON.stringify({records_json}))],
  ["ukam-export-columns", new MockElement(JSON.stringify({export_columns_json}))],
  ["ukam-pairwise-export-columns",
    new MockElement(JSON.stringify({pairwise_columns_json}))]
]);
const getElement = id => {{
  if (!mockElements.has(id)) mockElements.set(id, new MockElement());
  return mockElements.get(id);
}};
global.document = {{
  getElementById: getElement,
  createElement: () => new MockElement(),
  addEventListener() {{}},
  body: new MockElement()
}};
global.localStorage = {{
  getItem() {{ return null; }},
  setItem() {{}},
  removeItem() {{}}
}};
global.window = {{
  addEventListener() {{}},
  confirm() {{ return true; }},
  setTimeout
}};

{script_match.group(1)}

const exportedAt = "2026-07-14T12:00:00.000Z";
state.decisions["1"] = {{status: "matched", candidateId: "C2"}};
state.decisions["2"] = {{status: "confirmed_no_match", candidateId: null}};
const labels = exportRecords(exportedAt);
const pairwise = pairwiseExportRows(exportedAt);
const labelCsv = [exportColumns, ...exportRows(exportedAt)]
  .map(row => row.map(csvCell).join(","))
  .join("\\r\\n");
const pairwiseCsv = [pairwiseExportColumns, ...pairwise]
  .map(row => row.map(csvCell).join(","))
  .join("\\r\\n");
fs.writeFileSync(process.argv[2], JSON.stringify({{
  labels,
  pairwise,
  labelCsv,
  pairwiseCsv,
  formulaCell: csvCell("=2+2"),
  negativeNumberCell: csvCell(-8)
}}));
"""
    runner_path.write_text(runner, encoding="utf-8")

    subprocess.run(
        [shutil.which("node"), str(runner_path), str(result_path)],
        check=True,
        capture_output=True,
        text=True,
    )
    exported = json.loads(result_path.read_text(encoding="utf-8"))

    assert exported["labels"][0] == {
        "unique_id": "0001",
        "ukam_label": "C2",
        "label_status": "matched",
        "proposed_ukam_label": None,
        "selected_candidate_rank": 2,
        "selected_match_weight": -8,
        "recordset_id": exported["labels"][0]["recordset_id"],
        "exported_at_utc": "2026-07-14T12:00:00.000Z",
    }
    assert exported["labels"][1]["ukam_label"] is None
    assert exported["labels"][1]["label_status"] == "confirmed_no_match"
    assert exported["labels"][0]["recordset_id"] == exported["labels"][1]["recordset_id"]

    candidate_rows = [row for row in exported["pairwise"] if row[0] == "0001"]
    assert candidate_rows[0][3] == "90071992547409931234"
    assert candidate_rows[0][4] == "0"
    assert candidate_rows[1][3:5] == ["C2", "1"]
    assert candidate_rows[1][7:9] == [-8, 2]
    assert exported["pairwise"][-1][9] == "confirmed_no_match"
    assert exported["formulaCell"] == '"\'=2+2"'
    assert exported["negativeNumberCell"] == "-8"
    assert '"0001","C2","matched"' in exported["labelCsv"]
    assert '"90071992547409931234","0"' in exported["pairwiseCsv"]
