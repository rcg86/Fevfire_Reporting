#!/usr/bin/env python3
"""
report_data.py

Data-extraction layer for FEV block run reports.

Contains:
  - PatternNode / pattern hierarchy helpers
  - LogAnalyzer  — log file scanner
  - BlockRunAnalyzer — per-block orchestrator
  - Utility functions: config loading, chip hierarchy, CSV expression resolver
"""

import os
import sys
import re
import glob
import yaml
import csv
import copy
import json
from datetime import datetime
from pathlib import Path

CHIP_SCHEMA = "/proj/pd/work/hitesh/REL/SKYLP/SCRIPTS/v21.03.2026/skylp_synth_data_roll_up/skylp.config.yaml"


class PatternNode:
    """Holds capture groups from a pattern match and optional child pattern nodes.

    Attributes are named ``group1``, ``group2``, ... (int when numeric, str
    otherwise) and ``matched`` (bool).  Child sub-patterns from the
    ``subpatterns`` YAML list are accessible as ``pattern1``, ``pattern2``, ...
    attributes — each is itself a PatternNode, enabling arbitrary nesting.

    Designed as a format object so that Python's str.format() attribute-access
    syntax works naturally::

        {pattern1.group1}
        {pattern1.pattern1.group2}
        {pattern2.pattern1.pattern1.group3}
    """

    def __init__(self):
        self.matched = False

    @classmethod
    def from_match(cls, m, children=None):
        """Build from a successful re.Match object.

        Parameters
        ----------
        m : re.Match
            Successful match object.
        children : dict[str, PatternNode], optional
            Map of ``'pattern1'``, ``'pattern2'``, ... to child PatternNode
            instances produced by recursively applying ``subpatterns``.
        """
        obj = cls()
        obj.matched = True
        for gi, gval in enumerate(m.groups(), 1):
            if gval is not None:
                try:
                    setattr(obj, f'group{gi}', int(gval))
                except (ValueError, TypeError):
                    setattr(obj, f'group{gi}', gval)
            else:
                setattr(obj, f'group{gi}', '')
        if children:
            for cname, cnode in children.items():
                setattr(obj, cname, cnode)
        return obj

    @classmethod
    def empty(cls):
        """Build a no-match placeholder — all groupN return '' and all patternN
        return a nested empty PatternNode so deep references degrade silently."""
        obj = cls()
        obj.matched = False
        return obj

    def __getattr__(self, name):
        """Safe fallback: groupN → '', patternN → empty PatternNode."""
        if name.startswith('group'):
            return ''
        if name.startswith('pattern'):
            # Build and cache an empty child node so repeated access is stable.
            node = PatternNode.empty()
            object.__setattr__(self, name, node)
            return node
        raise AttributeError(name)

    def __repr__(self):
        return f'PatternNode(matched={self.matched})'


def _apply_pattern_hierarchy(subpatterns_yaml, text):
    """Recursively apply a ``subpatterns`` YAML list against *text*.

    Each list item corresponds to ``pattern1``, ``pattern2``, ... on the
    result dict.  If an item itself contains a ``subpatterns`` key the same
    function is called recursively with the child match text — enforcing strict
    parent-scope matching at every level.

    Parameters
    ----------
    subpatterns_yaml : list[dict]
        Each dict must have a ``'pattern'`` key (regex string) and may have a
        ``'subpatterns'`` key for deeper nesting.
    text : str
        The text to search within (parent ``match.group(0)`` for nested calls,
        or the full log content for the root call).

    Returns
    -------
    dict[str, PatternNode]
        Maps ``'pattern1'``, ``'pattern2'``, ... to PatternNode instances.
        Non-matching entries are empty PatternNodes so that deep references
        like ``{pattern1.pattern1.group1}`` never raise an AttributeError.
    """
    result = {}
    for idx, sp_entry in enumerate(subpatterns_yaml or [], 1):
        key = f'pattern{idx}'
        sp_regex = sp_entry.get('pattern') if isinstance(sp_entry, dict) else sp_entry
        nested_yaml = sp_entry.get('subpatterns', []) if isinstance(sp_entry, dict) else []

        if sp_regex:
            m = re.search(sp_regex, text, re.DOTALL)
            if m:
                children = _apply_pattern_hierarchy(nested_yaml, m.group(0))
                result[key] = PatternNode.from_match(m, children=children)
            else:
                # Not matched — still populate empty children for deep refs.
                node = PatternNode.empty()
                for cname, cnode in _apply_pattern_hierarchy(nested_yaml, '').items():
                    setattr(node, cname, cnode)
                result[key] = node
        else:
            result[key] = PatternNode.empty()
    return result


