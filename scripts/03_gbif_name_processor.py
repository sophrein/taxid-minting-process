#!/usr/bin/env python3
"""
GBIF Name Processor

Processes GBIF species match output files against a rules matrix to determine
whether to use original names or GBIF-matched names for ENA taxonomy submissions.

Workflow Context:
    1. Specimen names are matched against the GBIF backbone taxonomy
    2. This script applies rules based on match status, match type, and name similarity
    3. Output files are generated for downstream ENA submission or further validation

Input Requirements:
    - Input CSV must contain columns: ID, scientificName, type_status, gbif_status,
      gbif_matchType, gbif_species, gbif_genus, gbif_family, gbif_order, gbif_class,
      gbif_phylum, gbif_speciesKey, gbif_genusKey
    - Rules CSV must contain columns:
        Required: status, matchType, 'GBIF species value', 'GBIF genus value',
                  'Type specimen', 'name to use'
        Optional: 'text to add to \'description\' column of taxononmy_request.tsv',
                  'check ENA with GBIF name?', 'manual verification needed'

Decision Logic:
    The script compares the original scientific name against GBIF's matched name:
    - Species epithet comparison (same/different)
    - Genus comparison (same/different)
    - Type specimen status (yes/no)
    These factors, combined with GBIF's status and matchType, determine which
    name to use according to the rules matrix.

    Special Cases - "uncertain - check placement":
    The following cases are flagged as "uncertain - check placement" and require
    manual verification:
    
    - Genus mismatch detection: When GBIF returns an EXACT match (ACCEPTED or
      DOUBTFUL status) with the same species epithet but a different genus,
      this indicates GBIF places the species in a different genus than submitted.
    
    - No matching rule: When no matching rule is found in the rules matrix,
      including cases where:
      * The GBIF status is not covered (e.g., HETEROTYPIC_SYNONYM, HOMOTYPIC_SYNONYM)
      * The status + matchType combination is not covered (e.g., SYNONYM + HIGHERRANK)
      * Other parameter combinations not present in the rules matrix
    
    - Not a possible combination: Certain rule combinations flagged as logically
      impossible in the rules matrix.

    Manual Verification:
    The following outcomes automatically trigger manual_verification_needed = yes:
    - "uncertain - check placement"
    - "not a possible combination"
    - Any rule where 'manual verification needed' is 'yes' in the rules matrix

Output Files:
    1. {basename}_request_taxid.tsv
       Names where the original should be used, formatted for ENA taxonomy requests.
       Columns: proposed_name, name_type, host, project_id, description
       Notes:
         - Only includes rows where name_to_use == 'original' AND
           manual_verification_needed != 'yes'
         - Deduplicated by proposed_name (unique species names only)
         - Description contains GBIF URL; falls back to gbif_genusKey if
           gbif_speciesKey is empty/NOT_FOUND (these rows are also added to
           manually_verify.xlsx)
         - Type specimens are annotated with "| TYPE" appended to description
         - name_type is 'published_name' when gbif_speciesKey is used, or
           'novel_species' when falling back to gbif_genusKey

    2. {basename}_check_ENA.xlsx
       Names where the GBIF name should be used, requiring ENA validation.
       Contains all input columns for rows that require ENA taxid checking.

    3. {basename}_annotated.xlsx
       Complete input data with decision columns appended for review.
       Additional columns: name_to_use, description_text, check_ENA_with_GBIF,
       manual_verification_needed

    4. {basename}_manually_verify.xlsx
       Rows flagged for manual verification, containing all original input columns
       plus decision columns: name_to_use, description_text, check_ENA_with_GBIF,
       manual_verification_needed.
       Includes rows with the following name_to_use values:
         - "uncertain - check placement"
         - "not a possible combination"
         - Any row where manual_verification_needed == 'yes' from rules matrix

    5. {basename}.log
       Detailed log file containing:
         - Header with timestamp, input/output paths, and project ID
         - Per-sample processing details showing:
           * Original and GBIF names
           * Status and matchType
           * Genus and species epithet comparisons
           * Rule key used for lookup
           * Rule source (RULES_MATRIX, UNCERTAIN_GENUS_MISMATCH, or UNCERTAIN_NO_RULE)
           * Resulting name_to_use decision
         - Output file creation summary with row counts
         - Final statistics by name_to_use category

Usage:
    python gbif_name_processor.py -i INPUT_FILE -r RULES_FILE [-o OUTPUT_DIR] [-p PROJECT_ID]

Examples:
    # Basic usage with default project ID (BGE)
    python gbif_name_processor.py -i gbif_results.csv -r gbif_rules.csv

    # Specify output directory and custom project ID
    python gbif_name_processor.py -i gbif_results.csv -r gbif_rules.csv -o ./output -p UKBOL

Author: Ben Price @NHMUK (with contributions from Dan Parsons @NHMUK)
"""

