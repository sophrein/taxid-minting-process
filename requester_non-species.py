#!/usr/bin/env python3
"""
Non-Species TaxID Request Generator

Generates ENA taxonomy request files for specimens identified only to genus
level (or above). For each record, constructs a proposed name in the format
"{scientificName} sp. {ID}", queries the GBIF backbone to obtain a higher
taxon usageKey for the description URL, and writes the results as a TSV
formatted for ENA taxonomy submission.

GBIF queries include automatic disambiguation using higher taxonomy (phylum,
order, family) if the initial search returns an invalid or overly broad result.

Usage:
    python requester_non-species.py -i input.csv -p PROJECT_ID [-o output.tsv]

Required input columns:
    - ID: Unique specimen identifier (used to construct "sp. {ID}" names)
    - scientificName: The genus or higher-level name

Optional input columns (used for GBIF disambiguation):
    - phylum, order, family

Output files:
    {basename}_taxonomy_requests.tsv                    - Names formatted for ENA submission
    {basename}_taxonomy_requests_empty_descriptions.tsv  - Rows where GBIF lookup failed
    {basename}_taxonomy_requests.log                     - Processing log

Author: Dan Parsons @NHMUK
"""

import argparse
import csv
import sys
from pathlib import Path
from pygbif import species
import json
from datetime import datetime


class DualLogger:
    """Logger that writes to both terminal and file."""
    
    def __init__(self, log_file_path):
        self.terminal = sys.stdout
        self.log_file = open(log_file_path, 'w', encoding='utf-8')
    
    def write(self, message):
        self.terminal.write(message)
        self.log_file.write(message)
    
    def flush(self):
        self.terminal.flush()
        self.log_file.flush()
    
    def close(self):
        self.log_file.close()


def detect_delimiter(file_path, num_lines=5):
    """Detect the delimiter of a CSV/TSV file."""
    with open(file_path, 'r', encoding='utf-8') as f:
        sample = ''.join([f.readline() for _ in range(num_lines)])
    
    sniffer = csv.Sniffer()
    delimiter = sniffer.sniff(sample).delimiter
    return delimiter


def get_gbif_info(scientific_name, phylum=None, order=None, family=None):
    """
    Query GBIF backbone taxonomy for a scientific name.
    Returns tuple of (usageKey, result_dict, warning_message, was_disambiguated).
    
    If initial query returns usageKey=1 (Animalia) or matchType=NONE, 
    attempts disambiguation using higher taxonomy (phylum, order, family).
    
    Note: usageKey of 1 is treated as invalid (refers to root "Animalia").
    Matches at KINGDOM rank are rejected as too broad.
    """
    was_disambiguated = False
    
    # Ranks that are too broad to be useful
    invalid_ranks = {'KINGDOM'}
    
    def is_valid_result(result):
        """Check if a GBIF result is valid and specific enough."""
        if not result:
            return False
        if result.get('matchType') == 'NONE':
            return False
        usage_key = result.get('usageKey')
        if not usage_key or usage_key == 1:
            return False
        if result.get('rank') in invalid_ranks:
            return False
        return True
    
    try:
        # First attempt: simple name query
        result = species.name_backbone(name=scientific_name)
        
        # Check if we need disambiguation
        needs_disambiguation = not is_valid_result(result)
        
        # Attempt disambiguation with higher taxonomy if needed
        if needs_disambiguation and any([phylum, order, family]):
            disambig_result = species.name_backbone(
                name=scientific_name,
                phylum=phylum,
                order=order,
                family=family
            )
            
            # Check if disambiguation improved the result
            if is_valid_result(disambig_result):
                result = disambig_result
                was_disambiguated = True
        
        # Evaluate final result
        if result.get('matchType') == 'NONE':
            return None, result, f"No GBIF match found for '{scientific_name}'", was_disambiguated
        
        usage_key = result.get('usageKey', None)
        
        if not usage_key:
            return None, result, f"No usageKey found for '{scientific_name}'", was_disambiguated
        
        if usage_key == 1:
            return None, result, f"Invalid usageKey (1) returned for '{scientific_name}'", was_disambiguated
        
        if result.get('rank') in invalid_ranks:
            return None, result, f"Match too broad (rank={result.get('rank')}) for '{scientific_name}'", was_disambiguated
        
        return usage_key, result, None, was_disambiguated
        
    except Exception as e:
        return None, None, f"Error querying GBIF for '{scientific_name}': {str(e)}", False