def _render_title_format(title_format, groups_dict, occurrence, sub_patterns=None):
    """Render a title_format string with optional conditional block support.

    Conditional syntax: ``{?groupN|content}``
        - If groups_dict['groupN'] is non-None and non-empty the block is
          replaced with *content* (which may itself contain {groupN} placeholders).
        - Otherwise the whole block collapses to an empty string.

    After conditional blocks are resolved, the remaining string is rendered
    with Python's str.format() using the groups dict (None values are
    replaced with '' so they don't appear as 'None' in the title).

    Child pattern nodes (PatternNode) can be passed via *sub_patterns*, a dict
    mapping ``'pattern1'``, ``'pattern2'``, ... to PatternNode instances built
    by ``_apply_pattern_hierarchy()``.  These are merged directly into the
    format dict so that ``{pattern1.group1}`` and ``{pattern1.pattern1.group2}``
    resolve via Python's native attribute-access format syntax.
    """
    # --- Step 1: resolve conditional blocks {?groupN|content} ---
    def _replace_cond(m):
        key = m.group(1)
        inner = m.group(2)
        val = groups_dict.get(key)
        if val is not None and val != '':
            return inner
        return ''

    result = re.sub(
        r'\{\?(\w+)\|((?:[^{}]|\{[^{}]+\})*)\}',
        _replace_cond,
        title_format,
    )

    # --- Step 2: standard .format() with None → '' ---
    fmt_dict = {k: ('' if v is None else v) for k, v in groups_dict.items()}
    fmt_dict['occurrence'] = occurrence
    # Merge sub-pattern objects so {sub_pattern_1.group3} resolves via dot-access
    if sub_patterns:
        fmt_dict.update(sub_patterns)
    return result.format(**fmt_dict)


def _title_to_html(label):
    """Convert a rendered title_format string to an HTML fragment.

    Supported escape sequences (as written in single-quoted YAML strings,
    where they arrive here as literal two-character sequences):

    ``\\n``  – row break.  Each ``\\n`` starts a new line / table row.
    ``\\t``  – tab stop (em-space).  Kept for simple alignment needs.
    ``\\|``  – **column separator**.  When a row contains one or more ``\\|``
             the whole title is rendered as a ``<table>`` so every ``\\|``-
             separated cell gets an equal share of the available width,
             giving *symmetrical* alignment regardless of label length.

    Rows without ``\\|`` are rendered as plain ``<tr>`` full-width cells
    (they still support ``\\t`` em-spaces if desired).
    """
    rows = label.split('\\n')
    has_table = any('\\|' in row for row in rows)

    if not has_table:
        # Simple mode: no column separators — just replace escape sequences.
        return label.replace('\\n', '<br>').replace('\\t', '&emsp;')

    # Table mode: build an HTML table so columns align perfectly.
    # Determine the maximum number of cells in any row to set colspan correctly.
    max_cols = max((row.count('\\|') + 1) if '\\|' in row else 1 for row in rows)
    table_style = 'border-collapse: collapse; line-height: 1.6; width: 100%;'
    cell_style  = 'padding: 0 16px 0 0; white-space: nowrap; vertical-align: top;'
    full_style  = f'padding: 0; white-space: nowrap; vertical-align: top;'

    html_rows = []
    for row in rows:
        if '\\|' in row:
            cells = row.split('\\|')
            tds = ''.join(
                f'<td style="{cell_style}">{c.strip().replace(chr(92) + "t", "&emsp;")}</td>'
                for c in cells
            )
            html_rows.append(f'<tr>{tds}</tr>')
        else:
            plain = row.replace('\\t', '&emsp;')
            html_rows.append(
                f'<tr><td colspan="{max_cols}" style="{full_style}">{plain}</td></tr>'
            )

    inner = ''.join(html_rows)
    return f'<table style="{table_style}">{inner}</table>'


