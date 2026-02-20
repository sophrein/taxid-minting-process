#!/usr/bin/env python3
"""
ENA Taxonomy ID Checker (04_ena_taxid_check.py)
===============================================

This script validates taxonomic names against the European Nucleotide Archive (ENA) 
taxonomy database and retrieves corresponding taxon IDs for sequence submission.

This script is designed to process the output of the manual review step, where all
rows have been reviewed and a confirmed_taxonomy column has been populated with the
correct name to search in ENA.

WORKFLOW CONTEXT
----------------
1. Run gbif_name_processor.py to generate _annotated.xlsx
2. Manually review rows and populate confirmed_taxonomy column
3. Run this script to validate names against ENA and retrieve taxon IDs

OVERVIEW
--------
The script takes a spreadsheet of specimen records, processes ALL rows, queries the
ENA taxonomy API using the column specified by --man_verify_col (default: man_ver_name,
or confirmed_taxonomy as appropriate), and outputs validated taxonomy data with ENA
taxon IDs. Rows with an empty search column are flagged with a warning. Results are
split into separate files based on match quality to facilitate downstream processing.

INPUT REQUIREMENTS
------------------
Supported file formats: CSV, TSV, XLSX

Required columns:
    - <man_verify_col>: Name to search in ENA (default: man_ver_name). All rows are
      processed; a warning is issued for any row where this column is empty.
    
Recommended columns for homonym disambiguation:
    - phylum: Used to verify matches against ENA lineage (critical for resolving homonyms)
    
Optional columns (passed through to output):
    - ID: Sample/specimen identifier
    - scientificName: Original taxonomic name
    - And any other columns from the input file

SEARCH LOGIC
------------
The script:
1. Processes ALL rows in the input file
2. Warns (but continues) if any row has an empty confirmed_taxonomy (or specified column)
3. Searches ENA using the specified column
4. Outputs the full file with ENA columns appended

LOGICAL PROCESS
---------------
For each row:

1. GET SEARCH TERM
   - Uses the value from the specified column (--man_verify_col)
   - Removes 'cf.' qualifiers if present (e.g., "Genus cf. species" -> "Genus species")
   - If search term is empty/missing, warns and marks as SCIENTIFIC_NAME_EMPTY

2. QUERY ENA API
   - Calls: https://www.ebi.ac.uk/ena/taxonomy/rest/suggest-for-submission/{name}
   - Rate limited to 25 queries/second to respect API limits
   - Retries up to 3 times on failure with 2-second delays

3. FIND EXACT MATCHES
   - Compares search term against returned 'scientificName' fields (case-insensitive)
   - Also checks 'otherNames' field for synonyms
   - If no exact match found, marks as NO_EXACT_MATCH

4. VERIFY TAXONOMIC CONSISTENCY (Homonym Detection)
   - For each exact match, fetches full taxonomy via:
     https://www.ebi.ac.uk/ena/taxonomy/rest/tax-id/{taxid}
   - If input has 'phylum' column, checks if phylum appears in ENA lineage
   - Matches with wrong phylum are flagged as homonyms

5. SELECT BEST MATCH
   - Prioritises matches where phylum was verified
   - Falls back to unverified matches if no phylum provided in input

OUTPUT FILES
------------
The script produces a combined output file plus five category-specific files.
If --output is 'results.csv', the following files are created:

1. results.csv
   - Combined output containing all records with ENA columns appended
   
2. results-ena_matches.csv
   - Status: MATCH_TAXONOMICALLY_CONSISTENT
   - Records where ENA returned an exact match AND the phylum was verified
   - These are ready for ENA submission
   
3. results-errors.csv
   - Status: SCIENTIFIC_NAME_EMPTY, API_ERROR:*
   - Records that couldn't be processed due to missing data or API failures
   
4. results-possible_homonym.csv
   - Status: MATCH_PHYLUM_UNCHECKED, TAXONOMIC_MISMATCH_HOMONYM
   - Requires manual review to confirm correct taxon ID
   
5. results-no_ena_matches.csv
   - Status: NO_EXACT_MATCH, NO_RESULTS_RETURNED
   - May indicate spelling errors, synonyms not in ENA, or taxa not yet in database

6. results-filtered.csv
   - All records EXCEPT those with MATCH_TAXONOMICALLY_CONSISTENT status

OUTPUT COLUMNS
--------------
All input columns are preserved, plus the following ENA columns are appended:

    ena_search_term_2: The actual term searched (after cf. removal)
    ena_search_source_2: Column used for search
    ena_scientificName_2: Matched name in ENA database
    ena_taxid_2: ENA taxonomy ID (use this for submissions)
    ena_rank_2: Taxonomic rank in ENA (species, genus, family, etc.)
    ena_otherNames_2: Synonyms/alternative names in ENA (semicolon-separated)
    ena_status_2: Result status code (see STATUS CODES below)
    ena_lineage_2: Full taxonomic lineage from ENA

STATUS CODES
------------
    MATCH_TAXONOMICALLY_CONSISTENT: Exact match found, phylum verified
    MATCH_PHYLUM_UNCHECKED: Exact match found, but no phylum to verify
    TAXONOMIC_MISMATCH_HOMONYM: Exact match but phylum mismatch (wrong kingdom/phylum)
    NO_EXACT_MATCH: API returned results but none matched exactly
    NO_RESULTS_RETURNED: API returned empty results
    SCIENTIFIC_NAME_EMPTY: No search name provided in input row (warning issued)
    API_ERROR: *: Various API communication errors (includes error details)

USAGE
-----
    python 04_ena_taxid_check.py --input annotated.xlsx --output results.csv
    python 04_ena_taxid_check.py -i annotated.xlsx -o results.csv -mv confirmed_taxonomy
    
    Arguments:
        --input, -i         : Path to input file (CSV, TSV, or XLSX)
        --output, -o        : Path to output CSV file (split files use same base name)
        --man_verify_col, -mv : Column name to use as ENA search term (default: man_ver_name)

LOGGING
-------
A log file (04_ena_taxid_check.log) is created in the output directory containing:
    - API calls made
    - Match details and decisions
    - Summary statistics
    - Any warnings or errors

Author: Dan Parsons @NHMUK
"""