import argparse
import csv
import os
import sys
from pathlib import Path
from datetime import datetime
import pandas as pd


class Logger:
    """Simple logger that writes to both console and file."""
    
    def __init__(self, log_file: Path):
        self.log_file = log_file
        self.lines = []
    
    def log(self, message: str = "", console: bool = True):
        """Log a message to buffer and optionally to console."""
        self.lines.append(message)
        if console:
            print(message)
    
    def save(self):
        """Write all logged lines to file."""
        with open(self.log_file, 'w', encoding='utf-8') as f:
            f.write('\n'.join(self.lines) + '\n')


def load_rules(rules_file: str) -> dict:
    """
    Load the rules CSV into a dictionary keyed by (status, matchType, species_match, genus_match, is_type).
    """
    rules = {}
    with open(rules_file, 'r', encoding='utf-8') as f:
        reader = csv.DictReader(f)
        for row in reader:
            key = (
                row['status'].strip().upper(),
                row['matchType'].strip().upper(),
                row['GBIF species value'].strip().lower(),
                row['GBIF genus value'].strip().lower(),
                row['Type specimen'].strip().upper() == 'YES'
            )
            rules[key] = {
                'name_to_use': row['name to use'].strip().lower(),
                'description_text': row.get('text to add to \'description\' column of taxononmy_request.tsv', '').strip(),
                'check_ena': row.get('check ENA with GBIF name?', '').strip().lower() == 'yes',
                'manual_verification': row.get('manual verification needed', '').strip().lower() == 'yes'
            }
    return rules


def extract_species_epithet(binomial: str) -> str:
    """Extract the species epithet (second word) from a binomial name."""
    if not binomial:
        return ''
    parts = binomial.strip().split()
    return parts[1] if len(parts) >= 2 else ''


def extract_genus(binomial: str) -> str:
    """Extract the genus (first word) from a binomial name."""
    if not binomial:
        return ''
    parts = binomial.strip().split()
    return parts[0] if parts else ''


def is_type_specimen(type_status: str) -> bool:
    """Check if 'type' is present in the type_status column (case-insensitive)."""
    if not type_status:
        return False
    return 'type' in type_status.lower()


def compare_names(original: str, gbif: str) -> str:
    """
    Compare two name components and return 'same' or 'different'.
    Handles case-insensitive comparison.
    """
    if not original or not gbif:
        return 'different'
    return 'same' if original.strip().lower() == gbif.strip().lower() else 'different'


def check_genus_mismatch_case(status: str, match_type: str, species_match: str, genus_match: str) -> bool:
    """
    Check if this is a genus mismatch case that should be flagged specially.
    
    These are cases where GBIF returns an EXACT match (ACCEPTED or DOUBTFUL status)
    with the same species epithet but a different genus - indicating GBIF places
    the species in a different genus than submitted.
    """
    if match_type != 'EXACT':
        return False
    if species_match != 'same':
        return False
    if genus_match != 'different':
        return False
    if status not in ('ACCEPTED', 'DOUBTFUL'):
        return False
    return True


