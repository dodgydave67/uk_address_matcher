# Geting started

## Install

`uk_address_matcher` is a Python package available on PyPI. You can install with `pip`:

```bash
pip install uk_address_matcher
```


## Input data requirements

Both your messy addresses and your canonical addresses need at least these
columns:

| Column | Description |
|--------|-------------|
| `unique_id` | Stable unique identifier |
| `address_concat` | Address text, which can include the postcode |

Optionally you can provide:

| Column | Description |
|--------|-------------|
| `postcode` | If provided, this postcode is used in favour over any postcode provided in `address_concat` |
| `ukam_label` | The unique ID of the true match. If provided, it enables accuracy analysis output |


## Choose whether to pre-process your canonical dataset

If you're linking to a small canonical dataset (of say, less than 500,000 rows), then it's simplest to process the data on-the-fly.

If you're linking to a large canonical dataset (for example, national-scale NGD), then we recommend a one-time pre-processing step. It computes reusable datasets (indices and feature tables) once, so subsequent matching runs are fast.

The examples below use the fictional London datasets from `ukam_datasets`, which are included for runnable examples.



=== "Local / regional (processing on-the-fly)"

    ```python exec="true" source="tabbed-left" tabs="Source code|Output"
    import duckdb
    from uk_address_matcher import AddressMatcher, ukam_datasets

    con = duckdb.connect()

    df_messy = ukam_datasets.as_relation("fictional_london_messy", con=con)
    df_canonical = ukam_datasets.as_relation("fictional_london_canonical", con=con)

    matcher = AddressMatcher(
        canonical_addresses=df_canonical,
        addresses_to_match=df_messy,
        con=con,
    )
    result = matcher.match()
    print(result.matches().limit(5).to_df().to_markdown(index=False))
    ```

=== "National-scale (preprocessed data)"


    ```python exec="true" source="tabbed-left" tabs="Source code|Output"

    import duckdb
    import os
    import tempfile
    from uk_address_matcher import (
        AddressMatcher,
        prepare_canonical_folder,
        ukam_datasets,
    )

    con = duckdb.connect()
    df_messy = ukam_datasets.as_relation("fictional_london_messy", con=con)
    df_canonical = ukam_datasets.as_relation("fictional_london_canonical", con=con)

    # One-time preparation
    output_folder = tempfile.mkdtemp()
    prepare_canonical_folder(
        df_canonical,
        output_folder=output_folder,
        con=con,
        overwrite=True,
    )

    # Pass the folder path instead of a relation
    matcher = AddressMatcher(
        canonical_addresses=output_folder,
        addresses_to_match=df_messy,
        con=con,
    )
    result = matcher.match()

    print("Prepared folder contents:")
    for f in sorted(os.listdir(output_folder)):
        print(f"  {f}")
    print()
    print(result.matches().limit(5).to_df().to_markdown(index=False))
    ```

    The `output_folder` contains parquet files plus `ukam_manifest.json`
    (package version, row counts, file hashes) for reproducibility.

    Subsequent matching exercises that use the same canonical data can reuse this folder, skipping the `prepare_canonical_folder` step.

## Reading results

`matcher.match()` returns a `MatchResult` object:


| Property / method | Returns |
|-------------------|---------|
| `.matches()` | DuckDB relation with `unique_id`, `resolved_canonical_id`, `match_reason`, and more. |
| `.match_metrics()` | Match-reason breakdown with counts and percentages. |
| `.accuracy_analysis()` | Threshold-based accuracy analysis from labelled data (requires `ukam_label` in messy input). |


??? info "Customising stages"

    The default pipeline is `ExactMatchStage` → `SplinkStage`. Pass your own
    `stages` list to change behaviour:

    ```python
    from uk_address_matcher import (
        AddressMatcher,
        ExactMatchStage,
        PeeledAddressStage,
        UniqueTrigramStage,
        SplinkStage,
    )

    matcher = AddressMatcher(
        canonical_addresses=df_canonical,
        addresses_to_match=df_messy,
        con=con,
        stages=[
            ExactMatchStage(),
            PeeledAddressStage(),
            UniqueTrigramStage(),
            SplinkStage(
                final_match_weight_threshold=20.0,
                final_distinguishability_threshold=5.0,
            ),
        ],
    )
    ```

    Use `AddressMatcher.available_stages()` to discover registered stage classes. See [Choosing a matching threshold](choosing_a_matching_threshold.md) and [Optimising accuracy](optimising_accuracy.md) for further accuracy advice. The [API reference](api_reference.md) covers the main API docs.

