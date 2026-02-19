# taxid-minting-process
This repository outlines the process of generating taxonomic IDs (taxids) creation request (TSV) request forms from hierarchical taxonomy data at scale. The pipeline is designed for use in large biodiversity genomics submissions (e.g. UKBOL, BGE) where hundreds to thousands of specimens require validation on whether ENA taxids exist for the taxa before request forms are generated. 

The pipeline handles the full complexity of real-world museum specimen taxonomy: non-species-level identifications, homonyms across kingdoms, synonyms, names absent from ENA, and manual verification edge cases.

---

## Overview

The pipeline takes a flat CSV of specimen records with hierarchical taxonomic annotations and produces TSV request forms suitable for submission to ENA's taxid minting service. It does this by:

1. Splitting records by identification rank (species vs. non-species)
2. Validating names directly against the ENA taxonomy API
3. Using GBIF as an authority to check taxonomy validity for names not found in ENA
4. Routing records that cannot be resolved programmatically into a structured manual verification step
5. Packaging all resolved names into correctly formatted taxid request forms

The full pipeline involves **8 scripts** and several manual steps.

---

## Requirements

- Python 3.8+
- `pandas`
- `requests`
- `pygbif`
- `openpyxl` (for `.xlsx` output in later steps)

Install dependencies:

```bash
pip install pandas requests pygbif openpyxl
```

Internet access is required for ENA and GBIF API queries. API rate limits are respected internally by each script (ENA: 25 queries/second; GBIF: ~10 queries/second).

---

## Input Data

The pipeline expects a **CSV or TSV file** containing one row per specimen, with the following columns (column names are case-sensitive):

| Column | Required | Description |
|---|---|---|
| `Sample ID` (or allowed alternative) | Yes | Unique specimen/sample identifier |
| `species` | Yes | Species-level identification (leave empty if not identified to species) |
| `genus` | Yes | Genus (leave empty if not identified to genus) |
| `family` | Yes | Family (leave empty if not identified to family) |
| `order` | Yes | Order (leave empty if not identified to order) |
| `class` | Yes | Class (leave empty if not identified to class) |
| `phylum` | Yes | Phylum — critical for homonym resolution |
| `taxid` | No | Pre-existing NCBI taxid (passed through) |
| `matched_rank` | No | Rank of NCBI match (passed through) |
| `lineage` | No | NCBI lineage string (passed through) |
| `lineage_mismatch` | No | Flag for taxonomy conflicts (passed through) |
| `type_status` | Yes | Type specimen status (passed through at this stage but required later) |

> **Note on `phylum`:** Always populate this column where possible. It is the primary mechanism by which the pipeline detects and resolves homonyms - taxonomic names that exist in multiple kingdoms (e.g. a genus name shared between Arthropoda and Rhodophyta). Without phylum, homonym mismatches cannot be detected.

---

## Pipeline Steps

### Step 1 — Filter Taxonomy (`00_filter_taxonomy.py`)

**Purpose:** Split the input specimen metadata into two pools: those identified to species level, and those identified only to genus or higher rank. Determines the `scientificName` to be used downstream for each record by traversing the taxonomic hierarchy from most to least specific.

**Run:**
```bash
python 00_filter_taxonomy.py \
    --input /path/to/specimen_metadata.csv \
    --output ./taxid_request/00_filter_taxonomy_out.csv \
    --id-col "Sample ID"
```

| Argument | Description |
|---|---|
| `--input` / `-i` | Path to input CSV/TSV |
| `--output` / `-o` | Base path for output files (script generates `_species` and `_non_species` variants automatically) |
| `--id-col` | Name of the column to use as the specimen ID (will be renamed to `ID` in output) |

**Logic:**
For each row, the script determines a `scientificName` by checking columns in this order: `species → genus → family → order → class → phylum`. The first non-empty, non-placeholder value found becomes the `scientificName`, and `identified_rank` is set accordingly. Values of `"unspecified"` or `"not collected"` are treated as empty.

Rows are then split:
- **Species output:** rows where the `species` column is populated
- **Non-species output:** rows where `species` is empty (identified to genus or higher)

**Outputs:**
| File | Description |
|---|---|
| `*_species.csv` | Records identified to species level |
| `*_non_species.csv` | Records identified to genus level or higher |
| `non_species_list.txt` | Formatted list of non-species names |

