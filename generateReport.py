#!/usr/bin/env python3
"""
generateReport.py

Entry point for FEV block-run reporting.

Usage:
  python3 generateReport.py --run_location <path> [--output <html_file>]

Data extraction : report_data.py
HTML generation : report_html.py
"""

import os
import sys
import argparse
import yaml
from pathlib import Path

from report_data import (
    BlockRunAnalyzer,
    load_global_pattern_config,
    _merge_pattern_config,
    find_all_blocks,
    load_chip_hierarchy,
    CHIP_SCHEMA,
)
from report_html import (
    HTMLReportGenerator,
    CSVReportGenerator,
    serve_mode,
    comments_file_path,
    load_comments,
)

def main():
    parser = argparse.ArgumentParser(description="Generate HTML report from FEV block run logs")
    parser.add_argument('--run_location', help='Base directory containing block runs (default: current directory)')
    parser.add_argument('--output', default='fev_report.html', help='Output HTML file name')
    parser.add_argument('--block_name', help='Analyze only a specific block (optional)')
    parser.add_argument('--pattern_file', help='Path to global pattern.yaml file')
    parser.add_argument('--only_status', action='store_true',
                        help='Only generate status.html (skips the main HTML report and CSV reports)')
    parser.add_argument('--serve', action='store_true',
                        help='Start a local server so Save Comments writes directly to the filesystem')
    parser.add_argument('--port', type=int, default=8765,
                        help='Port for the local comment server (default: 8765)')
    args = parser.parse_args()

    # Reject --output values that would collide with the auto-generated status.html
    output_basename = os.path.basename(args.output).lower()
    if output_basename == 'status.html':
        print("ERROR: 'status.html' cannot be used as the --output filename because it "
              "collides with the block-status page automatically generated from the "
              "'status.csv' directive in pattern.yaml.\n"
              "Please choose a different name (e.g. 'fev_report.html').")
        sys.exit(1)

    # Use current directory if run_location not specified
    run_location = args.run_location if args.run_location else os.getcwd()

    if not os.path.isdir(run_location):
        print(f"ERROR: Run location does not exist: {run_location}")
        sys.exit(1)

    # --serve: skip report generation entirely, just start the save server
    if args.serve:
        serve_mode(run_location, args.port)
        sys.exit(0)
    
    # Load global pattern configuration
    global_pattern_config = None
    if args.pattern_file:
        global_pattern_config = load_global_pattern_config(args.pattern_file)
    else:
        # Look for pattern.yaml in the script directory
        script_dir = Path(__file__).parent
        default_pattern_file = os.path.join(script_dir, 'pattern.yaml')
        if os.path.isfile(default_pattern_file):
            print(f"Using default pattern file: {default_pattern_file}")
            global_pattern_config = load_global_pattern_config(default_pattern_file)
    
    # Find all blocks
    if args.block_name:
        block_names = [b.strip() for b in args.block_name.split(",") if b.strip()]
        blocks = []
        for bn in block_names:
            block_dir = os.path.join(run_location, bn)
            if not os.path.isdir(block_dir):
                print(f"ERROR: Block directory not found: {block_dir}")
                sys.exit(1)
            blocks.append((bn, block_dir))
    else:
        blocks = find_all_blocks(run_location)

    if not blocks:
        print(f"No block runs found in {run_location}")
        sys.exit(1)
    
    print(f"Found {len(blocks)} block(s) to analyze")
    
    # Analyze each block
    results = []
    pattern_file_dir = run_location  # look for ${block}.pattern.yaml in run_location (cwd)
    for block_name, block_dir in blocks:
        print(f"Analyzing {block_name}...")
        # Merge block-specific pattern file if it exists
        block_config = global_pattern_config
        if pattern_file_dir:
            block_yaml = os.path.join(pattern_file_dir, f"{block_name}.pattern.yaml")
            if os.path.isfile(block_yaml):
                print(f"  Merging block-specific patterns from {block_yaml}")
                try:
                    with open(block_yaml, 'r') as bf:
                        block_override = yaml.safe_load(bf) or {}
                    block_config = _merge_pattern_config(global_pattern_config, block_override)
                    print(f"    files: {len(block_config.get('files',[]))} total, "
                          f"ignore_error: {len(block_config.get('ignore_error',[]))}, "
                          f"ignore_warning: {len(block_config.get('ignore_warning',[]))}, "
                          f"report_patterns: {len(block_config.get('report_patterns',[]))}")
                except Exception as e:
                    print(f"  Warning: failed to load {block_yaml}: {e}")
            else:
                print(f"  No block-specific pattern file at {block_yaml}")
        analyzer = BlockRunAnalyzer(block_dir, block_name, block_config)
        result = analyzer.analyze()
        results.append(result)
    
    # Common setup needed by both full and status-only paths
    csv_directives    = (global_pattern_config or {}).get('csv_directives', [])
    ordered_pat_names = (global_pattern_config or {}).get('_ordered_pattern_names', [])

    # Load chip hierarchy from the hardcoded schema path
    chip_hierarchy = load_chip_hierarchy(CHIP_SCHEMA)
    _chip_name = chip_hierarchy[0] if chip_hierarchy and chip_hierarchy[0] else None
    _hierarchy = chip_hierarchy[1] if chip_hierarchy and len(chip_hierarchy) > 1 else None

    csv_generator = CSVReportGenerator(run_location)

    # Load persistent block comments (stored one level above run_location)
    comments = load_comments(run_location)
    print(f"Loaded {len(comments)} saved comment(s) from {comments_file_path(run_location)}")

    if args.only_status:
        # Fast path: only generate status.html, skip everything else
        print("\nRunning in --only_status mode: generating status.html only.")
        csv_generator.generate_custom_htmls(
            results, csv_directives, _chip_name, _hierarchy, ordered_pat_names,
            comments=comments, port=args.port
        )
    else:
        # Generate HTML report
        output_path = os.path.join(run_location, args.output)
        # If output_path is a directory, append default filename
        if os.path.isdir(output_path):
            output_path = os.path.join(output_path, 'fev_report.html')

        generator = HTMLReportGenerator(output_path)
        generator.generate(results, chip_hierarchy=chip_hierarchy)

        # Generate CSV reports
        csv_generator.generate(results)

        # Generate custom CSVs declared via 'csv:' directives in pattern.yaml
        csv_generator.generate_custom_csvs(
            results, csv_directives, _chip_name, _hierarchy, ordered_pat_names
        )

        # Generate HTML status pages that mirror the custom CSVs
        csv_generator.generate_custom_htmls(
            results, csv_directives, _chip_name, _hierarchy, ordered_pat_names,
            comments=comments, port=args.port
        )

    print(f"\nReport generation complete!")
    print(f"Summary:")
    for result in results:
        status_label = HTMLReportGenerator.STATUS_LABELS[result['overall_status']]
        print(f"  {result['block_name']}: {status_label}")
    print(f"\nTo save comments directly to the filesystem, start the local server:")
    print(f"  python3 generateReport.py --serve --run_location {run_location} --port {args.port}")
    print(f"  Then open: http://localhost:{args.port}/status.html")


if __name__ == '__main__':
    main()