import os
import sys
import pandas as pd
from pathlib import Path
import pathlib
import logging
import csv
import argparse
import requests
import urllib.parse
import time

# Configure logger
logger = logging.getLogger()
logger.addHandler(logging.StreamHandler(sys.stdout))
logger.setLevel(logging.INFO)


def setup_logging(output_path):
    """Set up file logging in the output directory"""
    output_dir = os.path.dirname(output_path)
    
    # Create output directory if it doesn't exist
    if output_dir and not os.path.exists(output_dir):
        os.makedirs(output_dir)
        logger.info(f"Created output directory: {output_dir}")
    
    log_file = os.path.join(output_dir, "04_ena_taxid_check.log") if output_dir else "04_ena_taxid_check.log"
    file_handler = logging.FileHandler(log_file)
    formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)
    logger.info(f"Logging to: {log_file}")


def read_file(file_path):
    """Read input file (CSV, TSV, or XLSX)"""
    if not Path(file_path).is_file():
        logger.error(f"Error: The file '{file_path}' does not exist.")
        sys.exit(1)
    
    file_suffix = pathlib.Path(file_path).suffix.lower()
    
    if file_suffix == ".xlsx":
        logger.info("Reading Excel file")
        df = pd.read_excel(file_path)
    elif file_suffix == ".tsv":
        logger.info("Reading TSV file (tab-delimited)")
        df = pd.read_csv(file_path, delimiter='\t')
    elif file_suffix == ".csv":
        logger.info("Reading CSV file (comma-delimited)")
        df = pd.read_csv(file_path, delimiter=',')
    else:
        logger.error(f"Unsupported file extension '{file_suffix}'. Supported formats: .csv, .tsv, .xlsx")
        sys.exit(1)
    
    # Drop rows that are entirely empty
    df.dropna(how="all", inplace=True)
    return df


def is_empty_value(value):
    """Check if a value is empty, NaN, or 'not collected'"""
    if pd.isna(value):
        return True
    str_val = str(value).strip().lower()
    return str_val in ('', 'nan', 'not collected', 'none')