class LogAnalyzer:
    """Analyzes log files for patterns and categorizes results."""
    
    def __init__(self, pattern_config=None):
        # Define default patterns to search for
        if pattern_config:
            self.error_patterns = pattern_config.get('error_patterns', [])
            self.warning_patterns = pattern_config.get('warning_patterns', [])
            self.info_patterns = pattern_config.get('info_patterns', [])
            self.ignore_error = pattern_config.get('ignore_error', [])
            self.ignore_warning = pattern_config.get('ignore_warning', [])
            self.ignore_info = pattern_config.get('ignore_info', [])
            self.report_patterns = pattern_config.get('report_patterns', [])
        else:
            # Default patterns
            self.error_patterns = [
                r'\s+//\s+Error:',
                r'\s+Missing:',
                r'ERROR',
                r'FAILED',
                r'Abort'
            ]
            
            self.warning_patterns = [
                r'WARNING',
                r'Warning:',
                r'\s+//\s+Warning:'
            ]
            
            self.info_patterns = [
                r'INFO:',
                r'Note:',
                r'FYI'
            ]
            
            # Separate ignore patterns for each category
            self.ignore_error = []
            self.ignore_warning = []
            self.ignore_info = []
            self.report_patterns = []
    
    def set_patterns(self, error_patterns=None, warning_patterns=None, info_patterns=None, 
                     ignore_error=None, ignore_warning=None, ignore_info=None):
        """Allow custom patterns to be set."""
        if error_patterns:
            self.error_patterns = error_patterns
        if warning_patterns:
            self.warning_patterns = warning_patterns
        if info_patterns:
            self.info_patterns = info_patterns
        if ignore_error:
            self.ignore_error = ignore_error
        if ignore_warning:
            self.ignore_warning = ignore_warning
        if ignore_info:
            self.ignore_info = ignore_info
    
    def should_ignore_error(self, line):
        """Check if an error line should be ignored."""
        for pattern in self.ignore_error:
            if re.search(pattern, line):
                # Uncomment for debugging:
                # print(f"    [DEBUG] Ignoring error (matched '{pattern}'): {line.strip()[:80]}")
                return True
        return False
    
    def should_ignore_warning(self, line):
        """Check if a warning line should be ignored."""
        for pattern in self.ignore_warning:
            if re.search(pattern, line):
                return True
        return False
    
    def should_ignore_info(self, line):
        """Check if an info line should be ignored."""
        for pattern in self.ignore_info:
            if re.search(pattern, line):
                return True
        return False
    
    def analyze_file(self, file_path):
        """
        Analyze a single log file for patterns.
        Returns dict with status and findings.
        """
        if not os.path.isfile(file_path):
            return {
                'status': 'missing',
                'errors': [],
                'warnings': [],
                'infos': [],
                'ignored_errors': [],
                'ignored_warnings': [],
                'ignored_infos': [],
                'report_sections': [],
                'exists': False
            }
        
        errors = []
        warnings = []
        infos = []
        ignored_errors = []
        ignored_warnings = []
        ignored_infos = []
        report_sections = []
        
        try:
            # Read entire file content for multi-line pattern matching
            with open(file_path, 'r', encoding='utf-8', errors='ignore') as f:
                file_content = f.read()
            
            # Extract report sections using multi-line patterns
            for report_pattern in self.report_patterns:
                # Skip csv-directive entries (no 'pattern' key)
                if isinstance(report_pattern, dict) and 'csv' in report_pattern:
                    continue
                pattern_str = report_pattern.get('pattern') if isinstance(report_pattern, dict) else report_pattern
                if not pattern_str:
                    continue
                pattern_name = report_pattern.get('name', 'Report Section') if isinstance(report_pattern, dict) else 'Report Section'
                title_format = report_pattern.get('title_format') if isinstance(report_pattern, dict) else None
                color_rules = report_pattern.get('color_rules') if isinstance(report_pattern, dict) else None
                
                # Read hierarchical subpatterns list (new schema)
                subpatterns_yaml = report_pattern.get('subpatterns', []) if isinstance(report_pattern, dict) else []

                # Track previous occurrence's values so color_rules can reference prev_patternN.groupM
                prev_sub_patterns = {}  # {'pattern1': PatternNode, ...} from previous match
                prev_groups = {}        # {'group1': value, ...} from previous match

                # Use finditer to find ALL occurrences, not just the first one (with DOTALL flag for multi-line matching)
                for match_num, match in enumerate(re.finditer(pattern_str, file_content, re.DOTALL), 1):
                    # Append occurrence number if more than one match found
                    section_name = pattern_name if match_num == 1 else f"{pattern_name} (Occurrence {match_num})"
                    
                    # Extract capture groups and build groups dict (group1, group2, ...)
                    groups_dict = {}
                    if match.groups():
                        for gi, gval in enumerate(match.groups(), 1):
                            if gval is not None:
                                try:
                                    groups_dict[f'group{gi}'] = int(gval)
                                except ValueError:
                                    groups_dict[f'group{gi}'] = gval
                            else:
                                groups_dict[f'group{gi}'] = None

                    # ---- Apply hierarchical subpatterns (scoped to main match text only) ----
                    # Result: {'pattern1': PatternNode, 'pattern2': PatternNode, ...}
                    match_text = match.group(0)
                    sub_patterns_data = _apply_pattern_hierarchy(subpatterns_yaml, match_text)

                    # Compute dynamic title from title_format if provided
                    custom_title = None
                    if title_format and groups_dict:
                        try:
                            custom_title = _render_title_format(title_format, groups_dict, match_num,
                                                                sub_patterns=sub_patterns_data)
                        except (KeyError, IndexError, ValueError) as e:
                            custom_title = None  # Fall back to default
                    
                    # Evaluate color_rules if provided
                    title_color = None
                    if color_rules and groups_dict:
                        safe_ns = {'__builtins__': {}, 'int': int, 'float': float, 'abs': abs, 'len': len, 'True': True, 'False': False, 'None': None}
                        safe_ns.update(groups_dict)
                        # Add PatternNode objects so conditions like pattern1.group2 work
                        safe_ns.update(sub_patterns_data)
                        # Inject previous occurrence values as prev_patternN / prev_groupN
                        # so conditions like prev_pattern5.pattern2.group1 work via PatternNode dot-access
                        for pkey, pnode in prev_sub_patterns.items():
                            safe_ns[f'prev_{pkey}'] = pnode
                        for gkey, gval in prev_groups.items():
                            safe_ns[f'prev_{gkey}'] = gval
                        for rule in color_rules:
                            condition = rule.get('condition', 'False')
                            try:
                                if eval(condition, safe_ns):
                                    title_color = rule.get('color')
                                    break
                            except Exception:
                                continue

                    # Save current occurrence as previous for the next iteration
                    prev_sub_patterns = sub_patterns_data
                    prev_groups = groups_dict
                    
                    # Calculate line numbers from character positions
                    start_pos = match.start()
                    end_pos = match.end()
                    start_line = file_content[:start_pos].count('\n') + 1
                    end_line = file_content[:end_pos].count('\n') + 1
                    
                    report_sections.append({
                        'name': section_name,
                        'content': match.group(0).strip(),
                        'start_pos': start_pos,
                        'end_pos': end_pos,
                        'line_number': start_line,
                        'end_line_number': end_line,
                        'custom_title': custom_title,
                        'title_color': title_color,
                        'groups': groups_dict,
                        'sub_patterns': sub_patterns_data,
                        'has_title_format': title_format is not None,
                    })
            
            # Line-by-line analysis for errors, warnings, infos
            with open(file_path, 'r', encoding='utf-8', errors='ignore') as f:
                for line_num, line in enumerate(f, 1):
                    # Check for errors
                    error_matched = False
                    for pattern in self.error_patterns:
                        if re.search(pattern, line):
                            # Check if this error should be ignored
                            if not self.should_ignore_error(line):
                                errors.append((line_num, line.strip()))
                            else:
                                ignored_errors.append((line_num, line.strip()))
                            error_matched = True
                            break
                    
                    if error_matched:
                        continue
                    
                    # Check for warnings
                    warning_matched = False
                    for pattern in self.warning_patterns:
                        if re.search(pattern, line):
                            # Check if this warning should be ignored
                            if not self.should_ignore_warning(line):
                                warnings.append((line_num, line.strip()))
                            else:
                                ignored_warnings.append((line_num, line.strip()))
                            warning_matched = True
                            break
                    
                    if warning_matched:
                        continue
                    
                    # Check for info
                    for pattern in self.info_patterns:
                        if re.search(pattern, line):
                            # Check if this info should be ignored
                            if not self.should_ignore_info(line):
                                infos.append((line_num, line.strip()))
                            else:
                                ignored_infos.append((line_num, line.strip()))
                            break
        
        except Exception as e:
            return {
                'status': 'error',
                'errors': [(0, f"Failed to read file: {str(e)}")],
                'warnings': [],
                'infos': [],
                'ignored_errors': [],
                'ignored_warnings': [],
                'ignored_infos': [],
                'report_sections': [],
                'exists': True
            }
        
        # Determine overall status
        if errors:
            status = 'error'
        elif warnings:
            status = 'warning'
        elif infos:
            status = 'info'
        else:
            status = 'success'
        
        return {
            'status': status,
            'errors': errors,
            'warnings': warnings,
            'infos': infos,
            'ignored_errors': ignored_errors,
            'ignored_warnings': ignored_warnings,
            'ignored_infos': ignored_infos,
            'report_sections': report_sections,
            'exists': True
        }