def process_row(row: dict, rules: dict, logger: Logger) -> dict:
    """
    Process a single row from the input file and determine the appropriate action.
    
    Returns a dictionary with the rule outcome and additional metadata.
    """
    # Extract values from input row
    status = row.get('gbif_status', '').strip().upper()
    match_type = row.get('gbif_matchType', '').strip().upper()
    type_status = row.get('type_status', '')
    verbatim_name = row.get('scientificName', '').strip()
    gbif_species = row.get('gbif_species', '').strip()
    gbif_genus = row.get('gbif_genus', '').strip()
    gbif_family = row.get('gbif_family', '').strip()
    gbif_key = row.get('gbif_speciesKey', '').strip()
    gbif_genus_key = row.get('gbif_genusKey', '').strip()
    sample_id = row.get('ID', '').strip()
    
    # Determine if type specimen
    is_type = is_type_specimen(type_status)
    
    # Extract species epithets for comparison
    original_species_epithet = extract_species_epithet(verbatim_name)
    gbif_species_epithet = extract_species_epithet(gbif_species)
    
    # Extract genera for comparison
    original_genus = extract_genus(verbatim_name)
    
    # Compare species and genus
    species_match = compare_names(original_species_epithet, gbif_species_epithet)
    genus_match = compare_names(original_genus, gbif_genus)
    
    # Build the rule key
    rule_key = (status, match_type, species_match, genus_match, is_type)
    
    # Check for genus mismatch special case first
    is_genus_mismatch = check_genus_mismatch_case(status, match_type, species_match, genus_match)
    
    # Look up the rule
    if is_genus_mismatch:
        # Special case: genus mismatch with exact species match
        rule = {
            'name_to_use': 'uncertain - check placement',
            'description_text': '',
            'check_ena': False,
            'manual_verification': True
        }
        rule_source = "UNCERTAIN_GENUS_MISMATCH"
    elif rule_key in rules:
        rule = rules[rule_key]
        rule_source = "RULES_MATRIX"
    else:
        # No matching rule found
        rule = {
            'name_to_use': 'uncertain - check placement',
            'description_text': '',
            'check_ena': False,
            'manual_verification': True
        }
        rule_source = "UNCERTAIN_NO_RULE"
    
    # Force manual verification for 'not a possible combination' and 'uncertain - check placement'
    if rule['name_to_use'] in ('not a possible combination', 'uncertain - check placement'):
        rule = rule.copy()  # Don't modify the original rule dict
        rule['manual_verification'] = True
    
    # Format description text (replace [original] placeholder)
    description_text = rule['description_text']
    if '[original]' in description_text:
        description_text = description_text.replace('[original]', verbatim_name)
    
    # Log the decision for this sample
    logger.log(f"\n{'='*80}")
    logger.log(f"Sample: {sample_id}")
    logger.log(f"  Scientific name (original): {verbatim_name}")
    logger.log(f"  GBIF species: {gbif_species if gbif_species else 'NOT_FOUND'}")
    logger.log(f"  GBIF genus: {gbif_genus if gbif_genus else 'NOT_FOUND'}")
    logger.log(f"  Status: {status}")
    logger.log(f"  MatchType: {match_type}")
    logger.log(f"  Original genus: {original_genus} | GBIF genus: {gbif_genus} → Genus match: {genus_match.upper()}")
    logger.log(f"  Original epithet: {original_species_epithet} | GBIF epithet: {gbif_species_epithet if gbif_species_epithet else 'N/A'} → Species match: {species_match.upper()}")
    logger.log(f"  Type specimen: {'YES' if is_type else 'NO'}")
    logger.log(f"  Rule key: ({status}, {match_type}, {species_match}, {genus_match}, {'YES' if is_type else 'NO'})")
    logger.log(f"  Rule source: {rule_source}")
    logger.log(f"  → name_to_use: {rule['name_to_use']}")
    if rule['manual_verification']:
        logger.log(f"  → manual_verification_needed: YES")
    if rule['check_ena']:
        logger.log(f"  → check_ENA_with_GBIF: YES")
    
    return {
        'name_to_use': rule['name_to_use'],
        'description_text': description_text,
        'check_ena': 'yes' if rule['check_ena'] else '',
        'manual_verification': 'yes' if rule['manual_verification'] else '',
        'gbif_key': gbif_key,
        'gbif_genus_key': gbif_genus_key,
        'verbatim_name': verbatim_name,
        'gbif_species': gbif_species,
        'gbif_genus': gbif_genus,
        'gbif_family': gbif_family,
        'gbif_order': row.get('gbif_order', '').strip(),
        'gbif_class': row.get('gbif_class', '').strip(),
        'gbif_phylum': row.get('gbif_phylum', '').strip(),
        'species_match': species_match,
        'genus_match': genus_match,
        'is_type': is_type,
        'status': status,
        'match_type': match_type,
        'sample_id': sample_id
    }