def search_ena_taxonomy(taxon_name, higher_taxonomy, logger):
    """
    Search ENA taxonomy database for a taxon name and verify with higher taxonomy.
    
    Parameters:
    - taxon_name: The name to search for
    - higher_taxonomy: dict containing phylum, class, order, family as available
    
    Returns dict with ENA fields or error information
    """
    result = {
        'ena_scientificName_2': '',
        'ena_taxid_2': '',
        'ena_displayName_2': '',
        'ena_rank_2': '',
        'ena_otherNames_2': '',
        'ena_commonName_2': '',
        'ena_status_2': '',
        'ena_lineage_2': ''
    }
    
    if not taxon_name:
        result['ena_status_2'] = 'SCIENTIFIC_NAME_EMPTY'
        return result
    
    # Clean taxon name - remove "cf." qualifier
    clean_taxon_name = taxon_name
    if " cf. " in clean_taxon_name:
        clean_taxon_name = clean_taxon_name.replace(" cf. ", " ")
        logger.info(f"Removed 'cf.' qualifier from taxon name: '{taxon_name}' -> '{clean_taxon_name}'")
    
    # URL encode the cleaned taxon name
    encoded_name = urllib.parse.quote(clean_taxon_name)
    api_url = f'https://www.ebi.ac.uk/ena/taxonomy/rest/suggest-for-submission/{encoded_name}'
    
    max_retries = 3
    retry_delay = 2  # seconds
    
    for attempt in range(max_retries):
        try:
            logger.info(f"Calling ENA API: {api_url}")
            response = requests.get(api_url, timeout=10)
            response.raise_for_status()
            
            results = response.json()
            
            # No results found
            if not results:
                logger.info(f"No results found for '{clean_taxon_name}'")
                result['ena_status_2'] = 'NO_RESULTS_RETURNED'
                return result
            
            logger.info(f"Found {len(results)} potential matches from ENA for '{clean_taxon_name}'")
            
            # First, find exact matches
            taxon_name_lower = clean_taxon_name.lower()
            exact_matches = []
            
            # Log all matches for debugging
            logger.info(f"Checking all {len(results)} matches for '{clean_taxon_name}':")
            
            for idx, taxon in enumerate(results):
                sci_name = taxon.get('scientificName', '')
                tax_id = taxon.get('taxId', '')
                display_name = taxon.get('displayName', '')
                
                logger.info(f"  Match {idx+1}: scientificName='{sci_name}', taxId={tax_id}, displayName='{display_name}'")
                
                # Check for direct match with scientificName
                is_match = False
                if sci_name.lower() == taxon_name_lower:
                    logger.info(f"  -> EXACT MATCH found in scientificName")
                    is_match = True
                
                # Check in otherNames for exact match
                if not is_match:
                    other_names = taxon.get('otherNames', [])
                    if other_names:
                        logger.info(f"     otherNames: {other_names}")
                        
                    for other_name in other_names:
                        # Extract the name part before any colon
                        name_parts = other_name.split(':', 1)
                        other_name_clean = name_parts[0].strip()
                        
                        if other_name_clean.lower() == taxon_name_lower:
                            logger.info(f"  -> EXACT MATCH found in otherNames: '{other_name}'")
                            is_match = True
                            break
                
                if is_match:
                    exact_matches.append(taxon)
            
            # If no exact matches, return no match
            if not exact_matches:
                logger.warning(f"No exact match found among {len(results)} results for '{clean_taxon_name}'")
                result['ena_status_2'] = 'NO_EXACT_MATCH'
                return result
            
            # For each match, verify taxonomic consistency
            valid_matches = []
            homonyms = []
            
            for match in exact_matches:
                tax_id = match.get('taxId', '')
                
                # Skip if no tax_id
                if not tax_id:
                    continue
                
                # Get lineage from ENA taxonomy API
                try:
                    lineage_url = f'https://www.ebi.ac.uk/ena/taxonomy/rest/tax-id/{tax_id}'
                    lineage_response = requests.get(lineage_url, timeout=10)
                    lineage_response.raise_for_status()
                    taxon_data = lineage_response.json()
                    
                    # Get lineage as string
                    lineage_str = taxon_data.get('lineage', '')
                    logger.info(f"Lineage for taxId {tax_id}: {lineage_str}")
                    
                    # Check if phylum matches
                    is_consistent = True
                    phylum_matched = False
                    is_homonym = False

                    if 'phylum' in higher_taxonomy and higher_taxonomy['phylum'] and 'lineage' in taxon_data:
                        phylum_value = higher_taxonomy['phylum'].lower()
                        lineage_lower = taxon_data.get('lineage', '').lower()
    
                        if phylum_value in lineage_lower:
                            logger.info(f"  Phylum match confirmed: '{phylum_value}' found in ENA lineage")
                            phylum_matched = True
                        else:
                            logger.info(f"  Phylum NOT found in lineage: Input phylum '{phylum_value}' not found in '{lineage_lower}'")
                            is_homonym = True
                            is_consistent = False        
                    
                    match_data = {
                        'taxon': match,
                        'lineage': lineage_str,
                        'phylum_matched': phylum_matched,
                        'is_homonym': is_homonym
                    }
                    
                    if is_homonym:
                        homonyms.append(match_data)
                    elif is_consistent:
                        valid_matches.append(match_data)
                        
                except requests.exceptions.RequestException as e:
                    logger.warning(f"Failed to get taxonomy for taxId {tax_id}: {str(e)}")
            
            # First check if we found homonyms
            if homonyms and not valid_matches:
                logger.warning(f"All {len(homonyms)} matches were taxonomic homonyms (different phylum)")
                result['ena_status_2'] = 'TAXONOMIC_MISMATCH_HOMONYM'
                return result
            
            # Process valid matches
            if valid_matches:
                phylum_matches = [m for m in valid_matches if m['phylum_matched']]
                
                if phylum_matches:
                    chosen_match = phylum_matches[0]['taxon']
                    result['ena_status_2'] = 'MATCH_TAXONOMICALLY_CONSISTENT'
                    result['ena_lineage_2'] = phylum_matches[0]['lineage']
                else:
                    chosen_match = valid_matches[0]['taxon']
                    result['ena_status_2'] = 'MATCH_PHYLUM_UNCHECKED'
                    result['ena_lineage_2'] = valid_matches[0]['lineage']
                
                result['ena_scientificName_2'] = chosen_match.get('scientificName', '')
                result['ena_taxid_2'] = str(chosen_match.get('taxId', ''))
                result['ena_displayName_2'] = chosen_match.get('displayName', '')
                result['ena_rank_2'] = chosen_match.get('rank', '')
                result['ena_commonName_2'] = chosen_match.get('commonName', '')
                
                other_names_list = chosen_match.get('otherNames', [])
                result['ena_otherNames_2'] = '; '.join(other_names_list) if other_names_list else ''
                
                return result
            else:
                if homonyms:
                    result['ena_status_2'] = 'TAXONOMIC_MISMATCH_HOMONYM'
                else:
                    result['ena_status_2'] = 'API_ERROR: Lineage lookup failed for all matches'
                return result
            
        except requests.exceptions.RequestException as e:
            if attempt < max_retries - 1:
                logger.warning(f"API request failed: {str(e)}. Retrying in {retry_delay} seconds...")
                time.sleep(retry_delay)
                continue
            logger.error(f"API request failed after {max_retries} attempts: {str(e)}")
            result['ena_status_2'] = f'API_ERROR: {str(e)}'
            return result
        
        except (ValueError, KeyError) as e:
            logger.error(f"API response parsing error: {str(e)}")
            result['ena_status_2'] = f'API_ERROR: Response parsing error - {str(e)}'
            return result
    
    result['ena_status_2'] = 'API_ERROR: Maximum retries exceeded'
    return result