class BlockRunAnalyzer:
    """Analyzes all log files for a block run."""
    
    def __init__(self, block_dir, block_name, global_pattern_config=None):
        self.block_dir = block_dir
        self.block_name = block_name
        self.run_location = os.path.dirname(block_dir)  # parent dir of block_dir

        # Load patterns (block-specific overrides global)
        pattern_config = self.load_pattern_config(global_pattern_config)
        self.file_patterns = pattern_config.get('files', [])  # list of glob pattern entries
        self.analyzer = LogAnalyzer(pattern_config)
    
    def load_pattern_config(self, global_pattern_config=None):
        """Load pattern configuration from YAML files."""
        # Start with global config
        if global_pattern_config:
            pattern_config = {
                'error_patterns': global_pattern_config.get('error_patterns', []).copy(),
                'warning_patterns': global_pattern_config.get('warning_patterns', []).copy(),
                'info_patterns': global_pattern_config.get('info_patterns', []).copy(),
                'ignore_error': global_pattern_config.get('ignore_error', []).copy(),
                'ignore_warning': global_pattern_config.get('ignore_warning', []).copy(),
                'ignore_info': global_pattern_config.get('ignore_info', []).copy(),
                'report_patterns': global_pattern_config.get('report_patterns', []).copy(),
                'files': list(global_pattern_config.get('files', []))
            }
        else:
            pattern_config = {
                'error_patterns': [],
                'warning_patterns': [],
                'info_patterns': [],
                'ignore_error': [],
                'ignore_warning': [],
                'ignore_info': [],
                'report_patterns': [],
                'files': []
            }
        
        # Look for block-specific pattern file: <block_dir>/pattern.yaml
        block_pattern_file = os.path.join(self.block_dir, 'pattern.yaml')
        if os.path.isfile(block_pattern_file):
            try:
                with open(block_pattern_file, 'r') as f:
                    block_config = yaml.safe_load(f) or {}
                
                print(f"  Loaded block-specific patterns from {block_pattern_file}")
                
                # Merge or override based on configuration
                # If block config has patterns, they extend (not replace) global patterns
                if 'error_patterns' in block_config:
                    print(f"    Adding {len(block_config['error_patterns'])} block error patterns")
                    pattern_config['error_patterns'].extend(block_config['error_patterns'])
                if 'warning_patterns' in block_config:
                    print(f"    Adding {len(block_config['warning_patterns'])} block warning patterns")
                    pattern_config['warning_patterns'].extend(block_config['warning_patterns'])
                if 'info_patterns' in block_config:
                    print(f"    Adding {len(block_config['info_patterns'])} block info patterns")
                    pattern_config['info_patterns'].extend(block_config['info_patterns'])
                if 'report_patterns' in block_config:
                    print(f"    Adding {len(block_config['report_patterns'])} block report patterns")
                    pattern_config['report_patterns'].extend(block_config['report_patterns'])
                if 'ignore_error' in block_config:
                    print(f"    Adding {len(block_config['ignore_error'])} block ignore_error patterns")
                    pattern_config['ignore_error'].extend(block_config['ignore_error'])
                if 'ignore_warning' in block_config:
                    print(f"    Adding {len(block_config['ignore_warning'])} block ignore_warning patterns")
                    pattern_config['ignore_warning'].extend(block_config['ignore_warning'])
                if 'ignore_info' in block_config:
                    print(f"    Adding {len(block_config['ignore_info'])} block ignore_info patterns")
                    pattern_config['ignore_info'].extend(block_config['ignore_info'])
                
            except Exception as e:
                print(f"  Warning: Failed to read pattern file {block_pattern_file}: {e}")
        else:
            print(f"  No block-specific pattern file found at {block_pattern_file}")
        
        print(f"  Final pattern counts - Errors: {len(pattern_config['error_patterns'])}, Warnings: {len(pattern_config['warning_patterns'])}, Infos: {len(pattern_config['info_patterns'])}")
        print(f"  Final ignore counts - ignore_error: {len(pattern_config['ignore_error'])}, ignore_warning: {len(pattern_config['ignore_warning'])}, ignore_info: {len(pattern_config['ignore_info'])}")
        
        return pattern_config
    
    def find_log_files(self):
        """Find all relevant log files for this block.

        If 'files' is defined in pattern.yaml, use those glob patterns.
        Each entry can be:
          - A plain string: "${block}/${block}_lec_.*.log"
          - A dict:         {pattern: "${block}/${block}_lec_.*.log", label: lec_main}

        Variables expanded: ${block} and ${block_name} -> block_name.
        Patterns are relative to run_location (parent of block_dir).
        When a wildcard matches multiple files the most-recently modified is used.
        Falls back to hardcoded defaults when 'files' is not set.
        """
        log_files = {}

        if self.file_patterns:
            for entry in self.file_patterns:
                if isinstance(entry, dict):
                    pat = entry.get('pattern', '')
                    label = entry.get('label', None)
                else:
                    pat = str(entry)
                    label = None

                # Expand ${block} and ${block_name}
                expanded = pat.replace('${block_name}', self.block_name).replace('${block}', self.block_name)
                full_pattern = os.path.join(self.run_location, expanded)
                matches = sorted(glob.glob(full_pattern), key=os.path.getmtime, reverse=True)

                if not matches:
                    key = label if label else re.sub(r'[^\w]+', '_', os.path.splitext(os.path.basename(expanded))[0]).strip('_') or 'unknown'
                    log_files[key] = None
                    print(f"  files pattern '{pat}' -> no files found at {full_pattern}")
                else:
                    chosen = matches[0]
                    key = label if label else re.sub(r'[^\w]+', '_', os.path.splitext(os.path.basename(chosen))[0]).strip('_') or 'unknown'
                    # Avoid duplicate keys by appending a suffix
                    orig_key = key
                    idx = 1
                    while key in log_files:
                        key = f"{orig_key}_{idx}"
                        idx += 1
                    log_files[key] = chosen
                    if len(matches) > 1:
                        print(f"  files pattern '{pat}' -> {chosen} (most recent of {len(matches)})")
                    else:
                        print(f"  files pattern '{pat}' -> {chosen}")
            return log_files

        # ---- Hardcoded fallback defaults ----
        # 1. fv/<block_name>/rtl_to_fv_map.log
        fv_log = os.path.join(self.block_dir, 'fv', self.block_name, 'rtl_to_fv_map.log')
        log_files['rtl_to_fv_map'] = fv_log
        print(f"  Looking for rtl_to_fv_map.log at: {fv_log}")
        if os.path.isfile(fv_log):
            print(f" Found")
        else:
            print(f" Not found")

        # 2. <block_name>_lec_<timestamp>.log
        lec_logs = glob.glob(os.path.join(self.block_dir, f"{self.block_name}_lec_*.log"))
        if lec_logs:
            lec_logs.sort(key=os.path.getmtime, reverse=True)
            log_files['lec_main'] = lec_logs[0]
        else:
            log_files['lec_main'] = None

        # 3. <block_name>_auto_lec_<timestamp>-job-<job_id>.out
        job_out_logs = glob.glob(os.path.join(self.block_dir, f"{self.block_name}_auto_lec_*-job-*.out"))
        if job_out_logs:
            job_out_logs.sort(key=os.path.getmtime, reverse=True)
            log_files['job_out'] = job_out_logs[0]
        else:
            log_files['job_out'] = None

        # 4. <block_name>_auto_lec_<timestamp>-job-<job_id>.err
        job_err_logs = glob.glob(os.path.join(self.block_dir, f"{self.block_name}_auto_lec_*-job-*.err"))
        if job_err_logs:
            job_err_logs.sort(key=os.path.getmtime, reverse=True)
            log_files['job_err'] = job_err_logs[0]
        else:
            log_files['job_err'] = None

        return log_files
    
    def analyze(self):
        """Analyze all log files and return results."""
        log_files = self.find_log_files()
        results = {}
        
        for log_type, log_path in log_files.items():
            if log_path:
                print(f"  Analyzing {log_type}: {log_path}")
                results[log_type] = self.analyzer.analyze_file(log_path)
                results[log_type]['path'] = log_path
                print(f"    Status: {results[log_type]['status']}, Errors: {len(results[log_type]['errors'])}, Warnings: {len(results[log_type]['warnings'])}, Infos: {len(results[log_type]['infos'])}")
            else:
                results[log_type] = {
                    'status': 'missing',
                    'errors': [],
                    'warnings': [],
                    'infos': [],
                    'exists': False,
                    'path': None
                }
        
        # Determine overall block status (worst status wins)
        statuses = [r['status'] for r in results.values()]
        if 'error' in statuses:
            overall_status = 'error'
        elif 'warning' in statuses:
            overall_status = 'warning'
        elif 'info' in statuses:
            overall_status = 'info'
        elif 'missing' in statuses:
            overall_status = 'missing'
        else:
            overall_status = 'success'
        
        return {
            'block_name': self.block_name,
            'block_dir': self.block_dir,
            'overall_status': overall_status,
            'log_results': results
        }



