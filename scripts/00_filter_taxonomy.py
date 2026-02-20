#!/usr/bin/env python3
"""
Split taxonomic CSV data based on species column and populate scientificName column.

Splits records into 'species' and 'non-species' outputs based on whether the
species column is populated, and determines scientificName using hierarchical
taxonomic rank traversal.

LOGICAL PROCESS
===============

1. INPUT HANDLING
   - Read CSV/TSV file (auto-detects delimiter)
   - Optionally rename a specified ID column to standardised "ID"

2. ROW SPLITTING
   - Split rows based on whether the 'species' column is empty or populated
   - species column populated → 'species' output CSV
   - species column empty → 'non-species' output CSV

3. SCIENTIFIC NAME DETERMINATION
   For each row, determine scientificName by traversing taxonomic hierarchy
   from most specific to least specific. Values of 'unspecified' or 
   'not collected' (case-insensitive) are treated as empty.

   3.1. If species not empty
        → Use species as scientificName (identified_rank = "species")
        
   3.2. Else if genus not empty
        → Use genus as scientificName (identified_rank = "genus")
        
   3.3. Else if family not empty
        → Use family as scientificName (identified_rank = "family")
        
   3.4. Else if order not empty
        → Use order as scientificName (identified_rank = "order")
        
   3.5. Else if class not empty
        → Use class as scientificName (identified_rank = "class")
        
   3.6. Else if phylum not empty
        → Use phylum as scientificName (identified_rank = "phylum")
        
   3.7. Else (all above empty)
        → Log warning with sample ID, leave scientificName empty

4. NON-SPECIES LIST GENERATION
   - For rows in non-species output where identified_rank is populated
   - Generate strings in format: "[scientificName] sp. [ID]"
   - Output to non_species_list.txt (same directory as output CSV)
   - Rows with empty identified_rank are logged as warnings and excluded

5. OUTPUT
   - Write two CSV files:
     a) Species output: rows where species column is populated
     b) Non-species output: rows where species column is empty
   - Rename taxid → NCBI_taxid, matched_rank → NCBI_matched_rank, 
     lineage → NCBI_lineage
   - Add new columns: scientificName, identified_rank, note
   - Output as comma-delimited CSV with fixed column order
"""

import argparse
import csv
import os
import sys


def parse_args():
    parser = argparse.ArgumentParser(
        description="Split taxonomic CSV by species column and populate scientificName using hierarchical taxonomy."
    )
    parser.add_argument(
        "--input", "--in", "-i",
        dest="input_file",
        required=True,
        help="Input CSV file path"
    )
    parser.add_argument(
        "--output", "--out", "-o",
        dest="output_file",
        required=True,
        help="Output CSV file path (base name - will generate _species and _non_species variants)"
    )
    parser.add_argument(
        "--id-col",
        dest="id_col",
        default=None,
        help="Name of the ID column to rename to 'ID' (e.g., 'Process ID', 'Sample ID')"
    )
    return parser.parse_args()


def is_empty(value):
    """Check if a value is empty, None, or a placeholder like 'unspecified'/'not collected'."""
    if value is None:
        return True
    stripped = str(value).strip().lower()
    if stripped == "":
        return True
    if stripped in ("unspecified", "not collected"):
        return True
    return False


def process_row(row):
    """
    Process a single row and return scientificName, note, and identified_rank.
    
    Traverses taxonomic hierarchy from species -> phylum to find the lowest
    (most specific) available rank.
    
    Returns tuple: (scientificName, note, identified_rank)
    """
    species = row.get("species", "").strip()
    genus = row.get("genus", "").strip()
    family = row.get("family", "").strip()
    order = row.get("order", "").strip()
    class_ = row.get("class", "").strip()
    phylum = row.get("phylum", "").strip()
    
    scientific_name = ""
    note = ""
    identified_rank = ""
    
    # Traverse hierarchy from most specific to least specific
    if not is_empty(species):
        scientific_name = species
        identified_rank = "species"
    elif not is_empty(genus):
        scientific_name = genus
        identified_rank = "genus"
    elif not is_empty(family):
        scientific_name = family
        identified_rank = "family"
    elif not is_empty(order):
        scientific_name = order
        identified_rank = "order"
    elif not is_empty(class_):
        scientific_name = class_
        identified_rank = "class"
    elif not is_empty(phylum):
        scientific_name = phylum
        identified_rank = "phylum"
    else:
        # No taxonomic rank available at phylum level or below
        note = "WARNING: no taxonomy available at phylum level or below"
    
    return scientific_name, note, identified_rank


# Output columns in order (using output names)
OUTPUT_COLUMNS = [
    "ID",
    "scientificName",
    "identified_rank",
    "phylum",
    "class",
    "order",
    "family",
    "genus",
    "species",
    "NCBI_taxid",
    "NCBI_matched_rank",
    "NCBI_lineage",
    "lineage_mismatch",
    "type_status",
    "note",
]