def process_dataframe(df, man_verify_col, logger):
    """
    Process ALL rows in the dataframe, searching ENA taxonomy for each.
    Rows with an empty search column are warned about and marked SCIENTIFIC_NAME_EMPTY.
    Appends ENA columns to all rows.
    """
    # Initialize ENA columns with empty values
    ena_columns = {
        'ena_search_term_2': [''] * len(df),
        'ena_search_source_2': [''] * len(df),
        'ena_scientificName_2': [''] * len(df),
        'ena_taxid_2': [''] * len(df),
        'ena_displayName_2': [''] * len(df),
        'ena_rank_2': [''] * len(df),
        'ena_otherNames_2': [''] * len(df),
        'ena_commonName_2': [''] * len(df),
        'ena_status_2': [''] * len(df),
        'ena_lineage_2': [''] * len(df)
    }
    
    # 40ms = 25 queries per second
    min_delay = 0.04
    last_request_time = 0
    
    # Track processing stats
    processed_count = 0
    empty_count = 0
    
    # Pre-flight check: warn about any rows with empty search column
    empty_rows = []
    for idx, row in df.iterrows():
        search_val = row.get(man_verify_col, '')
        if is_empty_value(search_val):
            sample_id = row.get('ID', idx)
            empty_rows.append((idx, sample_id))

    if empty_rows:
        logger.warning(
            f"WARNING: {len(empty_rows)} row(s) have an empty '{man_verify_col}' column "
            f"and will be marked as SCIENTIFIC_NAME_EMPTY:"
        )
        for idx, sample_id in empty_rows:
            logger.warning(f"  Row index {idx} (ID={sample_id})")
    else:
        logger.info(f"All rows have a value in '{man_verify_col}' - good.")

    for idx, row in df.iterrows():
        # Get search term
        search_term = row.get(man_verify_col, '')
        if not is_empty_value(search_term):
            search_term = str(search_term).strip()
        else:
            search_term = None
        
        # Collect higher taxonomy for homonym disambiguation
        higher_taxonomy = {}
        for level in ['phylum', 'class', 'order', 'family', 'genus']:
            if level in row and pd.notna(row[level]) and str(row[level]).strip().lower() not in ('', 'not collected', 'nan'):
                higher_taxonomy[level] = str(row[level]).strip()
        
        sample_id = row.get('ID', idx)
        logger.info(f"Row {idx} (ID={sample_id}): searching '{man_verify_col}' = '{search_term}'")
        
        # Rate limiting
        current_time = time.time()
        time_since_last = current_time - last_request_time
        if time_since_last < min_delay:
            time.sleep(min_delay - time_since_last)
        
        last_request_time = time.time()
        
        # Search ENA
        if search_term:
            result = search_ena_taxonomy(search_term, higher_taxonomy, logger)
            ena_columns['ena_search_term_2'][idx] = search_term
            processed_count += 1
        else:
            result = {
                'ena_scientificName_2': '',
                'ena_taxid_2': '',
                'ena_displayName_2': '',
                'ena_rank_2': '',
                'ena_otherNames_2': '',
                'ena_commonName_2': '',
                'ena_status_2': 'SCIENTIFIC_NAME_EMPTY',
                'ena_lineage_2': ''
            }
            ena_columns['ena_search_term_2'][idx] = ''
            empty_count += 1
        
        # Store results
        ena_columns['ena_search_source_2'][idx] = man_verify_col
        ena_columns['ena_scientificName_2'][idx] = result['ena_scientificName_2']
        ena_columns['ena_taxid_2'][idx] = result['ena_taxid_2']
        ena_columns['ena_displayName_2'][idx] = result['ena_displayName_2']
        ena_columns['ena_rank_2'][idx] = result['ena_rank_2']
        ena_columns['ena_otherNames_2'][idx] = result['ena_otherNames_2']
        ena_columns['ena_commonName_2'][idx] = result['ena_commonName_2']
        ena_columns['ena_status_2'][idx] = result['ena_status_2']
        ena_columns['ena_lineage_2'][idx] = result.get('ena_lineage_2', '')
    
    logger.info(
        f"Processing complete: {processed_count} rows searched, "
        f"{empty_count} rows skipped (empty '{man_verify_col}')"
    )
    
    # Append ENA columns to dataframe
    for col_name, col_data in ena_columns.items():
        df[col_name] = col_data
    
    return df