## Using labelled data

If you know the correct match for each address, add a `ukam_label` column to
your messy data. It propagates through to results, enabling accuracy analysis
with `MatchResult.accuracy_analysis()`.

### Create labels by reviewing candidates

After matching, create a local HTML tool for the records that reached the
`SplinkStage`:

```python
tool_path = result.create_labelling_tool(
    "address_labelling.html",
    max_candidates=5,
    messy_ids=["M123", "M456"],  # Omit to review every record that reached Splink
)
print(tool_path)
```

Open the generated file in a browser. It works without a server or internet
connection, supports keyboard shortcuts, and reports whether browser draft
autosave is available. Download JSON checkpoints regularly: browser storage for
local files is browser-dependent. No model candidate is selected automatically,
and model scores are hidden during review. Use **Download exact labels JSON**
when identifier round-tripping matters. The parallel labels CSV is hardened for
opening in spreadsheet software.

The JSON `labels` array and labels CSV have one row per source record:

| Column | Meaning |
|--------|---------|
| `unique_id` | Source record's `unique_id` |
| `ukam_label` | Confirmed canonical `unique_id`; blank for a confirmed non-match or unresolved record |
| `label_status` | `matched`, `matched_manual`, `confirmed_no_match`, `none_of_candidates`, `manual_unverified`, `pre_existing`, `skipped`, or `unreviewed` |
| `proposed_ukam_label` | An unverified manual or pre-existing label requiring review |
| `selected_candidate_rank` | Rank of a confirmed displayed candidate |
| `selected_match_weight` | Model weight retained for audit, but not shown during review |
| `recordset_id` | Fingerprint identifying the generated review set |
| `exported_at_utc` | Export timestamp |

Only `matched`, `matched_manual`, and `confirmed_no_match` are adjudicated ground
truth. Filter to those statuses and **inner join** the filtered rows to the
original data before accuracy analysis. This retains confirmed non-matches as a
null `ukam_label` without turning skipped or unreviewed records into negatives.

“None of these candidates” rejects only the displayed candidate set; it does not
claim that the property is absent from the full canonical data. “Confirm absent
from canonical” asks for explicit confirmation that a separate full lookup was
performed. A manually entered ID outside the displayed candidates starts as
`manual_unverified`. After checking that ID and its address in the full canonical
dataset, the reviewer can explicitly promote it to `matched_manual`. Non-null
labels already present in the input are similarly flagged as `pre_existing`
until reviewed.

For compatibility with the external-labels benchmark, expand **Compatibility
export for candidate-pair workflows** and download the optional CSV. Its first
five columns remain:

| Column | Meaning |
|--------|---------|
| `id` | Source record's `unique_id` |
| `messy_address` | Source address shown to the reviewer |
| `messy_postcode` | Source postcode shown to the reviewer |
| `unique_id_l` | Candidate canonical `unique_id` |
| `human_label` | `1` for the selected candidate, `0` for an explicitly rejected displayed candidate, or blank when unresolved |

After validating the compatibility export, copy or rename
`ukam_candidate_labels_*.csv` to `address_matching_labels/export.csv` for the
external-labels workflow. That benchmark consumes only rows where
`human_label = 1`; its explicit zero labels are retained for other pairwise uses.

This compatibility file contains address text. CSV cells that start like a
spreadsheet formula (optional whitespace followed by `=`, `+`, `-`, or `@`, or
a leading tab/newline control character) receive a leading apostrophe. Numeric
values, including negative match weights, remain numeric. Use the exact labels
JSON when a legitimate string identifier starts with one of those characters;
remove a CSV safeguard only after validation.

`max_candidates` is a display cap. The available pool was set when the matcher
ran by `SplinkStage.improve_top_n_matches` and
`SplinkStage.improve_threshold_match_weight`. Run with only a permissive
`SplinkStage` if every source record should reach clerical review.

The selected source `unique_id` values must be unique so exported labels can be
joined unambiguously. For large runs, use `messy_ids` to create manageable review
batches; embedding every retained address can produce a large HTML file and
browser draft.

The HTML and compatibility candidate-pair CSV contain address data; every export
contains identifiers or decisions. Keep them in an approved local location and
do not commit real labelled data to the repository.