def _parse_files_field(raw):
    """Normalise the 'files' field: accept list or space-separated string."""
    if not raw:
        return []
    if isinstance(raw, str):
        return raw.split()
    return list(raw)


def _merge_pattern_config(base, override):
    """Merge block-specific config into a copy of the global base.

    All keys APPEND to global (never replace), except report_patterns where
    an entry with the same 'name' replaces the global entry.
    """
    merged = copy.deepcopy(base)

    # files — append block files to global files
    if 'files' in override:
        merged.setdefault('files', [])
        merged['files'].extend(_parse_files_field(override['files']))

    # ignore lists — append
    for key in ('ignore_error', 'ignore_warning', 'ignore_info'):
        if key in override:
            merged.setdefault(key, [])
            merged[key].extend(override[key])

    # error/warning/info patterns — append
    for key in ('error_patterns', 'warning_patterns', 'info_patterns'):
        if key in override:
            merged.setdefault(key, [])
            merged[key].extend(override[key])

    # report_patterns — append new names, replace existing by name
    if 'report_patterns' in override:
        # Separate real patterns from csv directives in the override as well
        ovr_csv = [rp for rp in override['report_patterns'] if isinstance(rp, dict) and 'csv' in rp]
        ovr_rp  = [rp for rp in override['report_patterns'] if not (isinstance(rp, dict) and 'csv' in rp)]
        merged.setdefault('report_patterns', [])
        base_index = {rp.get('name'): i for i, rp in enumerate(merged['report_patterns'])}
        for rp in ovr_rp:
            name = rp.get('name')
            if name and name in base_index:
                merged['report_patterns'][base_index[name]] = rp  # replace
            else:
                merged['report_patterns'].append(rp)              # append new
        # Merge csv directives: append (keyed by 'csv' filename, replace if same name)
        merged.setdefault('csv_directives', [])
        existing_csv = {d.get('csv') for d in merged['csv_directives']}
        for d in ovr_csv:
            if d.get('csv') in existing_csv:
                merged['csv_directives'] = [x for x in merged['csv_directives'] if x.get('csv') != d.get('csv')]
            merged['csv_directives'].append(d)
        # Rebuild ordered_pattern_names
        merged['_ordered_pattern_names'] = [rp.get('name', f'pattern{i+1}')
                                             for i, rp in enumerate(merged['report_patterns'])]

    return merged