def get_output_filepath(base_output_path, suffix):
    """
    Generate output filepath with suffix inserted before the extension.
    e.g., 'results.csv' with suffix '-ena_matches' becomes 'results-ena_matches.csv'
    """
    path = Path(base_output_path)
    stem = path.stem
    extension = path.suffix
    parent = path.parent
    
    new_filename = f"{stem}{suffix}{extension}"
    return parent / new_filename


def categorize_status(status):
    """
    Categorize an ena_status_2 string into one of the output file categories.
    
    Returns one of: 'ena_matches', 'errors', 'possible_homonym', 'no_ena_matches'
    """
    if not status or str(status).strip() == '':
        return 'errors'  # Shouldn't happen since all rows are processed, but handle gracefully
    
    if status == 'MATCH_TAXONOMICALLY_CONSISTENT':
        return 'ena_matches'
    elif status == 'SCIENTIFIC_NAME_EMPTY' or str(status).startswith('API_ERROR'):
        return 'errors'
    elif status in ('MATCH_PHYLUM_UNCHECKED', 'TAXONOMIC_MISMATCH_HOMONYM'):
        return 'possible_homonym'
    elif status in ('NO_EXACT_MATCH', 'NO_RESULTS_RETURNED'):
        return 'no_ena_matches'
    else:
        logger.warning(f"Unexpected ena_status_2 '{status}' - categorizing as error")
        return 'errors'