def print_gbif_response(sample_num, total, scientific_name, result, warning=None, was_disambiguated=False):
    """Print formatted GBIF API response for a sample."""
    print(f"\n{'='*60}")
    status_flags = []
    if was_disambiguated:
        status_flags.append("DISAMBIGUATED")
    if warning:
        status_flags.append("WARNING")
    
    status_str = f" [{', '.join(status_flags)}]" if status_flags else ""
    print(f"  [{sample_num}/{total}] Querying: {scientific_name}{status_str}")
    print(f"{'='*60}")
    
    if result:
        print(json.dumps(result, indent=2))
    else:
        print("  No result returned from GBIF API")
    
    if warning:
        print(f"\n  ⚠ WARNING: {warning}")
    
    if was_disambiguated:
        print(f"\n  ✓ Successfully disambiguated using higher taxonomy")


def main():
    parser = argparse.ArgumentParser(
        description='Process NCBI match results and generate taxonomy request files'
    )
    parser.add_argument(
        '--input', '--in', '-i',
        required=True,
        dest='input',
        help='Input CSV/TSV file with NCBI match results'
    )
    parser.add_argument(
        '--output', '--out', '-o',
        required=False,
        dest='output',
        help='Output TSV file path (default: [input_name]_taxonomy_requests.tsv)'
    )
    parser.add_argument(
        '--project_id', '-p',
        required=True,
        dest='project_id',
        help='Project ID to use in the output file'
    )
    
    args = parser.parse_args()
    
    # Determine output file paths
    if args.output:
        taxonomy_requests_file = Path(args.output)
        output_stem = taxonomy_requests_file.stem
        output_dir = taxonomy_requests_file.parent
        empty_desc_file = output_dir / f"{output_stem}_empty_descriptions.tsv"
        log_file = output_dir / f"{output_stem}.log"
    else:
        input_path = Path(args.input)
        input_stem = input_path.stem
        input_dir = input_path.parent
        taxonomy_requests_file = input_dir / f"{input_stem}_taxonomy_requests.tsv"
        empty_desc_file = input_dir / f"{input_stem}_taxonomy_requests_empty_descriptions.tsv"
        log_file = input_dir / f"{input_stem}_taxonomy_requests.log"
    
    # Create output directories if they don't exist
    taxonomy_requests_file.parent.mkdir(parents=True, exist_ok=True)
    
    # Set up dual logging
    logger = DualLogger(log_file)
    sys.stdout = logger
    
    try:
        # Print header
        print(f"{'='*70}")
        print(f"non_species_request.py")
        print(f"Started: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        print(f"{'='*70}")
        
        # Auto-detect delimiter
        delimiter = detect_delimiter(args.input)
        print(f"\nInput file: {args.input}")
        print(f"Detected delimiter: {'tab' if delimiter == chr(9) else repr(delimiter)}")
        
        # Read input CSV/TSV
        with open(args.input, 'r', encoding='utf-8') as f:
            reader = csv.DictReader(f, delimiter=delimiter)
            fieldnames = reader.fieldnames
            rows = list(reader)
        
        print(f"Input columns: {fieldnames}")
        print(f"\nQuerying GBIF for {len(rows)} taxa...")
        
        # Check for higher taxonomy columns
        taxonomy_cols = {
            'phylum': None,
            'order': None,
            'family': None
        }
        
        for col in fieldnames:
            col_lower = col.lower()
            if col_lower in taxonomy_cols:
                taxonomy_cols[col_lower] = col
        
        available_taxonomy = [k for k, v in taxonomy_cols.items() if v]
        if available_taxonomy:
            print(f"Higher taxonomy columns available for disambiguation: {available_taxonomy}")
        else:
            print("No higher taxonomy columns found - disambiguation not available")
        
        warnings_list = []
        empty_description_rows = []
        disambiguated_samples = []
        
        # Create taxonomy request TSV
        with open(taxonomy_requests_file, 'w', encoding='utf-8', newline='') as f:
            out_fieldnames = ['proposed_name', 'name_type', 'host', 'project_id', 'description']
            writer = csv.DictWriter(f, fieldnames=out_fieldnames, delimiter='\t')
            writer.writeheader()
            
            for i, row in enumerate(rows, 1):
                sample_id = row['ID']
                scientific_name = row['scientificName']
                proposed_name = f"{scientific_name} sp. {sample_id}"
                
                # Extract higher taxonomy for disambiguation
                phylum = row.get(taxonomy_cols['phylum']) if taxonomy_cols['phylum'] else None
                order = row.get(taxonomy_cols['order']) if taxonomy_cols['order'] else None
                family = row.get(taxonomy_cols['family']) if taxonomy_cols['family'] else None
                
                # Query GBIF with disambiguation support
                usage_key, gbif_result, warning, was_disambiguated = get_gbif_info(
                    scientific_name,
                    phylum=phylum,
                    order=order,
                    family=family
                )
                
                # Print detailed GBIF response for this sample
                print_gbif_response(i, len(rows), scientific_name, gbif_result, warning, was_disambiguated)
                
                if warning:
                    warnings_list.append(f"  ⚠ {sample_id}: {warning}")
                
                if was_disambiguated:
                    disambig_info = {
                        'sample_id': sample_id,
                        'scientific_name': scientific_name,
                        'phylum': phylum,
                        'order': order,
                        'family': family,
                        'resolved_usageKey': usage_key,
                        'resolved_name': gbif_result.get('scientificName') if gbif_result else None
                    }
                    disambiguated_samples.append(disambig_info)
                
                # Build description (GBIF URL with higher taxon link marker)
                description = f"https://www.gbif.org/species/{usage_key} | HIGHER TAXON LINK" if usage_key else ''
                
                output_row = {
                    'proposed_name': proposed_name,
                    'name_type': 'Unidentified species',
                    'host': '',
                    'project_id': args.project_id,
                    'description': description
                }
                
                writer.writerow(output_row)
                
                # Track rows with empty descriptions
                if not description:
                    empty_description_rows.append(output_row)
        
        # Summary section
        print(f"\n{'='*70}")
        print(f"PROCESSING COMPLETE")
        print(f"{'='*70}")
        print(f"\n✓ Wrote {len(rows)} taxonomy requests to: {taxonomy_requests_file}")
        
        # Write empty descriptions file if there are any
        if empty_description_rows:
            with open(empty_desc_file, 'w', encoding='utf-8', newline='') as f:
                out_fieldnames = ['proposed_name', 'name_type', 'host', 'project_id', 'description']
                writer = csv.DictWriter(f, fieldnames=out_fieldnames, delimiter='\t')
                writer.writeheader()
                writer.writerows(empty_description_rows)
            
            print(f"✓ Wrote {len(empty_description_rows)} samples with empty descriptions to: {empty_desc_file}")
        
        # Print disambiguated samples summary
        if disambiguated_samples:
            print(f"\n{'='*70}")
            print(f"DISAMBIGUATED SAMPLES ({len(disambiguated_samples)} resolved using higher taxonomy):")
            print(f"{'='*70}")
            for sample in disambiguated_samples:
                taxonomy_used = []
                if sample['phylum']:
                    taxonomy_used.append(f"phylum={sample['phylum']}")
                if sample['order']:
                    taxonomy_used.append(f"order={sample['order']}")
                if sample['family']:
                    taxonomy_used.append(f"family={sample['family']}")
                
                print(f"  ✓ {sample['sample_id']}: '{sample['scientific_name']}'")
                print(f"      Disambiguation: {', '.join(taxonomy_used)}")
                print(f"      Resolved to: {sample['resolved_name']} (usageKey: {sample['resolved_usageKey']})")
            print(f"{'='*70}")
        else:
            print("\n✓ No samples required disambiguation")
        
        # Print warnings summary
        if warnings_list:
            print(f"\n{'='*70}")
            print(f"WARNINGS ({len(warnings_list)} issues found):")
            print(f"{'='*70}")
            for warning in warnings_list:
                print(warning)
            print(f"{'='*70}")
        else:
            print("\n✓ No warnings - all samples successfully matched with complete information")
        
        # Final stats
        print(f"\n{'='*70}")
        print(f"SUMMARY STATISTICS")
        print(f"{'='*70}")
        print(f"  Total samples processed:     {len(rows)}")
        print(f"  Successful matches:          {len(rows) - len(empty_description_rows)}")
        print(f"  Failed matches:              {len(empty_description_rows)}")
        print(f"  Disambiguated samples:       {len(disambiguated_samples)}")
        print(f"  Warnings:                    {len(warnings_list)}")
        print(f"{'='*70}")
        print(f"Finished: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        print(f"Log file: {log_file}")
        print(f"{'='*70}")
        
    finally:
        # Restore stdout and close log file
        sys.stdout = logger.terminal
        logger.close()
    
    print(f"\n✓ Log written to: {log_file}")


if __name__ == '__main__':
    main()