def load_global_pattern_config(pattern_file):
    """Load global pattern configuration from YAML file."""
    if not pattern_file or not os.path.isfile(pattern_file):
        return None
    
    try:
        with open(pattern_file, 'r') as f:
            config = yaml.safe_load(f) or {}

        # Separate real report_patterns from csv directives
        raw_rp = config.get('report_patterns', [])
        real_rp = [rp for rp in raw_rp if not (isinstance(rp, dict) and 'csv' in rp)]
        csv_dirs = [rp for rp in raw_rp if isinstance(rp, dict) and 'csv' in rp]

        pattern_config = {
            'error_patterns': config.get('error_patterns', []),
            'warning_patterns': config.get('warning_patterns', []),
            'info_patterns': config.get('info_patterns', []),
            'ignore_error': config.get('ignore_error', []),
            'ignore_warning': config.get('ignore_warning', []),
            'ignore_info': config.get('ignore_info', []),
            'report_patterns': real_rp,
            'csv_directives': csv_dirs,
            # ordered list of pattern names (1-indexed as pattern1, pattern2…)
            '_ordered_pattern_names': [rp.get('name', f'pattern{i+1}')
                                       for i, rp in enumerate(real_rp)],
            'files': _parse_files_field(config.get('files', [])),
            '_pattern_file_dir': os.path.dirname(os.path.abspath(pattern_file))
        }

        print(f"Loaded global pattern configuration from {pattern_file}")
        print(f"  Error patterns: {len(pattern_config['error_patterns'])}")
        print(f"  Warning patterns: {len(pattern_config['warning_patterns'])}")
        print(f"  Info patterns: {len(pattern_config['info_patterns'])}")
        print(f"  Report patterns: {len(pattern_config['report_patterns'])}")
        print(f"  CSV directives: {len(pattern_config['csv_directives'])}")
        print(f"  Ignore error patterns: {len(pattern_config['ignore_error'])}")
        print(f"  Ignore warning patterns: {len(pattern_config['ignore_warning'])}")
        print(f"  Ignore info patterns: {len(pattern_config['ignore_info'])}")
        
        return pattern_config
    except Exception as e:
        print(f"Warning: Failed to load global pattern file {pattern_file}: {e}")
        return None


