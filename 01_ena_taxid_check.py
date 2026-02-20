"""
ENA Taxonomy ID Checker (01_ena_taxid_check.py)
===============================================

This script validates taxonomic names against the European Nucleotide Archive (ENA) 
taxonomy database and retrieves corresponding taxon IDs for sequence submission.

OVERVIEW
--------
The script takes a spreadsheet of specimen records containing taxonomic information,
queries the ENA taxonomy API for each record, and outputs validated taxonomy data
with ENA taxon IDs. Results are split into separate files based on match quality
to facilitate downstream processing workflows.

INPUT REQUIREMENTS
------------------
Supported file formats: CSV, TSV, XLSX

Required columns:
    - scientificName: The taxonomic name to search (species, genus, or higher rank)
    
Recommended columns for homonym disambiguation:
    - phylum: Used to verify matches against ENA lineage (critical for resolving homonyms)
    
Optional columns (passed through to output):
    - ID: Sample/specimen identifier
    - identified_rank: Taxonomic rank of the identification
    - class, order, family, genus: Higher taxonomy for reference
    - NCBI_taxid, NCBI_matched_rank, NCBI_lineage: Pre-existing NCBI taxonomy data
    - lineage_mismatch: Flag for taxonomy conflicts
    - type_status: Type specimen status
    - note: Additional notes

LOGICAL PROCESS
---------------
For each row in the input:

1. EXTRACT SEARCH TERM
   - Reads the 'scientificName' column
   - Removes 'cf.' qualifiers if present (e.g., "Genus cf. species" -> "Genus species")
   - If scientificName is empty/missing, marks as MAN_VER_NAME_EMPTY

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
   - This step is critical because many taxonomic names are homonyms across
     kingdoms (e.g., "Atlanta" is both a gastropod and a plant genus)

5. SELECT BEST MATCH
   - Prioritises matches where phylum was verified
   - Falls back to unverified matches if no phylum provided in input

OUTPUT FILES
------------
The script produces a combined output file plus five category-specific files.
If --output is 'results.csv', the following files are created:

1. results.csv
   - Combined output containing all processed records
   
2. results-ena_matches.csv
   - Status: MATCH_TAXONOMICALLY_CONSISTENT
   - Records where ENA returned an exact match AND the phylum was verified
   - These are ready for ENA submission
   
3. results-errors.csv
   - Status: MAN_VER_NAME_EMPTY, API_ERROR:*
   - Records that couldn't be processed due to missing data or API failures
   - Requires manual review to add missing scientificName or retry
   
4. results-possible_homonym.csv
   - Status: MATCH_PHYLUM_UNCHECKED, TAXONOMIC_MISMATCH_HOMONYM
   - MATCH_PHYLUM_UNCHECKED: Match found but no phylum to verify against
   - TAXONOMIC_MISMATCH_HOMONYM: Match found but phylum doesn't match (likely wrong kingdom)
   - Requires manual review to confirm correct taxon ID
   
5. results-no_ena_matches.csv
   - Status: NO_EXACT_MATCH, NO_RESULTS_RETURNED
   - NO_RESULTS_RETURNED: ENA API returned no suggestions
   - NO_EXACT_MATCH: ENA returned suggestions but none matched exactly
   - May indicate spelling errors, synonyms not in ENA, or taxa not yet in database

6. results-filtered.csv
   - All records EXCEPT those with MATCH_TAXONOMICALLY_CONSISTENT status
   - Combines errors, possible_homonym, and no_ena_matches for review

OUTPUT COLUMNS
--------------
Input columns are preserved, plus the following ENA columns are added:

    ena_search_term_1: The actual term searched (after cf. removal)
    ena_search_source_1: Always 'scientificName' (column used for search)
    ena_scientificName_1: Matched name in ENA database
    ena_taxid_1: ENA taxonomy ID (use this for submissions)
    ena_rank_1: Taxonomic rank in ENA (species, genus, family, etc.)
    ena_otherNames_1: Synonyms/alternative names in ENA (semicolon-separated)
    ena_status_1: Result status code (see STATUS CODES below)
    ena_lineage_1: Full taxonomic lineage from ENA

STATUS CODES
------------
    MATCH_TAXONOMICALLY_CONSISTENT: Exact match found, phylum verified
    MATCH_PHYLUM_UNCHECKED: Exact match found, but no phylum to verify
    TAXONOMIC_MISMATCH_HOMONYM: Exact match but phylum mismatch (wrong kingdom/phylum)
    NO_EXACT_MATCH: API returned results but none matched exactly
    NO_RESULTS_RETURNED: API returned empty results
    MAN_VER_NAME_EMPTY: No scientificName provided in input
    API_ERROR: *: Various API communication errors (includes error details)

USAGE
-----
    python 01_ena_taxid_check4.py --input specimens.csv --output results.csv
    
    Arguments:
        --input, --in   : Path to input file (CSV, TSV, or XLSX)
        --output, --out : Path to output CSV file (split files use same base name)

LOGGING
-------
A log file (01_ena_taxid_check.log) is created in the output directory containing:
    - API calls made
    - Match details and decisions
    - Summary statistics
    - Any warnings or errors

EXAMPLES
--------
Input row:
    ID,scientificName,phylum,family
    SAMPLE001,Vanessa atalanta,Arthropoda,Nymphalidae

Output (successful match):
    ena_search_term_1: Vanessa atalanta
    ena_scientificName_1: Vanessa atalanta
    ena_taxid_1: 110450
    ena_rank_1: species
    ena_status_1: MATCH_TAXONOMICALLY_CONSISTENT
    ena_lineage_1: Eukaryota; Metazoa; Arthropoda; Hexapoda; Insecta; ...

NOTES
-----
- Rate limiting: The script respects ENA API limits (25 queries/second)
- Homonyms: Always provide phylum when possible to avoid incorrect matches
- Synonyms: The script checks ENA's 'otherNames' field for synonym matches
- cf. handling: "cf." qualifiers are automatically removed before searching

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
    
    log_file = os.path.join(output_dir, "01_ena_taxid_check.log") if output_dir else "01_ena_taxid_check.log"
    file_handler = logging.FileHandler(log_file)
    formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)
    logger.info(f"Logging to: {log_file}")

def detect_delimiter(file_path):
    """Detect the delimiter used in a CSV file"""
    with open(file_path, 'r') as csvfile:
        dialect = csv.Sniffer().sniff(csvfile.read(1024))
        return dialect.delimiter

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

def get_search_term(row):
    """
    Get the taxonomic search term from scientificName only.
    No fallback to other columns.
    
    Returns tuple: (search_term, source_column) or (None, None) if empty
    """
    scientific_name_val = row.get('scientificName', '')
    
    if not is_empty_value(scientific_name_val):
        return (str(scientific_name_val).strip(), "scientificName")
    
    return (None, None)

def search_ena_taxonomy(taxon_name, higher_taxonomy, search_level, logger):
    """
    Search ENA taxonomy database for a taxon name and verify with higher taxonomy.
    
    Parameters:
    - taxon_name: The name to search for (scientificName, genus, or family)
    - higher_taxonomy: dict containing phylum, class, order, family as available
    - search_level: Level of the search ('scientificName', 'genus', or 'family')
    
    Returns dict with ENA fields or error information
    """
    result = {
        'ena_scientificName_1': '',
        'ena_taxid_1': '',
        'ena_displayName_1': '',
        'ena_rank_1': '',
        'ena_otherNames_1': '',
        'ena_commonName_1': '',
        'ena_status_1': '',
        'ena_lineage_1': ''
    }
    
    if not taxon_name:
        result['ena_status_1'] = 'MAN_VER_NAME_EMPTY'
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
                result['ena_status_1'] = 'NO_RESULTS_RETURNED'
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
                result['ena_status_1'] = 'NO_EXACT_MATCH'
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
                    # Get taxonomy info including lineage
                    lineage_url = f'https://www.ebi.ac.uk/ena/taxonomy/rest/tax-id/{tax_id}'
                    lineage_response = requests.get(lineage_url, timeout=10)
                    lineage_response.raise_for_status()
                    taxon_data = lineage_response.json()
                    
                    # Get lineage as string
                    lineage_str = taxon_data.get('lineage', '')
                    logger.info(f"Lineage for taxId {tax_id}: {lineage_str}")
                    
                    # Check if phylum matches
                    
                    # Check if phylum matches
                    is_consistent = True
                    phylum_matched = False
                    is_homonym = False

                    if 'phylum' in higher_taxonomy and higher_taxonomy['phylum'] and 'lineage' in taxon_data:
                        phylum_value = higher_taxonomy['phylum'].lower()
                        lineage_lower = taxon_data.get('lineage', '').lower()
    
                        # Check if phylum appears anywhere in the lineage
                        if phylum_value in lineage_lower:
                            logger.info(f"  Phylum match confirmed: '{phylum_value}' found in ENA lineage")
                            phylum_matched = True
                        else:
                            logger.info(f"  Phylum NOT found in lineage: Input phylum '{phylum_value}' not found in '{lineage_lower}'")
                            is_homonym = True
                            is_consistent = False        
                    
                    # If consistent or no phylum to check against, add to valid matches
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
            
            # Process results based on matches found
            
            # First check if we found homonyms
            if homonyms and not valid_matches:
                # All matches were homonyms
                logger.warning(f"All {len(homonyms)} matches were taxonomic homonyms (different phylum)")
                result['ena_status_1'] = 'TAXONOMIC_MISMATCH_HOMONYM'
                return result
            
            # Process valid matches
            if valid_matches:
                # If we have matches with matching phylum, prioritize those
                phylum_matches = [m for m in valid_matches if m['phylum_matched']]
                
                if phylum_matches:
                    chosen_match = phylum_matches[0]['taxon']
                    result['ena_status_1'] = 'MATCH_TAXONOMICALLY_CONSISTENT'
                    result['ena_lineage_1'] = phylum_matches[0]['lineage']
                else:
                    chosen_match = valid_matches[0]['taxon']
                    result['ena_status_1'] = 'MATCH_PHYLUM_UNCHECKED'
                    result['ena_lineage_1'] = valid_matches[0]['lineage']
                
                # Use the chosen match
                result['ena_scientificName_1'] = chosen_match.get('scientificName', '')
                result['ena_taxid_1'] = str(chosen_match.get('taxId', ''))
                result['ena_displayName_1'] = chosen_match.get('displayName', '')
                result['ena_rank_1'] = chosen_match.get('rank', '')
                result['ena_commonName_1'] = chosen_match.get('commonName', '')
                
                # Join otherNames into comma-separated string
                other_names_list = chosen_match.get('otherNames', [])
                result['ena_otherNames_1'] = '; '.join(other_names_list) if other_names_list else ''
                
                return result
            else:
                # No taxonomically consistent matches
                if homonyms:
                    result['ena_status_1'] = 'TAXONOMIC_MISMATCH_HOMONYM'
                else:
                    result['ena_status_1'] = 'API_ERROR: Lineage lookup failed for all matches'
                return result
            
        except requests.exceptions.RequestException as e:
            if attempt < max_retries - 1:
                logger.warning(f"API request failed: {str(e)}. Retrying in {retry_delay} seconds...")
                time.sleep(retry_delay)
                continue
            logger.error(f"API request failed after {max_retries} attempts: {str(e)}")
            result['ena_status_1'] = f'API_ERROR: {str(e)}'
            return result
        
        except (ValueError, KeyError) as e:
            logger.error(f"API response parsing error: {str(e)}")
            result['ena_status_1'] = f'API_ERROR: Response parsing error - {str(e)}'
            return result
    
    # Should not reach here, but just in case
    result['ena_status_1'] = 'API_ERROR: Maximum retries exceeded'
    return result

def process_dataframe(df, logger):
    """
    Process each row in the dataframe to search ENA taxonomy.
    Adds ENA columns to the dataframe.
    Only uses fallback if the primary search field is empty (not if search fails).
    """
    ena_results = []
    search_terms_used = []
    search_sources_used = []
    
    # 40ms = 25 queries per second
    min_delay = 0.04
    last_request_time = 0
    
    for idx, row in df.iterrows():
        # Get the single search term (with fallback only if primary field is empty)
        search_term, source = get_search_term(row)
        
        # Collect higher taxonomy for disambiguation
        higher_taxonomy = {}
        for level in ['phylum', 'class', 'order', 'family', 'genus']:
            if level in row and pd.notna(row[level]) and str(row[level]).strip().lower() not in ('', 'not collected', 'nan'):
                higher_taxonomy[level] = str(row[level]).strip()
        
        identified_rank = str(row.get('identified_rank', '')).strip()
        logger.info(f"Row {idx}: identified_rank='{identified_rank}', searching for '{search_term}' (source: {source})")
        
        # Rate limiting
        current_time = time.time()
        time_since_last = current_time - last_request_time
        if time_since_last < min_delay:
            time.sleep(min_delay - time_since_last)
        
        last_request_time = time.time()
        
        # Search ENA (no fallback on failed search)
        if search_term:
            result = search_ena_taxonomy(search_term, higher_taxonomy, source, logger)
        else:
            result = {
                'ena_scientificName_1': '',
                'ena_taxid_1': '',
                'ena_displayName_1': '',
                'ena_rank_1': '',
                'ena_otherNames_1': '',
                'ena_commonName_1': '',
                'ena_status_1': 'MAN_VER_NAME_EMPTY',
                'ena_lineage_1': ''
            }
        
        search_terms_used.append(search_term if search_term else '')
        search_sources_used.append(source if source else '')
        ena_results.append(result)
    
    # Add search metadata columns (with _1 suffix)
    df['ena_search_term_1'] = search_terms_used
    df['ena_search_source_1'] = search_sources_used
    
    # Add ENA result columns (with _1 suffix)
    df['ena_scientificName_1'] = [r['ena_scientificName_1'] for r in ena_results]
    df['ena_taxid_1'] = [r['ena_taxid_1'] for r in ena_results]
    df['ena_displayName_1'] = [r['ena_displayName_1'] for r in ena_results]
    df['ena_rank_1'] = [r['ena_rank_1'] for r in ena_results]
    df['ena_otherNames_1'] = [r['ena_otherNames_1'] for r in ena_results]
    df['ena_commonName_1'] = [r['ena_commonName_1'] for r in ena_results]
    df['ena_status_1'] = [r['ena_status_1'] for r in ena_results]
    df['ena_lineage_1'] = [r.get('ena_lineage_1', '') for r in ena_results]
    
    return df

def select_output_columns(df):
    """
    Select and rename columns for output.
    Matches the new input structure with ID, scientificName, identified_rank, etc.
    """
    # Define desired columns (only include if they exist in the dataframe)
    columns_map = {
        'ID': 'ID',
        'scientificName': 'scientificName',
        'identified_rank': 'identified_rank',
        'phylum': 'phylum',
        'class': 'class',
        'order': 'order',
        'family': 'family',
        'genus': 'genus',
        'NCBI_taxid': 'NCBI_taxid',
        'NCBI_matched_rank': 'NCBI_matched_rank',
        'NCBI_lineage': 'NCBI_lineage',
        'lineage_mismatch': 'lineage_mismatch',
        'type_status': 'type_status',
        'note': 'note',
        'ena_search_term_1': 'ena_search_term_1',
        'ena_search_source_1': 'ena_search_source_1',
        'ena_scientificName_1': 'ena_scientificName_1',
        'ena_taxid_1': 'ena_taxid_1',
        'ena_rank_1': 'ena_rank_1',
        'ena_otherNames_1': 'ena_otherNames_1',
        'ena_status_1': 'ena_status_1',
        'ena_lineage_1': 'ena_lineage_1'
    }
    
    # Select only columns that exist in the dataframe
    available_columns = {}
    for orig_col, new_col in columns_map.items():
        if orig_col in df.columns:
            available_columns[orig_col] = new_col
    
    # Select and rename columns
    output_df = df[list(available_columns.keys())].copy()
    output_df.rename(columns=available_columns, inplace=True)
    
    return output_df

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
    Categorize an ena_status_1 string into one of the output file categories.
    
    Returns one of: 'ena_matches', 'errors', 'possible_homonym', 'no_ena_matches'
    """
    if status == 'MATCH_TAXONOMICALLY_CONSISTENT':
        return 'ena_matches'
    elif status == 'MAN_VER_NAME_EMPTY' or status.startswith('API_ERROR'):
        return 'errors'
    elif status in ('MATCH_PHYLUM_UNCHECKED', 'TAXONOMIC_MISMATCH_HOMONYM'):
        return 'possible_homonym'
    elif status in ('NO_EXACT_MATCH', 'NO_RESULTS_RETURNED'):
        return 'no_ena_matches'
    else:
        # Fallback for any unexpected status - treat as error
        logger.warning(f"Unexpected ena_status_1 '{status}' - categorizing as error")
        return 'errors'