def process_file(input_file: str, rules_file: str, output_dir: str = None, project_id: str = 'BGE'):
    """
    Process the input file and generate output files.
    """
    # Set up paths
    input_path = Path(input_file)
    basename = input_path.stem

    if output_dir:
        out_path = Path(output_dir)
    else:
        out_path = input_path.parent

    # Set up logger
    log_file = out_path / f"{basename}.log"
    logger = Logger(log_file)
    
    # Log header
    logger.log(f"GBIF Name Processor - Processing Log")
    logger.log(f"{'='*80}")
    logger.log(f"Timestamp: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    logger.log(f"Input file: {input_file}")
    logger.log(f"Rules file: {rules_file}")
    logger.log(f"Output directory: {out_path}")
    logger.log(f"Project ID: {project_id}")

    # Load rules
    rules = load_rules(rules_file)
    logger.log(f"Loaded {len(rules)} rules from rules matrix")
    
    logger.log(f"\n{'='*80}")
    logger.log("SAMPLE-BY-SAMPLE PROCESSING")
    logger.log(f"{'='*80}")

    # Prepare output files
    request_taxid_file = out_path / f"{basename}_request_taxid.tsv"
    check_ena_file = out_path / f"{basename}_check_ENA.xlsx"
    annotated_file = out_path / f"{basename}_annotated.xlsx"
    manually_verify_file = out_path / f"{basename}_manually_verify.xlsx"

    # Containers for processing
    original_names = {}
    check_ena_rows = []
    annotated_rows = []
    manually_verify_rows = []

    # Read input and process
    with open(input_file, 'r', encoding='utf-8') as f:
        reader = csv.DictReader(f)
        fieldnames = reader.fieldnames.copy()

        for row in reader:
            result = process_row(row, rules, logger)

            # Build annotated row
            annotated_row = row.copy()
            annotated_row['name_to_use'] = result['name_to_use']
            annotated_row['description_text'] = result['description_text']
            annotated_row['check_ENA_with_GBIF'] = result['check_ena']
            annotated_row['manual_verification_needed'] = result['manual_verification']
            annotated_rows.append(annotated_row)

            # Route to appropriate output
            if result['name_to_use'] == 'original':
                if result['manual_verification'] != 'yes':
                    proposed_name = result['verbatim_name']

                    gbif_key = result['gbif_key']
                    used_genus_fallback = False
                    needs_manual_verify_for_fallback = False

                    if not gbif_key or gbif_key.upper() == 'NOT_FOUND':
                        gbif_key = result['gbif_genus_key']
                        used_genus_fallback = True
                        needs_manual_verify_for_fallback = True
                        if not gbif_key or gbif_key.upper() == 'NOT_FOUND':
                            gbif_key = ''

                    if proposed_name in original_names:
                        if result['is_type']:
                            original_names[proposed_name]['is_type'] = True
                        if needs_manual_verify_for_fallback:
                            original_names[proposed_name]['needs_manual_verify'] = True
                        if used_genus_fallback:
                            original_names[proposed_name]['used_genus_fallback'] = True
                    else:
                        original_names[proposed_name] = {
                            'gbif_key': gbif_key,
                            'is_type': result['is_type'],
                            'needs_manual_verify': needs_manual_verify_for_fallback,
                            'used_genus_fallback': used_genus_fallback
                        }

                    if needs_manual_verify_for_fallback:
                        manually_verify_rows.append(annotated_row.copy())

            elif result['name_to_use'] == 'gbif':
                check_ena_rows.append(row.copy())

            # Add to manual verification if flagged (including 'not a possible combination' and 'uncertain - check placement')
            if result['manual_verification'] == 'yes':
                manually_verify_rows.append(annotated_row.copy())

    # Build deduplicated original_rows list
    original_rows = []
    for proposed_name, info in original_names.items():
        gbif_link = f"https://www.gbif.org/species/{info['gbif_key']}" if info['gbif_key'] else ''
        if info['is_type']:
            description = f"{gbif_link} | TYPE" if gbif_link else "TYPE"
        else:
            description = gbif_link

        name_type = 'novel_species' if info['used_genus_fallback'] else 'published_name'

        original_rows.append({
            'proposed_name': proposed_name,
            'name_type': name_type,
            'host': '',
            'project_id': project_id,
            'description': description
        })

    # Extended fieldnames including decision columns
    new_fieldnames = fieldnames + [
        'name_to_use',
        'description_text',
        'check_ENA_with_GBIF',
        'manual_verification_needed'
    ]

    # Log summary section
    logger.log(f"\n{'='*80}")
    logger.log("OUTPUT FILES")
    logger.log(f"{'='*80}")

    # Write request_taxid.tsv
    with open(request_taxid_file, 'w', encoding='utf-8', newline='') as f:
        writer = csv.DictWriter(
            f,
            fieldnames=['proposed_name', 'name_type', 'host', 'project_id', 'description'],
            delimiter='\t'
        )
        writer.writeheader()
        writer.writerows(original_rows)

    logger.log(f"Created: {request_taxid_file} ({len(original_rows)} rows)")

    # Write check_ENA.xlsx
    pd.DataFrame(check_ena_rows, columns=fieldnames).to_excel(
        check_ena_file,
        index=False
    )
    logger.log(f"Created: {check_ena_file} ({len(check_ena_rows)} rows)")

    # Write annotated.xlsx
    pd.DataFrame(annotated_rows, columns=new_fieldnames).to_excel(
        annotated_file,
        index=False
    )
    logger.log(f"Created: {annotated_file} ({len(annotated_rows)} rows)")

    # Write manually_verify.xlsx (includes decision columns)
    pd.DataFrame(manually_verify_rows, columns=new_fieldnames).to_excel(
        manually_verify_file,
        index=False
    )
    logger.log(f"Created: {manually_verify_file} ({len(manually_verify_rows)} rows)")

    # Print summary statistics
    not_possible = sum(1 for r in annotated_rows if r['name_to_use'] == 'not a possible combination')
    uncertain = sum(1 for r in annotated_rows if r['name_to_use'] == 'uncertain - check placement')
    total_original_records = sum(1 for r in annotated_rows if r['name_to_use'] == 'original')
    total_gbif_records = sum(1 for r in annotated_rows if r['name_to_use'] == 'gbif')

    logger.log(f"\n{'='*80}")
    logger.log("SUMMARY")
    logger.log(f"{'='*80}")
    logger.log(f"Total samples processed: {len(annotated_rows)}")
    logger.log(f"")
    logger.log(f"Results by name_to_use:")
    logger.log(f"  original: {total_original_records} records ({len(original_rows)} unique names)")
    logger.log(f"  gbif (check ENA): {total_gbif_records} records")
    logger.log(f"  uncertain - check placement: {uncertain} records")
    logger.log(f"  not a possible combination: {not_possible} records")
    logger.log(f"")
    logger.log(f"Manual verification required: {len(manually_verify_rows)} records")
    
    # Save log file
    logger.save()
    print(f"\nLog saved: {log_file}")


def main():
    parser = argparse.ArgumentParser(
        description='Process GBIF species match output against rules matrix.',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Outputs:
  {input_basename}_request_taxid.tsv  - Names to use as original (with GBIF hyperlinks)
  {input_basename}_check_ENA.xlsx      - Names to check in ENA with GBIF name (all input columns)
  {input_basename}_annotated.xlsx      - Full input with decision columns added
  {input_basename}_manually_verify.xlsx - Rows needing manual verification (with decision columns)
  {input_basename}.log                  - Detailed processing log

Example:
  python gbif_name_processor.py -i XE-4013_output.csv -r gbif_rules.csv -p BGE
        """
    )
    parser.add_argument('-i', '--input', required=True, help='Input CSV file (GBIF match output)')
    parser.add_argument('-r', '--rules', required=True, help='Rules CSV file')
    parser.add_argument('-o', '--output-dir', help='Output directory (default: same as input)')
    parser.add_argument('-p', '--project-id', default='BGE', help='Project ID for taxonomy requests (default: BGE)')
    
    args = parser.parse_args()
    
    # Validate input files exist
    if not os.path.isfile(args.input):
        print(f"Error: Input file not found: {args.input}", file=sys.stderr)
        sys.exit(1)
    
    if not os.path.isfile(args.rules):
        print(f"Error: Rules file not found: {args.rules}", file=sys.stderr)
        sys.exit(1)
    
    # Create output directory if specified and doesn't exist
    if args.output_dir:
        os.makedirs(args.output_dir, exist_ok=True)
    
    process_file(args.input, args.rules, args.output_dir, args.project_id)


if __name__ == '__main__':
    main()