def _find_block_paths(block_name, chip_name, hierarchy):
    """Return a list of parent-path strings for every place *block_name* appears
    in the chip hierarchy.

    Example: 'upaw' lives directly under 'cf100' → ['skylp.cf100'].
    'ldm' lives under 'cf700.lduw' → ['skylp.cf700.lduw'].
    'llpw' appears under cf300/cf301/cf302/cf303 → four paths.
    """
    paths = []

    def _search(node_children, prefix):
        for name, children in (node_children or {}).items():
            if name == block_name:
                paths.append(prefix)
            _search(children or {}, f"{prefix}.{name}")

    for cf_name, cf_children in hierarchy.items():
        if cf_name == block_name:
            paths.append(chip_name)
        _search(cf_children or {}, f"{chip_name}.{cf_name}")

    return paths


def _walk_section_path(section, rest_parts):
    """Resolve *rest_parts* (a dot-split tail) against a report-section dict.

    *rest_parts* examples:
      ['group1']                          – main capture group
      ['pattern1', 'group1']             – sub-pattern group
      ['pattern3', 'pattern1', 'group1'] – nested sub-pattern group

    Returns a string, empty string on any miss.
    """
    if not rest_parts:
        return ''
    first = rest_parts[0]
    remaining = rest_parts[1:]

    if first.startswith('group'):
        val = section.get('groups', {}).get(first)
        return str(val) if val is not None else ''

    if first.startswith('pattern'):
        node = section.get('sub_patterns', {}).get(first)
        if node is None:
            return ''
        # Walk remaining parts via PatternNode attribute access.
        # PatternNode.__getattr__ returns '' for missing groupN and an empty
        # PatternNode for missing patternN, so traversal never raises.
        for part in remaining:
            node = getattr(node, part, None)
            if node is None:
                return ''
        # At the end we expect a scalar (groupN value), not a PatternNode.
        return '' if node is None else str(node)

    return ''


# Regex to parse   info.yaml(field_name)   expressions in CSV data directives.
_INFO_YAML_RE = re.compile(r'^info\.yaml\(([^)]+)\)$')


def _read_info_yaml_field(block_dir, field_name):
    """Read *field_name* from ``info.yaml`` located in *block_dir*.

    Returns
    -------
    str
        The field value as a string, or one of the sentinel strings:
        * ``'info.yaml not available'``   – file does not exist
        * ``'info.yaml no data available'`` – file exists but key is absent / empty
    """
    if not block_dir:
        return 'info.yaml not available'
    info_path = os.path.join(block_dir, 'info.yaml')
    if not os.path.isfile(info_path):
        return 'info.yaml not available'
    try:
        with open(info_path, 'r') as fh:
            data = yaml.safe_load(fh) or {}
    except Exception:
        return 'info.yaml not available'
    val = data.get(field_name)
    if val is None or val == '':
        return 'info.yaml no data available'
    return str(val)