def write_split_outputs(output_df, base_output_path, logger):
    """
    Split the output dataframe by ena_status_1 category and write to separate files.
    
    Categories:
    - ena_matches: MATCH_TAXONOMICALLY_CONSISTENT
    - errors: MAN_VER_NAME_EMPTY, API_ERROR:*
    - possible_homonym: MATCH_PHYLUM_UNCHECKED, TAXONOMIC_MISMATCH_HOMONYM
    - no_ena_matches: NO_EXACT_MATCH, NO_RESULTS_RETURNED
    - filtered: All records EXCEPT MATCH_TAXONOMICALLY_CONSISTENT
    """
    # Define category suffixes
    category_suffixes = {
        'ena_matches': '-ena_matches',
        'errors': '-errors',
        'possible_homonym': '-possible_homonym',
        'no_ena_matches': '-no_ena_matches'
    }
    
    # Categorize each row
    output_df = output_df.copy()
    output_df['_category'] = output_df['ena_status_1'].apply(categorize_status)
    
    # Write each category to its own file
    for category, suffix in category_suffixes.items():
        category_df = output_df[output_df['_category'] == category].drop(columns=['_category'])
        output_path = get_output_filepath(base_output_path, suffix)
        
        category_df.to_csv(output_path, index=False)
        logger.info(f"Wrote {len(category_df)} rows to '{output_path}'")
    
    # Write filtered file (all rows EXCEPT MATCH_TAXONOMICALLY_CONSISTENT)
    filtered_df = output_df[output_df['ena_status_1'] != 'MATCH_TAXONOMICALLY_CONSISTENT'].drop(columns=['_category'])
    filtered_output_path = get_output_filepath(base_output_path, '-filtered')
    filtered_df.to_csv(filtered_output_path, index=False)
    logger.info(f"Wrote {len(filtered_df)} rows to '{filtered_output_path}' (filtered: excludes MATCH_TAXONOMICALLY_CONSISTENT)")
    
    # Remove temporary category column for the main output
    output_df.drop(columns=['_category'], inplace=True)