**Example output columns:** `ID`, `scientificName`, `identified_rank`, `phylum`, `class`, `order`, `family`, `genus`, `species`, `NCBI_taxid`, `NCBI_matched_rank`, `NCBI_lineage`, `lineage_mismatch`, `type_status`, `note`
- `scientificName` — the most specific available taxonomic name
- `identified_rank` — the rank at which that name was assigned (species, genus, family, etc.)
- `note` — warnings if no taxonomy could be assigned



---



### Step 2 — Non-Species Taxid Request (`requester_non-species.py`)

**Purpose:** Handles the records identified only to genus level or higher (from step 1). These cannot be submitted with a species-level taxid, and ENA requires a specific format for requesting IDs for such specimens (formatted as `[rank] sp. [Sample ID]`).

**Run:**
```bash
python requester_non-species.py \
    --input ./taxid_request/00_filter_taxonomy_out_non_species.csv \
    --output ./taxid_request/non-species_request/non-species_request_form.tsv \
    --project_id "YOUR_PROJECT_ID"
```

| Argument | Description |
|---|---|
| `--input` | Non-species CSV from Step 1 |
| `--output` | Output path for the TSV request form |
| `--project_id` | Your project identifier string (e.g. `"UKBOL_accelerated"`) |

**Logic:**
The script takes the lowest identified rank for the samples not identified to species-level, and queries the GBIF backbone taxonomy database to retrieve the GBIF usageKey for that taxonomic rank. A GBIF species URL, containing the returned usageKey is used to populate the 'description' column of the output request TSV (https://www.gbif.org/species/[usageKey]).

Where a name cannot be matched directly, the script attempts disambiguation using the available higher taxonomy to help 'anchor' a second GBIF search. Records flagged as "disambiguated" in the log are ones where the initial match was too broad and a more specific match was found using genus/family constraints.

**Outputs:**
| File | Description |
|---|---|
| `non-species_request_form.tsv` | TSV request form ready for submission |

**Diagnostics to check:**
- `Successful matches` should equal `Total samples processed`
- `Failed matches: 0` — any failures require manual resolution before proceeding
- `Disambiguated samples` — review these to confirm the correct taxid was selected

**The non-species TSV produced here is a completed request form.** Set it aside; it will be combined with species-level request forms in Step 11.



---



### Step 3 — ENA Taxid Check, Round 1 (`01_ena_taxid_check4.py`)

**Purpose:** Query the ENA taxonomy API for each species-level record from Step 1. Identifies which names are already present in ENA (and taxonomically consistent), and which require further processing.

**Run:**
```bash
python 01_ena_taxid_check.py \
    --input ./taxid_request/00_filter_taxonomy_out_species.csv \
    --output ./taxid_request/01_ena_taxid_check/01_ena_check_species.csv
```

| Argument | Description |
|---|---|
| `--input` / `--in` | Species-level CSV from Step 1 |
| `--output` / `--out` | Base path for output files (script generates split files automatically) |

**Logic:**
For each record, the script:
1. Reads `scientificName` and strips any ambiguity qualifiers (e.g. 'cf.' or 'nr.') before querying (e.g. `"Genus cf. species"` → `"Genus species"`)
2. Queries the ENA suggest-for-submission API endpoint
3. Checks returned results for an exact match against `scientificName` (case-insensitive), and also checks the `otherNames` field for synonym matches
4. For each match found, fetches the full ENA lineage via the taxid endpoint, and verifies that the input `phylum` appears in the returned lineage (another homonym detection step)
5. Assigns an ENA status code to each record

| Status code | Meaning | Action |
|---|---|---|
| `MATCH_TAXONOMICALLY_CONSISTENT` | Exact match found and phylum verified | ✅ No taxid needed — excluded from further processing |
| `NO_EXACT_MATCH` | ENA returned results but none matched exactly | ⚠️ Requires GBIF search (Step 4) |
| `NO_RESULTS_RETURNED` | ENA returned no suggestions at all | ⚠️ Requires GBIF search (Step 4) |
| `MATCH_PHYLUM_UNCHECKED` | Match found but no phylum to verify against | ⚠️ Possible homonym — manual review |
| `TAXONOMIC_MISMATCH_HOMONYM` | Match found but phylum is wrong kingdom | ⚠️ Homonym — requires GBIF search |
| `MAN_VER_NAME_EMPTY` | No `scientificName` provided | ❌ Data error — fix input |
| `API_ERROR: *` | API communication failure | ❌ Retry or check connectivity |

**Outputs:**
| File | Description |
|---|---|
| `*-ena_matches.csv` | `MATCH_TAXONOMICALLY_CONSISTENT` — End |
| `*-no_ena_matches.csv` | `NO_EXACT_MATCH` / `NO_RESULTS_RETURNED` — proceed to GBIF search |
| `*-possible_homonym.csv` | `MATCH_PHYLUM_UNCHECKED` / `TAXONOMIC_MISMATCH_HOMONYM` — review |
| `*-errors.csv` | `MAN_VER_NAME_EMPTY` / `API_ERROR` — review and fix input |
| `*-filtered.csv` | All records **except** `MATCH_TAXONOMICALLY_CONSISTENT` — this is the input to Step 4 |
| `01_ena_taxid_check.log` | Full API call log with match decisions |

**The `-filtered.csv` file is the input for the next step** — it contains everything that still needs resolution.
The `*-ena_matches.csv` records have confirmed ENA taxids. These do not require a new taxid request and can be used directly in submission metadata.

**Diagnostics to check:**
- `errors.csv` should be empty — Fix sample data in input file. If any API errors suggest connectivity issues; re-run affected records
- `possible_homonym.csv` should be empty if `phylum` is consistently populated in your input data



---



### Step 4 — GBIF Backbone Search, Round 1 (`02_gbif_backbone_search2.py`)

**Purpose:** For records that did not match in ENA (the `-filtered.csv` from Step 3), query the GBIF backbone taxonomy to retrieve canonical name information, synonymy, and authoritative species keys. GBIF is intended to serve as a source of truth for names not yet registered in ENA.

**Run:**
```bash
python 02_gbif_backbone_search.py \
    --input ./taxid_request/01_ena_taxid_check/01_ena_check_species-filtered.csv \
    --output ./taxid_request/02_gbif_search/02_gbif_output_1.csv
```

| Argument | Description |
|---|---|
| `-i` / `--input` | Filtered CSV from Step 3 |
| `-o` / `--output` | Output CSV path |

**Logic:**
The script implements a multi-stage search strategy for each record:

**Stage 1 — Standard search.** Queries `name_backbone()` with the `scientificName` only (no taxonomic constraints). If the result is valid (has a usageKey, is not too broad), this result is used.
**Stage 2 — Disambiguation.** If Stage 1 returns `usageKey=1` (Animalia root, meaning the name matched the entire animal kingdom) or a rank of `KINGDOM`/`PHYLUM`, the search is retried with `phylum`, `order`, and/or `family` as constraints. This handles cases like genus names that are homonyms across multiple families.
**Stage 3 — Homonym resolution.** If the ENA status for this record was `TAXONOMIC_MISMATCH_HOMONYM`, the search goes directly to a phylum-constrained lookup, bypassing Stage 1.
**Stage 4 — Fuzzy lookup.** If all above stages fail to find a valid match, queries with `verbose=True` to retrieve alternative matches, filtered by phylum (and optionally family) to catch misspellings. This catches genuine misspellings in the input data (e.g. `"Etone flava"` → `"Eteone flava"`).


**Key output columns added:**
| Column | Description |
|---|---|
| `gbif_usageKey` | GBIF usage key for the matched taxon |
| `gbif_scientificName` | Full scientific name as recorded in GBIF |
| `gbif_canonicalName` | Name without authorship |
| `gbif_rank` | Taxonomic rank of the match |
| `gbif_status` | `ACCEPTED`, `SYNONYM`, `DOUBTFUL`, etc. |
| `gbif_matchType` | `EXACT`, `FUZZY`, `HIGHERRANK`, `NONE` |
| `gbif_confidence` | Match confidence score (0–100) |
| `gbif_species` | Accepted species name (useful when input is a synonym) |
| `gbif_speciesKey` | GBIF key for the accepted species |
| `gbif_notes` | How the match was resolved (e.g. `homonym_resolved_with_phylum`, `suggested_match_from_fuzzy_alternative`) |

**GBIF match types explained:**
| `gbif_matchType` | Meaning |
|---|---|
| `EXACT` | Name matched exactly |
| `FUZZY` | Name matched with spelling correction |
| `FUZZY_ALTERNATIVE` | Match came from verbose alternatives list |
| `NONE` | No match found |

**`gbif_notes` values explained:**
| Note | Meaning |
|---|---|
| *(empty)* | Standard match, no special handling |
| `homonym_resolved_with_phylum` | Phylum constraint used to resolve kingdom-level homonym |
| `disambiguated_with_phylum=X+...` | Broader match narrowed using higher taxonomy |
| `suggested_match_from_fuzzy_alternative` | Match found via verbose alternatives — check recommended |
| `no_match_found` | No match found at any stage |
| `disambiguation_failed` | Disambiguation attempted but still no valid result |
| `fuzzy_search_failed` | Fuzzy lookup also failed |
| `api_error` | GBIF API error |

**Output columns added (all prefixed `gbif_`):**
`usageKey`, `scientificName`, `canonicalName`, `rank`, `status`, `matchType`, `confidence`, `kingdom`, `phylum`, `class`, `order`, `family`, `genus`, `species`, plus key columns for each rank, `acceptedUsageKey`, and `gbif_notes`.

**Diagnostics to check:**
- A `match rate` of 100% is expected for well-curated input data — any `no_match_found` records likely have spelling errors or use names not yet in GBIF
- Records with `suggested_match_from_fuzzy_alternative` in `gbif_notes` should be checked manually — the fuzzy match may or may not be the intended taxon
- `gbif_status == 'SYNONYM'` records will have an `acceptedUsageKey` pointing to the accepted name — the GBIF processor in Step 5 handles these automatically



---


## Step 5 - Processing Name Processing (`gbif_processor.py`)

**Purpose:** This step applies a rules-based decision matrix to the GBIF backbone taxonomy match results from Step 4, determining whether the original submitted name or the GBIF-matched name should be used for each specimen in the downstream ENA taxonomy submission.

**Run:**
```bash
python gbif_name_processor.py -i ./taxid_request/02_gbif_search/02_gbif_output_1.csv -r gbif_rules.csv -o ./output --project-id [PROJECT_ID[
```

| Argument | Description |
|---|---|
| `-i / --input` | Input CSV file (GBIF match results from Step 4) |
| `-r / --rules` | Rules matrix CSV file |
| `-o / --output-dir` | Output directory (default: same directory as input) |
| `-p / --project-id` | Project ID to populate in taxonomy request TSV (default: `BGE`) |

**Logic:** Each specimen's GBIF match is evaluated against a configurable rules matrix (`gbif_rules.csv`). The script compares the original scientific name against GBIF's returned match across three axes — species epithet similarity, genus similarity, and type specimen status, then combines these with the GBIF match `status` (e.g. `ACCEPTED`, `SYNONYM`, `DOUBTFUL`) and `matchType` (e.g. `EXACT`, `FUZZY`) to look up the appropriate action in the rules matrix.

For each specimen the script:
1. Extracts the species epithet and genus from both the original `scientificName` and the GBIF-returned `gbif_species` and `gbif_genus` fields.
2. Compares them to determine whether they are `same` or `different` at the epithet and genus levels.
3. Checks whether the specimen has a type designation (`type_status` column).
4. Constructs a rule key `(status, matchType, species_match, genus_match, is_type)` and looks it up in the loaded rules matrix.
5. Using the rules matrix, the script then evaluates each record's GBIF result and assigns one of the following `name_to_use` values:

| `name_to_use` | Meaning |
|---|---|
| `original` | The original `scientificName` is correct and accepted in GBIF. Proceed to taxid request. |
| `gbif (check ENA)` | GBIF found a different accepted name (e.g. the input name is a synonym). The GBIF-accepted name should be re-checked against ENA. |
| `uncertain - check placement` | GBIF match is ambiguous or taxonomic placement is uncertain. Requires manual review. |
| `not a possible combination` | GBIF flagged the name as taxonomically invalid. Requires manual review. |

**Outputs:**
| File | Description |
|---|---|
| `*_request_taxid.tsv` | Ready-to-submit taxid request TSV for `name_to_use == original` records and no manual verification is required. Deduplicated by name. Description column contains a GBIF species URL (`gbif_speciesKey`), falling back to the genus URL (`gbif_genusKey`) if the species key is absent. Type specimens have `\| TYPE` appended to the description. `name_type` is `published_name` for species-key matches or `novel_species` for genus-key fallbacks |
| `*_check_ENA.xlsx` | Specimens where `name_to_use == gbif`, requiring ENA validation using the GBIF-matched name. Contains all input columns |
| `*_annotated.xlsx` | Full input for all records, with four appended decision columns: `name_to_use`, `description_text`, `check_ENA_with_GBIF`, `manual_verification_needed` |
| `*_manually_verify.xlsx` | All specimens flagged for manual verification (see Step 6). Contains all input columns plus decision columns |

**Example Log Output (per sample):**
```
================================================================================
Sample: BSUIO096-24
  Scientific name (original): Teratocoris discolor
  GBIF species: Teratocoris discolor
  GBIF genus: Teratocoris
  Status: ACCEPTED
  MatchType: EXACT
  Original genus: Teratocoris | GBIF genus: Teratocoris → Genus match: SAME
  Original epithet: discolor | GBIF epithet: discolor → Species match: SAME
  Type specimen: NO
  Rule key: (ACCEPTED, EXACT, same, same, NO)
  Rule source: RULES_MATRIX
  → name_to_use: original
```

**Records assigned `original` (with confirmed GBIF matches) are formatted directly into a taxid request TSV. The `*_request_taxid.tsv` produced here is a completed request form** for programmatically resolved species. Set it aside for Step 11.



---



### Step 6 — Manual Verification









–8 (Documentation in Progress)

The remaining steps are:

| Step | Script | Purpose |
|---|---|---|
| 6 | *(manual — Ben's Gemini Gem)* | Manual verification of scientificNames that could not be resolved programmatically |
| 7 | `04_post_ver_ena_check.py` | Re-run ENA check using manually verified/corrected names |
| 8 | `05_post_ver_gbif_check.py` | Re-run GBIF search for names that still have no ENA match after verification |
| 9 | *(manual)* | Final verification of unresolved records |
| 10 | `06_final_species_request.py` | Format all resolved species records into the final taxid request TSV |
| 11 | *(manual)* | Concatenate all three request forms (non-species, programmatic species, manually verified species) into a single submission file |

---

## Output Files for Submission

At the end of the pipeline, three TSV request forms are produced and concatenated into a single file:

| Form | Source | Description |
|---|---|---|
| Non-species request form | `requester_non-species.py` | Records identified to genus or higher |
| Species request form (programmatic) | `gbif_name_processor2.py` | Species names resolved via ENA/GBIF without manual intervention |
| Species request form (manual) | `06_final_species_request.py` | Species names resolved after manual verification |

---

## Troubleshooting

**High rate of `NO_EXACT_MATCH` from ENA.** This is expected for obscure taxa (parasitoids, rare beetles, microlepidoptera) that are not yet registered in ENA. The GBIF search in Step 4 and the GBIF processor in Step 5 handle these programmatically. Truly unresolvable names go to manual verification in Step 6.

**`TAXONOMIC_MISMATCH_HOMONYM` records appearing.** This means a name exists in ENA but under a different phylum than expected. Always check that your input `phylum` column is correctly populated. If the phylum is correct, the GBIF search in Step 4 will use it to resolve the homonym.

**API timeouts or `API_ERROR` records.** Both the ENA and GBIF scripts include retry logic (3 attempts with delays). Persistent errors usually indicate a connectivity issue or temporary API outage. Affected records can be re-run by subsetting the input to just the failed rows.

**`MATCH_PHYLUM_UNCHECKED` records.** These occur when `phylum` is empty for a record and a match was found in ENA. The match may be correct but cannot be verified. Populate `phylum` and re-run, or review manually.

---

## Repository Structure

```
taxid-minting-process/
├── 00_filter_taxonomy2.py          # Step 1: Split by rank, determine scientificName
├── requester_non-species.py        # Step 2: Request form for non-species records
├── 01_ena_taxid_check4.py          # Step 3: ENA validation (round 1)
├── 02_gbif_backbone_search2.py     # Step 4: GBIF backbone search (round 1)
├── gbif-processor/
│   ├── gbif_name_processor2.py     # Step 5: Process GBIF results, apply rules
│   └── gbif_rules.csv              # Business rules for GBIF processor
├── 04_post_ver_ena_check.py        # Step 7: ENA validation (round 2, post-manual)
├── 05_gbif_ver_check.py            # Step 8: GBIF search (round 2, post-manual)
├── 06_final_species_request.py     # Step 10: Final species request form formatting
└── README.md
```

---

## Citation / Acknowledgements

Developed at the Natural History Museum London as part of the Biodiversity Genomics Europe (BGE) initiative and UKBOL (UK Barcode of Life) project.