def _resolve_csv_expr(expr, block_name, hier_path, all_report_sections, ordered_names,
                      block_dir=None):
    """Resolve one data-column expression from a csv directive to a string.

    Supported expression formats
    ----------------------------
    block_name
        The block being analysed.
    hier_name
        Hierarchical dot-path from the chip schema (pre-computed per row).
    info.yaml(field_name)
        Read *field_name* from ``<block_dir>/info.yaml``.
        Returns ``'info.yaml not available'`` when the file is missing and
        ``'info.yaml no data available'`` when the key is absent or empty.
    <pattern_name>.<dotpath>          ← preferred / name-based
        Look up sections whose ``name`` field equals *pattern_name*, then
        resolve *dotpath* against that section.  Examples::

            status.group1
            status.pattern1.group1
            Overview.pattern3.pattern1.group1

    patternN.<dotpath>                ← positional / legacy form
        *N* is the 1-based index of the report_pattern in the ordered list.
        Equivalent to using the name directly.  Kept for backward compat.
    """
    expr = expr.strip()
    if expr == 'block_name':
        return block_name
    if expr == 'hier_name':
        return hier_path

    # info.yaml(field_name) — read from the block's info.yaml file
    m_info = _INFO_YAML_RE.match(expr)
    if m_info:
        return _read_info_yaml_field(block_dir, m_info.group(1).strip())

    parts = expr.split('.')
    if not parts:
        return ''

    head = parts[0]
    rest_parts = parts[1:]

    # ── Resolve target pattern name ──────────────────────────────────────────
    # Name-based: head is a known report_pattern name (e.g. 'status', 'Overview')
    if head in ordered_names:
        target_name = head
    # Positional: head is 'patternN'
    elif head.startswith('pattern'):
        try:
            pidx = int(head[len('pattern'):]) - 1   # 0-based
        except ValueError:
            return ''
        if pidx < 0 or pidx >= len(ordered_names):
            return ''
        target_name = ordered_names[pidx]
    else:
        return ''

    if not rest_parts:
        return ''

    # ── Find the worst-ranked occurrence of the target pattern ───────────────
    _CRANK = {'red': 4, 'orange': 3, 'yellow': 2, 'green': 1}
    best_rank = -1
    best_value = ''

    for section in all_report_sections:
        # Strip the " (Occurrence N)" suffix when comparing names
        sname = section.get('name', '').split(' (Occurrence')[0]
        if sname != target_name:
            continue
        rank = _CRANK.get(str(section.get('title_color', '')).lower(), 0)
        if rank < best_rank:
            continue

        val = _walk_section_path(section, rest_parts)
        best_rank = rank
        best_value = val

    return best_value


def load_chip_hierarchy(schema_file):
    """Load the chip schema YAML and return ``(chip_name, hierarchy_dict)``.

    *hierarchy_dict* maps each CF cluster name to a nested dict of its
    instances::

        {
          'cf000': {'scpw': {'cmrt': {}}},
          'cf001': {'appw': {'cmu': {}, 'cmv': {}, ...}},
          ...
        }

    Returns ``(None, None)`` when the file cannot be read or the expected
    chip structure is not found.
    """
    if not schema_file or not os.path.isfile(schema_file):
        print(f"Note: chip schema not found at {schema_file!r} – hierarchy view disabled.")
        return None, None

    try:
        with open(schema_file, 'r') as f:
            raw = yaml.safe_load(f) or {}
    except Exception as exc:
        print(f"Warning: cannot load chip schema {schema_file!r}: {exc}")
        return None, None

    # The chip-level key is the first dict key whose value contains cf* sub-entries.
    chip_name = None
    chip_data = {}
    for key, val in raw.items():
        if isinstance(val, dict) and any(k.startswith('cf') for k in (val or {})):
            chip_name = key
            chip_data = val or {}
            break

    if chip_name is None:
        print(f"Warning: no chip-level entry (with cf* children) found in {schema_file!r}")
        return None, None

    def _extract(node):
        """Recursively extract named children from a YAML node's 'instances' map."""
        if not isinstance(node, dict):
            return {}
        instances = node.get('instances') or {}
        if not isinstance(instances, dict):
            return {}
        return {name: _extract(child or {}) for name, child in instances.items()}

    hierarchy = {
        cf_name: _extract(cf_data or {})
        for cf_name, cf_data in chip_data.items()
    }

    print(f"Loaded chip hierarchy from {schema_file!r}: "
          f"{len(hierarchy)} CF clusters under '{chip_name}'")
    return chip_name, hierarchy


def find_all_blocks(run_location):
    """Find all block directories in the run location."""
    # Directories that are never block runs regardless of content
    SKIP_DIRS = {'gitRepo', 'logs', 'old_runs'}

    blocks = []

    # Recognised signatures:
    #   fv/              - FEV / rtl_syn run
    #   *_lec_*.log      - LEC log (fev run)
    #   makefile symlink - lint run (lintFire.py)
    #   *.pending        - lint dry-run placeholder (lintFire.py --nofire)
    for item in os.listdir(run_location):
        if item in SKIP_DIRS:
            continue
        item_path = os.path.join(run_location, item)
        if os.path.isdir(item_path):
            has_fv       = os.path.isdir(os.path.join(item_path, 'fv'))
            has_lec_log  = bool(glob.glob(os.path.join(item_path, '*_lec_*.log')))
            has_makefile = os.path.islink(os.path.join(item_path, 'makefile'))
            has_pending  = bool(glob.glob(os.path.join(item_path, '*.pending')))

            if has_fv or has_lec_log or has_makefile or has_pending:
                blocks.append((item, item_path))

    return blocks