def main(input_file, output_file):
    # Set up logging
    setup_logging(output_file)
    
    logger.info(f"Reading input file: {input_file}")
    df = read_file(input_file)
    logger.info(f"Read {len(df)} rows from input file")
    
    # Check required columns exist - updated to use scientificName
    required_cols = ['scientificName', 'genus', 'family']
    missing_cols = [col for col in required_cols if col not in df.columns]
    if missing_cols:
        logger.warning(f"Missing columns (will use fallback hierarchy): {missing_cols}")
    
    # Check at least one search column exists
    search_cols = ['scientificName', 'genus', 'family']
    available_search_cols = [col for col in search_cols if col in df.columns]
    if not available_search_cols:
        logger.error(f"No search columns found. Need at least one of: {search_cols}")
        sys.exit(1)
    
    logger.info(f"Available search columns: {available_search_cols}")
    
    # Updated log message to reflect new rate limit of 25 queries/second
    logger.info("Starting ENA taxonomy searches (rate limited to 25 queries/second)...")
    df_with_ena = process_dataframe(df, logger)
    logger.info("Completed ENA taxonomy searches")
    
    # Select and rename output columns
    output_df = select_output_columns(df_with_ena)
    
    # Save combined output file
    output_df.to_csv(output_file, index=False)
    logger.info(f"Combined output file '{output_file}' created successfully with {len(output_df)} rows")
    
    # Write split output files by category
    logger.info("Writing split output files by ena_status_1 category...")
    write_split_outputs(output_df, output_file, logger)
    
    # Summary statistics
    status_counts = df_with_ena['ena_status_1'].value_counts()
    logger.info("Summary of ENA search results:")
    for status, count in status_counts.items():
        logger.info(f"  {status}: {count}")

if __name__ == "__main__":
    # Set up argument parser
    parser = argparse.ArgumentParser(
        description='Search ENA taxonomy database for scientificName/genus/family and output results'
    )
    parser.add_argument('--input', '--in', dest='input', required=True,
                        help='Input CSV/TSV/XLSX file path')
    parser.add_argument('--output', '--out', dest='output', required=True,
                        help='Output CSV file path')
    
    args = parser.parse_args()
    
    # Call the main function
    main(args.input, args.output)