def main():
    args = parse_args()
    
    try:
        with open(args.input_file, "r", newline="", encoding="utf-8") as infile:
            # Detect delimiter (tab or comma)
            sample = infile.read(4096)
            infile.seek(0)
            if "\t" in sample.split("\n")[0]:
                delimiter = "\t"
            else:
                delimiter = ","
            
            reader = csv.DictReader(infile, delimiter=delimiter)
            fieldnames = list(reader.fieldnames)
            
            # Handle ID column renaming
            if args.id_col:
                if args.id_col not in fieldnames:
                    print(f"Error: ID column '{args.id_col}' not found in input file.", file=sys.stderr)
                    print(f"Available columns: {', '.join(fieldnames)}", file=sys.stderr)
                    sys.exit(1)
            
            species_rows = []
            non_species_rows = []
            non_species_list = []
            no_taxonomy_warnings = []
            total_rows = 0
            
            for row in reader:
                total_rows += 1
                
                # Process the row to get scientificName
                scientific_name, note, identified_rank = process_row(row)
                
                # Build output row with only the specified columns
                output_row = {}
                
                # Handle ID column
                if args.id_col:
                    output_row["ID"] = row.get(args.id_col, "")
                else:
                    # Try common ID column names
                    output_row["ID"] = ""
                    for id_name in ["ID", "Sample ID", "Process ID"]:
                        if id_name in row:
                            output_row["ID"] = row.get(id_name, "")
                            break
                
                # Add taxonomic columns
                output_row["phylum"] = row.get("phylum", "")
                output_row["class"] = row.get("class", "")
                output_row["order"] = row.get("order", "")
                output_row["family"] = row.get("family", "")
                output_row["genus"] = row.get("genus", "")
                output_row["species"] = row.get("species", "")
                
                # Add renamed NCBI columns
                output_row["NCBI_taxid"] = row.get("taxid", "")
                output_row["NCBI_matched_rank"] = row.get("matched_rank", "")
                output_row["NCBI_lineage"] = row.get("lineage", "")
                
                # Add other columns
                output_row["lineage_mismatch"] = row.get("lineage_mismatch", "")
                output_row["type_status"] = row.get("type_status", "")
                
                # Add new columns
                output_row["scientificName"] = scientific_name
                output_row["identified_rank"] = identified_rank
                output_row["note"] = note
                
                # Split based on whether species column is populated
                species_value = row.get("species", "").strip()
                
                if not is_empty(species_value):
                    # Species column populated → species output
                    species_rows.append(output_row)
                else:
                    # Species column empty → non-species output
                    non_species_rows.append(output_row)
                    
                    # Generate non-species list entry
                    sample_id = output_row["ID"]
                    if is_empty(identified_rank):
                        # Log warning for empty identified_rank
                        no_taxonomy_warnings.append(sample_id)
                    else:
                        # Build list string: "[scientificName] sp. [ID]"
                        list_string = f"{scientific_name} sp. {sample_id}"
                        non_species_list.append(list_string)
        
        # Generate output filenames
        output_dir = os.path.dirname(args.output_file)
        if not output_dir:
            output_dir = "."
        # Generate output filenames
        output_dir = os.path.dirname(args.output_file)
        if not output_dir:
            output_dir = "."
        else:
            os.makedirs(output_dir, exist_ok=True)
            
        base_name = os.path.basename(args.output_file)
        name_part, ext = os.path.splitext(base_name)
        species_file = os.path.join(output_dir, f"{name_part}_species{ext}")
        non_species_file = os.path.join(output_dir, f"{name_part}_non_species{ext}")
        
        # Write species output CSV
        with open(species_file, "w", newline="", encoding="utf-8") as outfile:
            writer = csv.DictWriter(outfile, fieldnames=OUTPUT_COLUMNS, delimiter=",")
            writer.writeheader()
            writer.writerows(species_rows)
        
        # Write non-species output CSV
        with open(non_species_file, "w", newline="", encoding="utf-8") as outfile:
            writer = csv.DictWriter(outfile, fieldnames=OUTPUT_COLUMNS, delimiter=",")
            writer.writeheader()
            writer.writerows(non_species_rows)
        
        # Write non_species_list.txt to same directory as output CSV
        list_file = os.path.join(output_dir, "non_species_list.txt")
        
        with open(list_file, "w", encoding="utf-8") as lf:
            for entry in non_species_list:
                lf.write(f"{entry}\n")
        
        # Print summary
        print(f"Processed {total_rows} rows")
        print(f"Species output written to: {species_file} ({len(species_rows)} rows)")
        print(f"Non-species output written to: {non_species_file} ({len(non_species_rows)} rows)")
        print(f"Non-species list written to: {list_file} ({len(non_species_list)} entries)")
        
        # Print warnings for samples with no taxonomy at phylum level or below
        if no_taxonomy_warnings:
            print(f"\nWARNING: {len(no_taxonomy_warnings)} samples have no taxonomy at phylum level or below:")
            for sample_id in no_taxonomy_warnings:
                print(f"  - {sample_id}")
        
    except FileNotFoundError:
        print(f"Error: Input file '{args.input_file}' not found.", file=sys.stderr)
        sys.exit(1)
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()