def write_split_outputs(output_df, base_output_path, logger):
    """
    Split the output dataframe by ena_status_2 category and write to separate files.
    Since all rows are processed, all rows appear in one of the split files.
    
    Categories:
    - ena_matches: MATCH_TAXONOMICALLY_CONSISTENT
    - errors: SCIENTIFIC_NAME_EMPTY, API_ERROR:*
    - possible_homonym: MATCH_PHYLUM_UNCHECKED, TAXONOMIC_MISMATCH_HOMONYM
    - no_ena_matches: NO_EXACT_MATCH, NO_RESULTS_RETURNED
    - filtered: All records EXCEPT MATCH_TAXONOMICALLY_CONSISTENT
    """
    category_suffixes = {
        'ena_matches': '-ena_matches',
        'errors': '-errors',
        'possible_homonym': '-possible_homonym',
        'no_ena_matches': '-no_ena_matches'
    }
    
    output_df = output_df.copy()
    output_df['_category'] = output_df['ena_status_2'].apply(categorize_status)
    
    for category, suffix in category_suffixes.items():
        category_df = output_df[output_df['_category'] == category].drop(columns=['_category'])
        output_path = get_output_filepath(base_output_path, suffix)
        category_df.to_csv(output_path, index=False)
        logger.info(f"Wrote {len(category_df)} rows to '{output_path}'")
    
    # Filtered file: everything except confirmed matches
    filtered_df = output_df[
        output_df['ena_status_2'] != 'MATCH_TAXONOMICALLY_CONSISTENT'
    ].drop(columns=['_category'])
    filtered_output_path = get_output_filepath(base_output_path, '-filtered')
    filtered_df.to_csv(filtered_output_path, index=False)
    logger.info(
        f"Wrote {len(filtered_df)} rows to '{filtered_output_path}' "
        f"(filtered: excludes MATCH_TAXONOMICALLY_CONSISTENT rows)"
    )
    
    output_df.drop(columns=['_category'], inplace=True)


def main(input_file, output_file, man_verify_col):
    setup_logging(output_file)
    
    logger.info(f"Reading input file: {input_file}")
    df = read_file(input_file)
    logger.info(f"Read {len(df)} rows from input file")
    logger.info(f"Input columns: {df.columns.tolist()}")
    
    # Check that the search column exists
    if man_verify_col not in df.columns:
        logger.error(f"Search column '{man_verify_col}' not found in input.")
        logger.error(f"Available columns: {df.columns.tolist()}")
        sys.exit(1)
    
    logger.info(f"ENA search column: '{man_verify_col}'")
    logger.info(f"Total rows to process: {len(df)}")
    
    logger.info("Starting ENA taxonomy searches (rate limited to 25 queries/second)...")
    df_with_ena = process_dataframe(df, man_verify_col, logger)
    logger.info("Completed ENA taxonomy searches")
    
    # Save combined output (all rows, ENA columns appended)
    df_with_ena.to_csv(output_file, index=False)
    logger.info(f"Combined output written to '{output_file}' ({len(df_with_ena)} rows)")
    
    # Write split output files by category
    logger.info("Writing split output files by ena_status_2 category...")
    write_split_outputs(df_with_ena, output_file, logger)
    
    # Summary statistics
    status_counts = df_with_ena['ena_status_2'].value_counts()
    logger.info("Summary of ENA search results:")
    for status, count in status_counts.items():
        logger.info(f"  {status}: {count}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description='Search ENA taxonomy database for all rows in the input file, '
                    'using the column specified by --man_verify_col as the search term.'
    )
    parser.add_argument('--input', '-i', dest='input', required=True,
                        help='Input CSV/TSV/XLSX file path')
    parser.add_argument('--output', '-o', dest='output', required=True,
                        help='Output CSV file path')
    parser.add_argument('--man_verify_col', '-mv', dest='man_verify_col', default='man_ver_name',
                        help='Column name to use as ENA search term (default: man_ver_name)')
    
    args = parser.parse_args()
    main(args.input, args.output, args.man_verify_col)