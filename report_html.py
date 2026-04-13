#!/usr/bin/env python3
"""
report_html.py

HTML / CSV report generation layer for FEV block run reports.

Contains:
  - HTMLReportGenerator — full hierarchical HTML report
  - CSVReportGenerator  — CSV files + status.html (with comments & user-status)
  - Comment server helpers: _make_comment_handler, serve_mode,
    comments_file_path, load_comments
"""

import os
import re
import json
import threading
import http.server
import urllib.parse
from datetime import datetime
from pathlib import Path

from report_data import _title_to_html, _find_block_paths, _resolve_csv_expr

class HTMLReportGenerator:
    """Generates HTML report from analysis results."""
    
    STATUS_COLORS = {
        'error':   '#ff4444',   # Red
        'warning': '#ff9933',   # Orange
        'info':    '#ffdd44',   # Yellow
        'success': '#44cc44',   # Green
        'missing': '#cccccc',   # Gray
        'nodata':  '#aaaaaa',   # Light gray – no run data available
    }

    STATUS_LABELS = {
        'error':   'ERROR',
        'warning': 'WARNING',
        'info':    'INFO',
        'success': 'PASS',
        'missing': 'MISSING',
        'nodata':  'No Data',
    }

    # Rank used when aggregating child statuses up the hierarchy (highest = worst)
    _STATUS_RANK = {'error': 4, 'warning': 3, 'info': 2, 'missing': 1, 'success': 0, 'nodata': -1}
    
    def __init__(self, output_file):
        self.output_file = output_file
    
    def generate(self, block_results, chip_hierarchy=None):
        """Generate HTML report from block analysis results.

        When *chip_hierarchy* is supplied (a tuple returned by
        ``load_chip_hierarchy``), the report is rendered as a collapsible
        chip-level tree rather than a flat summary table.
        """
        html = self._generate_header()
        html += '<div style="background:#fff8e1;border:1px solid #ffecb3;padding:8px 12px;border-radius:4px;margin:10px 0;">If clicking "Reports" or "Open Full Log" does nothing, open this HTML in a web browser (Chrome/Firefox). Some previewers block scripts and inline events.</div>'

        # Prefer hierarchical chip view when a schema was loaded.
        _chip_name = chip_hierarchy[0] if chip_hierarchy else None
        _hierarchy = chip_hierarchy[1] if chip_hierarchy and len(chip_hierarchy) > 1 else None
        if _chip_name and _hierarchy:
            html += self._generate_hierarchical_view(block_results, _chip_name, _hierarchy)
        else:
            # (Per-section filter bars are embedded in each pattern section below)
            html += self._generate_summary_table(block_results)
            html += self._generate_detailed_sections(block_results)

        html += self._generate_footer()
        
        with open(self.output_file, 'w') as f:
            f.write(html)
        
        print(f"HTML report generated: {self.output_file}")
        
        # Also generate standalone log viewer HTML files
        output_dir = os.path.join(os.path.dirname(self.output_file), 'block_html')
        os.makedirs(output_dir, exist_ok=True)
        for result in block_results:
            block_name = result['block_name']
            for log_type, log_result in result['log_results'].items():
                if log_result.get('exists'):
                    viewer_file = self._generate_log_viewer_html(log_result, block_name, log_type, output_dir)
                    if viewer_file:
                        print(f"Log viewer generated: {viewer_file}")
    
    def _generate_header(self):
        """Generate HTML header with CSS."""
        return f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>FEV Block Run Report</title>
    <style>
        body {{
            font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif;
            margin: 20px;
            background-color: #f5f5f5;
        }}
        h1 {{
            color: #333;
            border-bottom: 3px solid #007acc;
            padding-bottom: 10px;
        }}
        h2 {{
            color: #555;
            margin-top: 30px;
            border-bottom: 2px solid #ccc;
            padding-bottom: 5px;
        }}
        h3 {{
            color: #666;
            margin-top: 20px;
        }}
        .timestamp {{
            color: #888;
            font-size: 0.9em;
        }}
        table {{
            border-collapse: collapse;
            width: 100%;
            background-color: white;
            box-shadow: 0 2px 4px rgba(0,0,0,0.1);
            margin-bottom: 20px;
        }}
        th {{
            background-color: #007acc;
            color: white;
            padding: 12px;
            text-align: left;
            font-weight: bold;
        }}
        td {{
            padding: 10px;
            border-bottom: 1px solid #ddd;
        }}
        tr:hover {{
            background-color: #f9f9f9;
        }}
        .status-cell {{
            font-weight: bold;
            text-align: center;
            padding: 8px;
            border-radius: 4px;
        }}
        .block-section {{
            background-color: white;
            padding: 20px;
            margin-bottom: 20px;
            border-radius: 5px;
            box-shadow: 0 2px 4px rgba(0,0,0,0.1);
        }}
        .log-entry {{
            font-family: 'Courier New', monospace;
            font-size: 0.85em;
            padding: 5px;
            margin: 2px 0;
            background-color: #f9f9f9;
            border-left: 3px solid #ccc;
            padding-left: 10px;
        }}
        .log-entry.error {{
            border-left-color: #ff4444;
            background-color: #ffeeee;
        }}
        .log-entry.warning {{
            border-left-color: #ff9933;
            background-color: #fff8ee;
        }}
        .log-entry.info {{
            border-left-color: #ffdd44;
            background-color: #fffcee;
        }}
        .line-number {{
            color: #888;
            margin-right: 10px;
        }}
        .collapsible {{
            cursor: pointer;
            padding: 10px;
            background-color: #f0f0f0;
            border: none;
            text-align: left;
            width: 100%;
            font-weight: bold;
            margin-top: 10px;
            border: 1px solid #ccc;
            border-radius: 3px;
        }}
        .collapsible:hover {{
            background-color: #e0e0e0;
        }}
        .collapsible:active {{
            background-color: #d0d0d0;
        }}
        /* Remove default marker from block-level summary bars */
        details > summary {{
            list-style: none;
        }}
        details > summary::-webkit-details-marker {{
            display: none;
        }}
        .content {{
            display: none;
            padding: 10px;
            background-color: #fafafa;
            border: 1px solid #ddd;
        }}
        .tabs {{
            margin: 10px 0 20px;
        }}
        .tab-buttons {{
            display: flex;
            flex-wrap: wrap;
            gap: 6px;
            margin-bottom: 8px;
        }}
        .tab-button {{
            background-color: #eee;
            border: 1px solid #ccc;
            border-radius: 4px;
            padding: 6px 10px;
            cursor: pointer;
        }}
        .tab-button.active {{
            background-color: #007acc;
            color: white;
            border-color: #007acc;
        }}
        .tab-close {{
            margin-left: 8px;
            color: inherit;
            text-decoration: none;
            cursor: pointer;
        }}
        .tab-panels .tab-panel {{
            display: none;
            background-color: white;
            border: 1px solid #ddd;
            border-radius: 4px;
            padding: 10px;
            font-family: 'Courier New', monospace;
            font-size: 0.85em;
            max-height: 600px;
            overflow: auto;
            white-space: pre-wrap;
            word-wrap: break-word;
        }}
        .tab-panels .tab-panel.active {{
            display: block;
        }}
        .highlight {{
            background-color: #fff59d; /* light yellow */
            border-left: 3px solid #ffdd44;
            padding-left: 6px;
        }}
        .log-viewer {{
            background-color: white;
            border: 1px solid #ddd;
            border-radius: 4px;
            padding: 10px;
            font-family: 'Courier New', monospace;
            font-size: 0.85em;
            max-height: 500px;
            overflow: auto;
            white-space: pre-wrap;
            word-wrap: break-word;
            margin-top: 10px;
        }}
        .log-line {{
            padding: 2px 8px;
            margin: 0;
            display: block;
            line-height: 1.5;
        }}
        .log-line:target {{
            background-color: #ffff99 !important;
            border-left: 3px solid #ffdd44;
            padding-left: 5px;
        }}
        .log-line-number {{
            color: #888;
            margin-right: 10px;
            user-select: none;
            display: inline-block;
            min-width: 50px;
            text-align: right;
        }}
        .log-line-content {{
            display: inline;
        }}
        .log-line a {{
            color: #007acc;
            text-decoration: none;
            cursor: pointer;
            font-weight: bold;
        }}
        .log-line a:hover {{
            text-decoration: underline;
        }}
        /* Pure CSS color filter — checkbox + label siblings */
        input.cf {{ display: none; }}
        label.cf-label {{ display:inline-flex;align-items:center;gap:4px;cursor:pointer;padding:4px 10px;margin:2px;border:2px solid #ccc;border-radius:4px;background:#f5f5f5;user-select:none;font-size:0.9em; }}
        input.cf:checked + label.cf-label {{ border-color:#007acc; background:#e3f2fd; }}
        input.cf[data-color="green"]:not(:checked) ~ .occ-wrap[data-color="green"] {{ display: none !important; }}
        input.cf[data-color="orange"]:not(:checked) ~ .occ-wrap[data-color="orange"] {{ display: none !important; }}
        input.cf[data-color="red"]:not(:checked) ~ .occ-wrap[data-color="red"] {{ display: none !important; }}
        input.cf[data-color="default"]:not(:checked) ~ .occ-wrap[data-color="default"] {{ display: none !important; }}
    </style>
    <script>
        function toggleContent(id) {{
            console.log('toggleContent called with id:', id);
            var content = document.getElementById(id);
            if (!content) {{
                console.error('Element not found:', id);
                alert('Error: Could not find element with ID: ' + id);
                return;
            }}
            var current = window.getComputedStyle(content).display;
            console.log('Current display:', current);
            if (current === "none") {{
                content.style.display = "block";
                console.log('Set to block');
            }} else {{
                content.style.display = "none";
                console.log('Set to none');
            }}
        }}

        function escapeHtml(text) {{
            if (!text) return '';
            return text
                .replace(/&/g, '&amp;')
                .replace(/</g, '&lt;')
                .replace(/>/g, '&gt;');
        }}

        // Open a log in a new tab and highlight the match region
        function openLogTab(tabId, title, contentId, startPos, endPos) {{
            var buttons = document.getElementById('tabButtons');
            var panels = document.getElementById('tabPanels');
            if (!buttons || !panels) return;

            var panel = document.getElementById(tabId);
            if (!panel) {{
                var btn = document.createElement('button');
                btn.className = 'tab-button';
                btn.id = 'btn_' + tabId;
                btn.innerHTML = escapeHtml(title) + ' <span class="tab-close" onclick="closeTab(\'' + tabId + '\')">✕</span>';
                btn.onclick = function() {{ selectTab(tabId); }};
                buttons.appendChild(btn);

                panel = document.createElement('div');
                panel.className = 'tab-panel';
                panel.id = tabId;
                panels.appendChild(panel);
            }}

            var rawContentElement = document.getElementById(contentId);
            var raw = rawContentElement ? (rawContentElement.textContent || rawContentElement.innerText || '') : '';

            if (startPos < 0 || endPos > raw.length || startPos >= endPos) {{
                panel.innerHTML = escapeHtml(raw);
            }} else {{
                var before = raw.slice(0, startPos);
                var match = raw.slice(startPos, endPos);
                var after = raw.slice(endPos);
                var html = escapeHtml(before) + '<span class="highlight">' + escapeHtml(match) + '</span>' + escapeHtml(after);
                panel.innerHTML = html;
            }}

            selectTab(tabId);

            setTimeout(function() {{
                var rect = panel.querySelector('.highlight');
                if (rect) {{
                    var top = rect.offsetTop - 16;
                    panel.scrollTo({{ top: top, behavior: 'smooth' }});
                }}
            }}, 50);
        }}

        function selectTab(tabId) {{
            var buttons = document.querySelectorAll('.tab-button');
            buttons.forEach(function(b) {{ b.classList.remove('active'); }});
            var panels = document.querySelectorAll('.tab-panel');
            panels.forEach(function(p) {{ p.classList.remove('active'); }});
            var btn = document.getElementById('btn_' + tabId);
            if (btn) btn.classList.add('active');
            var panel = document.getElementById(tabId);
            if (panel) panel.classList.add('active');
        }}

        function closeTab(tabId) {{
            var btn = document.getElementById('btn_' + tabId);
            var panel = document.getElementById(tabId);
            if (btn) btn.remove();
            if (panel) panel.remove();
        }}

        // (Color filtering is pure CSS — no JS needed)
    </script>
</head>
<body>
    <a id="top"></a>
    <h1>FEV Block Run Report</h1>
    <p class="timestamp">Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}</p>
    <div id="logTabs" class="tabs">
        <div id="tabButtons" class="tab-buttons"></div>
        <div id="tabPanels" class="tab-panels"></div>
    </div>
"""
    
    def _generate_summary_table(self, block_results):
        """Generate summary table of all blocks."""
        html = '<h2>Summary</h2>\n'
        html += '<p style="font-size:0.85em;color:#666;margin-bottom:6px;">Counts shown as <strong>remaining / ignored / total</strong> &mdash; remaining are active issues, ignored were suppressed by ignore patterns, total = remaining + ignored.</p>\n'
        html += '<table>\n'
        html += '<tr><th>Block Name</th><th>Status</th><th>Errors<br><span style="font-weight:normal;font-size:0.8em;">remaining/ignored/total</span></th><th>Warnings<br><span style="font-weight:normal;font-size:0.8em;">remaining/ignored/total</span></th><th>Infos<br><span style="font-weight:normal;font-size:0.8em;">remaining/ignored/total</span></th><th>Block Directory</th></tr>\n'
        
        for result in block_results:
            block_name = result['block_name']
            status = result['overall_status']
            block_dir = result['block_dir']
            
            # remaining = not ignored, ignored = suppressed by ignore patterns, total = remaining + ignored
            rem_errors   = sum(len(r['errors'])           for r in result['log_results'].values())
            ign_errors   = sum(len(r.get('ignored_errors',   [])) for r in result['log_results'].values())
            tot_errors   = rem_errors + ign_errors
            rem_warnings = sum(len(r['warnings'])         for r in result['log_results'].values())
            ign_warnings = sum(len(r.get('ignored_warnings', [])) for r in result['log_results'].values())
            tot_warnings = rem_warnings + ign_warnings
            rem_infos    = sum(len(r['infos'])             for r in result['log_results'].values())
            ign_infos    = sum(len(r.get('ignored_infos',    [])) for r in result['log_results'].values())
            tot_infos    = rem_infos + ign_infos

            def fmt(rem, ign, tot):
                rem_style = 'color:#cc0000;font-weight:bold;' if rem > 0 else ''
                ign_style = 'color:#888;'
                tot_style = 'color:#333;'
                return (f'<span style="{rem_style}" title="remaining (active)">{rem}</span>'
                        f' / <span style="{ign_style}" title="ignored by pattern">{ign}</span>'
                        f' / <span style="{tot_style}" title="total matched">{tot}</span>')

            # ── Status color from the 'status' report_pattern ──────────────────
            # Collect title_color values from every occurrence whose name == 'status'
            _COLOR_RANK = {'red': 3, 'orange': 2, 'yellow': 1, 'green': 0}
            _COLOR_BG   = {'red': '#cc2222', 'orange': '#ff9933', 'yellow': '#ccaa00', 'green': '#44aa44'}
            _COLOR_LABEL = {'red': 'FAIL', 'orange': 'WARN', 'yellow': 'INFO', 'green': 'PASS'}

            status_colors_found = []
            for log_result in result['log_results'].values():
                for sec in log_result.get('report_sections', []):
                    if sec.get('name', '').lower() == 'status' and sec.get('title_color'):
                        status_colors_found.append(sec['title_color'].lower())

            if status_colors_found:
                # Worst color wins (highest rank)
                winning = max(status_colors_found, key=lambda c: _COLOR_RANK.get(c, -1))
                color = _COLOR_BG.get(winning, winning)   # use hex bg if known, else raw CSS
                label = _COLOR_LABEL.get(winning, winning.upper())
            else:
                # Fallback to overall_status when no 'status' pattern found
                color = self.STATUS_COLORS[status]
                label = self.STATUS_LABELS[status]
            # ───────────────────────────────────────────────────────────────────

            html += f'<tr id="summary_{block_name}">\n'
            html += f'  <td><a href="#block_{block_name}">{block_name}</a></td>\n'
            html += f'  <td class="status-cell" style="background-color: {color}; color: white;">{label}</td>\n'
            html += f'  <td style="text-align: center;">{fmt(rem_errors, ign_errors, tot_errors)}</td>\n'
            html += f'  <td style="text-align: center;">{fmt(rem_warnings, ign_warnings, tot_warnings)}</td>\n'
            html += f'  <td style="text-align: center;">{fmt(rem_infos, ign_infos, tot_infos)}</td>\n'
            html += f'  <td><code>{block_dir}</code></td>\n'
            html += f'</tr>\n'
        
        html += '</table>\n'
        return html
    
    def _generate_detailed_sections(self, block_results):
        """Generate detailed sections for each block."""
        html = '<h2>Detailed Results</h2>\n'
        
        for result in block_results:
            html += self._generate_block_section(result)
        
        return html
    
    def _generate_block_section(self, result):
        """Generate a collapsible section for a single block.

        The <summary> bar is always visible and shows:
          block name | status badge | E/W/I counts | ↑ Summary link
        Clicking expands the full log detail (log file sections, errors, etc.).
        """
        block_name = result['block_name']
        status = result['overall_status']
        color = self.STATUS_COLORS[status]
        label = self.STATUS_LABELS[status]

        # Counts for the summary bar (remaining / active issues only)
        rem_errors   = sum(len(r['errors'])   for r in result['log_results'].values())
        rem_warnings = sum(len(r['warnings']) for r in result['log_results'].values())
        rem_infos    = sum(len(r['infos'])    for r in result['log_results'].values())

        count_parts = []
        if rem_errors:
            count_parts.append(
                f'<span style="background:#ffcdd2;color:#b71c1c;border-radius:10px;'
                f'padding:1px 8px;font-size:0.8em;font-weight:bold;">E:{rem_errors}</span>')
        if rem_warnings:
            count_parts.append(
                f'<span style="background:#ffe0b2;color:#e65100;border-radius:10px;'
                f'padding:1px 8px;font-size:0.8em;font-weight:bold;">W:{rem_warnings}</span>')
        if rem_infos:
            count_parts.append(
                f'<span style="background:#fff9c4;color:#f57f17;border-radius:10px;'
                f'padding:1px 8px;font-size:0.8em;font-weight:bold;">I:{rem_infos}</span>')
        counts_html = ' '.join(count_parts)

        # Collapsed by default — user clicks to see log details
        html  = f'<details id="block_{block_name}" style="margin-bottom:6px;border:1px solid #ddd;border-radius:5px;background:white;box-shadow:0 1px 3px rgba(0,0,0,0.07);">\n'
        html += f'  <summary style="cursor:pointer;padding:10px 16px;display:flex;align-items:center;gap:10px;user-select:none;background:#f8f9fa;border-radius:5px;list-style:none;">\n'
        html += f'    <span style="font-family:\'Courier New\',monospace;font-weight:bold;min-width:160px;">{block_name}</span>\n'
        html += f'    <span style="background:{color};color:white;padding:2px 10px;border-radius:10px;font-size:0.8em;font-weight:bold;">{label}</span>\n'
        if counts_html:
            html += f'    {counts_html}\n'
        html += f'    <a href="#summary_{block_name}" onclick="event.stopPropagation();" style="margin-left:auto;color:#007acc;text-decoration:none;font-size:0.82em;font-weight:bold;white-space:nowrap;">↑ Summary</a>\n'
        html += f'  </summary>\n'
        html += f'  <div style="padding:16px 20px;">\n'

        for log_type, log_result in result['log_results'].items():
            html += self._generate_log_section(log_type, log_result, block_name)

        html += f'    <p style="text-align:right;margin-top:15px;"><a href="#summary_{block_name}" style="color:#007acc;text-decoration:none;font-weight:bold;">↑ Back to Summary</a></p>\n'
        html += f'  </div>\n'
        html += '</details>\n'
        return html
    
    def _generate_log_section(self, log_type, log_result, block_name):
        """Generate section for a single log file."""
        html = f'<h4>{log_type}</h4>\n'
        
        if not log_result['exists']:
            html += '<p style="color: #999;">Log file not found</p>\n'
            return html
        
        html += f'<p><strong>File:</strong> <code>{log_result["path"]}</code></p>\n'
        
        var_base = f"{block_name}_{log_type}".replace(' ', '_')
        log_viewer_filename = f"block_html/{var_base}_log_viewer.html"
        
        # Link to open full log viewer in a named tab (so line number clicks reuse the same tab)
        html += f'<a href="{log_viewer_filename}" target="log_viewer_{var_base}" style="display: inline-block; padding: 10px 15px; background-color: #007acc; color: white; text-decoration: none; border-radius: 4px; margin: 8px 0; font-weight: bold;">📖 Open Full Log Viewer in New Tab</a>\n'
        html += f'<a href="file://{log_result["path"]}" target="_blank" style="display: inline-block; margin-left: 10px; padding: 10px 15px; background-color: #666; color: white; text-decoration: none; border-radius: 4px; margin: 8px 0; font-weight: bold;">📄 Open Raw File</a>\n'
        
        # Report sections - Group by pattern type with hierarchical structure
        # Only include sections that have a title_format (csv-only patterns are excluded)
        html_report_sections = [r for r in log_result.get('report_sections', []) if r.get('has_title_format')]
        if html_report_sections:
            # Group report sections by base pattern name
            grouped_reports = {}
            for report in html_report_sections:
                # Extract base name (remove occurrence suffix)
                base_name = report['name'].split(' (Occurrence')[0]
                if base_name not in grouped_reports:
                    grouped_reports[base_name] = []
                grouped_reports[base_name].append(report)
            
            # Count reports by color
            color_counts = {}
            for report in html_report_sections:
                color = report.get('title_color')
                if not color:
                    color = 'default'
                color_counts[color] = color_counts.get(color, 0) + 1
            
            # Define section ID before building badges (badges reference it in onclick)
            reports_section_id = f"{block_name}_{log_type}_reports"

            # Build color count display (display only)
            color_badges = []
            for color, count in sorted(color_counts.items()):
                if color and color != 'default':
                    color_badges.append(f'<span style="color: {color}; font-weight: bold; margin-left: 8px;">&#9679;{count}</span>')
                elif color == 'default':
                    color_badges.append(f'<span style="color: #666; font-weight: bold; margin-left: 8px;">&#9679;{count}</span>')
            color_display = ''.join(color_badges)

            # Create top-level Reports dropdown
            total_reports = len(html_report_sections)
            html += f'<details style="margin-top: 10px; border: 1px solid #ccc; border-radius: 3px; padding: 0;"><summary style="cursor: pointer; padding: 10px; background-color: #f0f0f0; font-weight: bold; user-select: none; display: flex; justify-content: space-between; align-items: center;"><span>Reports ({total_reports})</span><span>{color_display}</span></summary>\n'
            html += f'<div id="{reports_section_id}" style="padding: 10px; background-color: #fafafa; border-top: 1px solid #ddd;">\n'
            
            # For each pattern type, create a sub-dropdown
            for base_name, reports in grouped_reports.items():
                pattern_section_id = f"{block_name}_{log_type}_pattern_{base_name.replace(' ', '_').replace(':', '')}"

                # Build per-pattern color count badges (same style as the top-level Reports header)
                pat_color_counts = {}
                for r in reports:
                    c = r.get('title_color') or 'default'
                    pat_color_counts[c] = pat_color_counts.get(c, 0) + 1
                pat_badges = []
                for c, cnt in sorted(pat_color_counts.items()):
                    if c and c != 'default':
                        pat_badges.append(f'<span style="color: {c}; font-weight: bold; margin-left: 8px;">&#9679;{cnt}</span>')
                    else:
                        pat_badges.append(f'<span style="color: #666; font-weight: bold; margin-left: 8px;">&#9679;{cnt}</span>')
                pat_color_display = ''.join(pat_badges)

                html += f'  <details style="margin-left: 10px; margin-top: 8px; border: 1px solid #ddd; border-radius: 3px; padding: 0;"><summary style="cursor: pointer; padding: 10px; background-color: #f5f5f5; user-select: none; display: flex; justify-content: space-between; align-items: center;"><span>{base_name} ({len(reports)})</span><span>{pat_color_display}</span></summary>\n'
                html += f'  <div id="{pattern_section_id}" style="padding: 10px; background-color: white; border-top: 1px solid #ddd;">\n'

                # Pure CSS filter: checkbox + label siblings BEFORE the occ-wrap divs
                # "All" button uses tiny JS to check/uncheck all .cf inputs in the same container
                html += f'    <span style="display:inline-flex;align-items:center;gap:4px;margin-bottom:8px;"><strong style="font-size:0.85em;">Filter:</strong> <button class="cf-label" style="border:2px solid #007acc;background:#e3f2fd;cursor:pointer;padding:4px 10px;border-radius:4px;font-size:0.9em;font-weight:bold;" onclick="var c=this.parentElement.parentElement;var cbs=c.querySelectorAll(\'input.cf\');var allChecked=true;cbs.forEach(function(x){{if(!x.checked)allChecked=false;}});cbs.forEach(function(x){{x.checked=!allChecked;}});">All</button></span>\n'
                for c_color, c_cnt in sorted(pat_color_counts.items()):
                    dot_c = c_color if c_color and c_color != 'default' else '#aaa'
                    c_label = c_color if c_color and c_color != 'default' else 'default'
                    cb_id = f"{pattern_section_id}_cf_{c_color}"
                    html += f'    <input type="checkbox" class="cf" id="{cb_id}" data-color="{c_color}" checked>\n'
                    html += f'    <label class="cf-label" for="{cb_id}"><span style="color:{dot_c};font-size:1.2em;">&#9679;</span> {c_label} ({c_cnt})</label>\n'
                
                # For each occurrence
                for idx, report in enumerate(reports, 1):
                    occurrence_id = f"{pattern_section_id}_occ{idx}"
                    # Use custom_title from pattern groups if available, else default
                    if report.get('custom_title'):
                        occurrence_label = report['custom_title']
                    else:
                        occurrence_label = f"Occurrence {idx}" if len(reports) > 1 else "Data"
                    title_color = report.get('title_color')
                    start_pos = report.get('start_pos', 0)
                    end_pos = report.get('end_pos', 0)
                    line_num = report.get('line_number', 0)
                    
                    # Build title style with optional color
                    title_style = 'font-weight: bold;'
                    if title_color:
                        title_style += f' color: {title_color};'
                    
                    # Convert escape sequences and optional \| column separators to HTML.
                    # See _title_to_html() for the full set of supported sequences.
                    occurrence_label_html = _title_to_html(occurrence_label)
                    
                    # Create link to the log viewer HTML file with anchor to the specific line
                    log_viewer_filename = f"block_html/{var_base}_log_viewer.html"
                    line_anchor = f"log_{var_base}_line_{line_num}"
                    log_viewer_link = f"{log_viewer_filename}#{line_anchor}"
                    occ_color_attr = title_color if title_color else 'default'
                    html += f'    <div class="occ-wrap" data-color="{occ_color_attr}" style="margin-left: 10px; margin-top: 6px;">\n'
                    html += f'    <details style="border: 1px solid #eee; border-radius: 3px; padding: 0;"><summary style="cursor: pointer; padding: 8px; background-color: #f9f9f9; user-select: none; display: flex; justify-content: space-between; align-items: center;">\n'
                    html += f'      <span style="{title_style}">{occurrence_label_html}</span>\n'
                    html += f'      <a href="{log_viewer_link}" target="log_viewer_{var_base}" style="font-size: 0.85em; color: #007acc; text-decoration: none; font-weight: bold;">📄 Line {line_num}</a>\n'
                    html += f'    </summary>\n'
                    html += f'    <div id="{occurrence_id}" style="padding: 10px; background-color: white; border-top: 1px solid #eee;">\n'
                    html += '      <div style="font-family: monospace; white-space: pre-wrap; word-wrap: break-word; background-color: #f5f5f5; padding: 10px; border: 1px solid #ddd; border-radius: 4px; font-size: 0.9em;">\n'
                    html += '        ' + self._escape_html(report['content']).replace('\n', '\n        ')
                    html += '\n      </div>\n'
                    html += '    </div>\n'
                    html += '    </details>\n'
                    html += '    </div>\n'
                
                html += '  </div>\n'
                html += '  </details>\n'
            
            html += '</div>\n'
            html += '</details>\n'
        
        # Prepare log viewer link info (used for Errors/Warnings/Infos)
        log_viewer_filename = f"block_html/{var_base}_log_viewer.html"
        
        # Errors
        if log_result['errors']:
            section_id = f"{block_name}_{log_type}_errors"
            error_count = len(log_result['errors'])
            html += f'<details style="margin-top: 10px; border: 1px solid #ccc; border-radius: 3px; padding: 0;"><summary style="cursor: pointer; padding: 10px; background-color: #f0f0f0; font-weight: bold; user-select: none; display: flex; justify-content: space-between; align-items: center;"><span>Errors ({error_count})</span><span style="color: #ff4444; font-weight: bold;">●{error_count}</span></summary>\n'
            html += f'<div id="{section_id}" style="padding: 10px; background-color: #fafafa; border-top: 1px solid #ddd;">\n'
            for line_num, line in log_result['errors']:
                line_anchor = f"log_{var_base}_line_{line_num}"
                log_viewer_link = f"{log_viewer_filename}#{line_anchor}"
                html += f'<div class="log-entry error"><a href="{log_viewer_link}" target="log_viewer_{var_base}" style="color: #007acc; text-decoration: none; font-weight: bold;">Line {line_num}:</a> {self._escape_html(line)}</div>\n'
            html += '</div></details>\n'
        
        # Warnings
        if log_result['warnings']:
            section_id = f"{block_name}_{log_type}_warnings"
            warning_count = len(log_result['warnings'])
            html += f'<details style="margin-top: 10px; border: 1px solid #ccc; border-radius: 3px; padding: 0;"><summary style="cursor: pointer; padding: 10px; background-color: #f0f0f0; font-weight: bold; user-select: none; display: flex; justify-content: space-between; align-items: center;"><span>Warnings ({warning_count})</span><span style="color: #ff9933; font-weight: bold;">●{warning_count}</span></summary>\n'
            html += f'<div id="{section_id}" style="padding: 10px; background-color: #fafafa; border-top: 1px solid #ddd;">\n'
            for line_num, line in log_result['warnings']:
                line_anchor = f"log_{var_base}_line_{line_num}"
                log_viewer_link = f"{log_viewer_filename}#{line_anchor}"
                html += f'<div class="log-entry warning"><a href="{log_viewer_link}" target="log_viewer_{var_base}" style="color: #007acc; text-decoration: none; font-weight: bold;">Line {line_num}:</a> {self._escape_html(line)}</div>\n'
            html += '</div></details>\n'
        
        # Infos
        if log_result['infos']:
            section_id = f"{block_name}_{log_type}_infos"
            info_count = len(log_result['infos'])
            html += f'<details style="margin-top: 10px; border: 1px solid #ccc; border-radius: 3px; padding: 0;"><summary style="cursor: pointer; padding: 10px; background-color: #f0f0f0; font-weight: bold; user-select: none; display: flex; justify-content: space-between; align-items: center;"><span>Info ({info_count})</span><span style="color: #ffdd44; font-weight: bold;">●{info_count}</span></summary>\n'
            html += f'<div id="{section_id}" style="padding: 10px; background-color: #fafafa; border-top: 1px solid #ddd;">\n'
            for line_num, line in log_result['infos']:
                line_anchor = f"log_{var_base}_line_{line_num}"
                log_viewer_link = f"{log_viewer_filename}#{line_anchor}"
                html += f'<div class="log-entry info"><a href="{log_viewer_link}" target="log_viewer_{var_base}" style="color: #007acc; text-decoration: none; font-weight: bold;">Line {line_num}:</a> {self._escape_html(line)}</div>\n'
            html += '</div></details>\n'
        
        
        if not log_result['errors'] and not log_result['warnings'] and not log_result['infos'] and not html_report_sections:
            html += '<p style="color: #44cc44;">✓ No issues found</p>\n'
        
        return html
    
    def _escape_html(self, text):
        """Escape HTML special characters."""
        return text.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')

    # ------------------------------------------------------------------
    # Chip-hierarchy rendering helpers
    # ------------------------------------------------------------------

    def _aggregate_counts(self, node_name, node_children, results_by_name):
        """Return ``(rem_e, ign_e, rem_w, ign_w)`` summed across *node_name* and
        all its descendants (recursive).  Nodes without data contribute zeros."""
        rem_e = ign_e = rem_w = ign_w = 0
        if node_name in results_by_name:
            result = results_by_name[node_name]
            for r in result['log_results'].values():
                rem_e += len(r.get('errors', []))
                ign_e += len(r.get('ignored_errors', []))
                rem_w += len(r.get('warnings', []))
                ign_w += len(r.get('ignored_warnings', []))
        for child_name, child_children in node_children.items():
            ce, ci, cw, cwi = self._aggregate_counts(child_name, child_children, results_by_name)
            rem_e += ce; ign_e += ci; rem_w += cw; ign_w += cwi
        return rem_e, ign_e, rem_w, ign_w

    @staticmethod
    def _fmt_counts(rem, ign, tot, rem_css='color:#cc0000', zero_css='color:#888'):
        """Return a compact HTML badge: ``rem / ign / tot``.

        All three numbers are always shown so the format is consistent:
        * *rem* (remaining) is highlighted when > 0.
        * *ign* (waived/ignored) is shown in muted style.
        * *tot* (total = rem+ign) closes the triple.
        A ``title`` tooltip spells out the meaning.
        """
        rem_style = rem_css if rem > 0 else zero_css
        return (
            f'<span title="remaining / waived / total" style="font-size:0.82em;font-weight:bold;">'
            f'<span style="{rem_style}">{rem}</span>'
            f'<span style="color:#aaa;font-weight:normal;">/</span>'
            f'<span style="color:#888;">{ign}</span>'
            f'<span style="color:#aaa;font-weight:normal;">/</span>'
            f'<span style="color:#555;">{tot}</span>'
            f'</span>'
        )

    def _compute_aggregate_status(self, node_name, node_children, results_by_name):
        """Return the worst-case aggregate status for *node_name* and all descendants.

        If the node itself has run results those take precedence over
        any child aggregation.  Nodes with no data anywhere return 'nodata'.
        """
        if node_name in results_by_name:
            return results_by_name[node_name]['overall_status']
        if not node_children:
            return 'nodata'
        child_statuses = [
            self._compute_aggregate_status(c, cc, results_by_name)
            for c, cc in node_children.items()
        ]
        real = [s for s in child_statuses if s != 'nodata']
        if not real:
            return 'nodata'
        return max(real, key=lambda s: self._STATUS_RANK.get(s, -1))

    def _render_hierarchy_node(self, node_name, node_children, results_by_name, depth=0):
        """Recursively render one chip-hierarchy node as a collapsible <details>.

        All levels are open by default so the full tree is visible on load.
        Only the inner detail sections (Reports/Errors/Warnings/Info) are collapsed.
        """
        status = self._compute_aggregate_status(node_name, node_children, results_by_name)
        color  = self.STATUS_COLORS.get(status, '#aaaaaa')
        label  = self.STATUS_LABELS.get(status, status.upper())
        has_data = node_name in results_by_name
        result   = results_by_name.get(node_name)

        open_attr = ' open'  # all hierarchy levels expanded on load

        # Summary bar style – progressively lighter with depth
        if depth == 0:
            sum_bg  = '#e8f0fe'
            sum_bdr = '2px solid #3f51b5'
            sum_fnt = 'font-weight:bold; font-size:1.0em;'
        elif depth == 1:
            sum_bg  = '#f5f5f5'
            sum_bdr = '1px solid #bdbdbd'
            sum_fnt = 'font-weight:600;'
        else:
            sum_bg  = '#ffffff'
            sum_bdr = '1px solid #e0e0e0'
            sum_fnt = ''

        html  = f'<details class="hier-node"{open_attr} style="margin:4px 0;">\n'
        html += (f'  <summary style="display:flex;align-items:center;gap:10px;cursor:pointer;'
                 f'padding:8px 12px;border-radius:4px;user-select:none;'
                 f'background:{sum_bg};border:{sum_bdr};{sum_fnt}">\n')
        html += f'    <span style="font-family:\'Courier New\',monospace;">{node_name}</span>\n'

        # Inline error / warning counts: remaining / waived / total
        rem_e, ign_e, rem_w, ign_w = self._aggregate_counts(node_name, node_children, results_by_name)
        tot_e = rem_e + ign_e
        tot_w = rem_w + ign_w
        if tot_e > 0:
            html += (f'    <span style="font-size:0.82em;">'
                     f'<span style="color:#cc0000;font-weight:bold;">E:</span>&nbsp;'
                     f'{self._fmt_counts(rem_e, ign_e, tot_e)}'
                     f'</span>\n')
        if tot_w > 0:
            html += (f'    <span style="font-size:0.82em;">'
                     f'<span style="color:#cc7a00;font-weight:bold;">W:</span>&nbsp;'
                     f'{self._fmt_counts(rem_w, ign_w, tot_w, rem_css="color:#cc7a00")}'
                     f'</span>\n')

        html += (f'    <span style="margin-left:auto;background:{color};color:white;'
                 f'padding:3px 10px;border-radius:12px;font-size:0.82em;font-weight:bold;">'
                 f'{label}</span>\n')
        html += '  </summary>\n'

        html += '  <div style="padding-left:20px;border-left:2px solid #e0e4f0;margin-left:8px;margin-top:4px;">\n'

        # Recurse into children first, then emit this node's data section
        for child_name, child_children in node_children.items():
            html += self._render_hierarchy_node(child_name, child_children, results_by_name, depth + 1)

        if has_data and result:
            html += self._generate_block_section(result)
        elif not node_children:
            html += '    <div style="padding:8px 12px;color:#999;font-style:italic;font-size:0.9em;">No data available</div>\n'

        html += '  </div>\n'
        html += '</details>\n'
        return html

    def _generate_hierarchical_view(self, block_results, chip_name, hierarchy):
        """Render the full chip hierarchy as nested collapsible sections.

        All blocks from the schema are shown.  Blocks with run results display
        their actual status and detail; blocks without data show 'No Data'.
        """
        results_by_name = {r['block_name']: r for r in block_results}

        # Aggregate chip-level status from all CF children
        child_statuses = [
            self._compute_aggregate_status(cf, cf_ch, results_by_name)
            for cf, cf_ch in hierarchy.items()
        ]
        real = [s for s in child_statuses if s != 'nodata']
        chip_status = max(real, key=lambda s: self._STATUS_RANK.get(s, -1)) if real else 'nodata'
        chip_color  = self.STATUS_COLORS.get(chip_status, '#aaaaaa')
        chip_label  = self.STATUS_LABELS.get(chip_status, chip_status.upper())

        html  = '<h2>Chip Hierarchy</h2>\n'
        html += '<div style="margin:10px 0;">\n'

        # ── Chip-level (skylp) collapsible ──────────────────────────────────
        html += '<details open>\n'
        html += (f'  <summary style="display:flex;align-items:center;gap:12px;cursor:pointer;'
                 f'padding:10px 18px;border-radius:6px;background:#1a237e;color:white;'
                 f'user-select:none;font-size:1.15em;font-weight:bold;">\n')
        html += (f'    <span style="font-family:\'Courier New\',monospace;letter-spacing:1px;">'
                 f'{chip_name.upper()}</span>\n')

        # Chip-level aggregate counts
        chip_rem_e = chip_ign_e = chip_rem_w = chip_ign_w = 0
        for cf, cf_ch in hierarchy.items():
            ce, ci, cw, cwi = self._aggregate_counts(cf, cf_ch, results_by_name)
            chip_rem_e += ce; chip_ign_e += ci; chip_rem_w += cw; chip_ign_w += cwi
        chip_tot_e = chip_rem_e + chip_ign_e
        chip_tot_w = chip_rem_w + chip_ign_w
        if chip_tot_e > 0:
            html += (f'    <span style="font-size:0.9em;">'
                     f'<span style="color:#ffaaaa;font-weight:bold;">E:</span>&thinsp;'
                     f'<span title="remaining / waived / total" style="font-weight:bold;">'
                     f'<span style="color:#ff8888;">{chip_rem_e}</span>'
                     f'<span style="color:#ccc;font-weight:normal;">/</span>'
                     f'<span style="color:#bbb;">{chip_ign_e}</span>'
                     f'<span style="color:#ccc;font-weight:normal;">/</span>'
                     f'<span style="color:#eee;">{chip_tot_e}</span>'
                     f'</span></span>\n')
        if chip_tot_w > 0:
            html += (f'    <span style="font-size:0.9em;">'
                     f'<span style="color:#ffcc88;font-weight:bold;">W:</span>&thinsp;'
                     f'<span title="remaining / waived / total" style="font-weight:bold;">'
                     f'<span style="color:#ffa040;">{chip_rem_w}</span>'
                     f'<span style="color:#ccc;font-weight:normal;">/</span>'
                     f'<span style="color:#bbb;">{chip_ign_w}</span>'
                     f'<span style="color:#ccc;font-weight:normal;">/</span>'
                     f'<span style="color:#eee;">{chip_tot_w}</span>'
                     f'</span></span>\n')

        html += (f'    <span style="margin-left:auto;background:{chip_color};'
                 f'padding:4px 14px;border-radius:14px;font-size:0.85em;">{chip_label}</span>\n')
        html += '  </summary>\n'
        html += '  <div style="padding-left:20px;border-left:3px solid #1a237e;margin-left:10px;margin-top:6px;">\n'

        for cf_name, cf_children in hierarchy.items():
            html += self._render_hierarchy_node(cf_name, cf_children, results_by_name, depth=0)

        html += '  </div>\n'
        html += '</details>\n'
        html += '</div>\n'
        return html

    def _generate_footer(self):
        """Generate HTML footer."""
        return """
</body>
</html>
"""
    
    def _generate_log_viewer_html(self, log_result, block_name, log_type, output_dir):
        """Generate a standalone HTML file for log viewing with line numbers."""
        var_base = f"{block_name}_{log_type}".replace(' ', '_')
        viewer_filename = os.path.join(output_dir, f"{var_base}_log_viewer.html")
        
        html = f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Log Viewer: {block_name}/{log_type}</title>
    <style>
        body {{
            font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif;
            margin: 0;
            padding: 20px;
            background-color: #f5f5f5;
        }}
        h1 {{
            color: #333;
            border-bottom: 3px solid #007acc;
            padding-bottom: 10px;
        }}
        .controls {{
            background-color: white;
            padding: 10px;
            margin-bottom: 15px;
            border-radius: 4px;
            box-shadow: 0 2px 4px rgba(0,0,0,0.1);
        }}
        .search-box {{
            padding: 8px;
            width: 300px;
            border: 1px solid #ccc;
            border-radius: 4px;
            font-family: 'Courier New', monospace;
        }}
        .log-viewer {{
            background-color: white;
            border: 1px solid #ddd;
            border-radius: 4px;
            padding: 0;
            box-shadow: 0 2px 4px rgba(0,0,0,0.1);
            font-family: 'Courier New', monospace;
            font-size: 13px;
            line-height: 1.6;
            overflow-x: auto;
        }}
        .log-line {{
            padding: 2px 8px;
            display: flex;
            border-bottom: 1px solid #f0f0f0;
        }}
        .log-line:hover {{
            background-color: #f9f9f9;
        }}
        .log-line-number {{
            color: #888;
            margin-right: 15px;
            user-select: none;
            display: inline-block;
            min-width: 50px;
            text-align: right;
            flex-shrink: 0;
        }}
        .log-line-content {{
            flex-grow: 1;
            white-space: pre-wrap;
            word-wrap: break-word;
            color: #333;
        }}
        .log-line.error {{
            background-color: #ffeeee;
            border-left: 3px solid #ff4444;
        }}
        .log-line.warning {{
            background-color: #fff8ee;
            border-left: 3px solid #ff9933;
        }}
        .log-line.info {{
            background-color: #fffcee;
            border-left: 3px solid #ffdd44;
        }}
        .log-line.report {{
            background-color: #f0f8ff;
            border-left: 3px solid #007acc;
            font-weight: bold;
        }}
        .log-line:target {{
            background-color: #ffff99 !important;
            border-left: 4px solid #ffdd44;
        }}
    </style>
</head>
<body>
    <h1>📖 Log Viewer: {block_name}/{log_type}</h1>
    <p><strong>File:</strong> <code>{log_result.get("path", "Unknown")}</code></p>
    
    <div class="controls">
        <input type="text" class="search-box" id="searchBox" placeholder="Use Ctrl+F to search lines...">
    </div>
    
    <div class="log-viewer" id="logViewer">
"""
        
        # Read and process log lines
        try:
            with open(log_result["path"], 'r', encoding='utf-8', errors='ignore') as lf:
                log_lines = lf.readlines()
        except:
            log_lines = []
        
        # Build a set of special lines for quick lookup
        error_lines = {line_num for line_num, _ in log_result.get('errors', [])}
        warning_lines = {line_num for line_num, _ in log_result.get('warnings', [])}
        info_lines = {line_num for line_num, _ in log_result.get('infos', [])}
        report_start_lines = {report.get('line_number') for report in log_result.get('report_sections', [])}
        
        # Generate line HTML
        for line_idx, log_line in enumerate(log_lines, 1):
            line_anchor = f"log_{var_base}_line_{line_idx}"
            
            # Determine line class
            line_class = ""
            if line_idx in error_lines:
                line_class = "error"
            elif line_idx in warning_lines:
                line_class = "warning"
            elif line_idx in info_lines:
                line_class = "info"
            elif line_idx in report_start_lines:
                line_class = "report"
            
            class_attr = f' class="log-line {line_class}"' if line_class else ' class="log-line"'
            
            # Escape HTML
            line_content = log_line.rstrip()
            line_content = line_content.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
            
            html += f'<div id="{line_anchor}"{class_attr}><span class="log-line-number">{line_idx}</span><span class="log-line-content">{line_content}</span></div>\n'
        
        html += """    </div>
</body>
</html>
"""
        
        # Write the standalone HTML file
        try:
            with open(viewer_filename, 'w', encoding='utf-8') as f:
                f.write(html)
            return viewer_filename
        except Exception as e:
            print(f"Warning: Failed to write log viewer file {viewer_filename}: {e}")
            return None


class CSVReportGenerator:
    """Generates CSV report from analysis results."""

    def __init__(self, output_dir):
        self.output_dir = output_dir

    def generate_custom_csvs(self, block_results, csv_directives, chip_name, hierarchy,
                             ordered_pattern_names):
        """Generate custom CSV files declared by ``csv`` directives in pattern.yaml.

        Each directive looks like::

            - csv: 'status.csv'
              cols: block_name, hier_name, status
              data: block_name, hier_name, pattern1.group1

        ``cols`` defines the header row.  ``data`` maps column expressions
        (comma-separated) to the header.  Supported expressions:

        * ``block_name``          – the block being analysed
        * ``hier_name``           – dot-path(s) from the chip hierarchy
        * ``patternN.groupM``     – Nth report_pattern, group M (worst-ranked occ)
        * ``patternN.patternM.groupK`` – via sub_patterns

        One row is emitted per (block, hier_path) pair.  A block absent from
        the hierarchy still gets one row with an empty ``hier_name``.
        """
        if not csv_directives:
            return

        for directive in csv_directives:
            csv_filename = directive.get('csv', 'custom_report.csv')
            cols_raw = directive.get('cols', '')
            data_raw = directive.get('data', '')

            headers = [c.strip() for c in cols_raw.split(',') if c.strip()]
            exprs   = [c.strip() for c in data_raw.split(',') if c.strip()]

            # Pad / truncate to same length
            while len(exprs) < len(headers):
                exprs.append('')
            exprs = exprs[:len(headers)]

            out_path = os.path.join(self.output_dir, csv_filename)
            try:
                with open(out_path, 'w', newline='') as f:
                    writer = csv.writer(f)
                    writer.writerow(headers)

                    for result in block_results:
                        block_name = result['block_name']

                        # Collect ALL report_sections across every log file for this block
                        all_sections = []
                        for lr in result['log_results'].values():
                            all_sections.extend(lr.get('report_sections', []))

                        # Determine hier_paths for this block
                        if chip_name and hierarchy:
                            hier_paths = _find_block_paths(block_name, chip_name, hierarchy)
                        else:
                            hier_paths = []
                        if not hier_paths:
                            hier_paths = ['']  # still emit one row

                        for hier_path in hier_paths:
                            row = [
                                _resolve_csv_expr(expr, block_name, hier_path,
                                                  all_sections, ordered_pattern_names,
                                                  block_dir=result['block_dir'])
                                for expr in exprs
                            ]
                            writer.writerow(row)

                print(f"Custom CSV generated: {out_path}")
            except Exception as e:
                print(f"Error generating custom CSV '{csv_filename}': {e}")

    def generate(self, block_results):
        """Generate CSV reports for each block."""
        csv_dir = os.path.join(self.output_dir, 'block_csv')
        os.makedirs(csv_dir, exist_ok=True)
        # Generate summary CSV
        summary_file = os.path.join(csv_dir, 'fev_report_summary.csv')
        self._generate_summary_csv(block_results, summary_file)
        
        # Generate detailed CSV for each block
        for result in block_results:
            block_name = result['block_name']
            csv_file = os.path.join(csv_dir, f'{block_name}_detailed_report.csv')
            self._generate_detailed_csv(result, csv_file)
    
    def _generate_summary_csv(self, block_results, csv_file):
        """Generate summary CSV with one row per block."""
        try:
            with open(csv_file, 'w', newline='') as f:
                writer = csv.writer(f)
                
                # Write header
                writer.writerow(['Block Name', 'Status', 'Total Errors', 'Total Warnings', 'Total Infos', 'Block Directory'])
                
                # Write data for each block
                for result in block_results:
                    block_name = result['block_name']
                    status = result['overall_status']
                    block_dir = result['block_dir']
                    
                    # Count totals
                    total_errors = sum(len(r['errors']) for r in result['log_results'].values())
                    total_warnings = sum(len(r['warnings']) for r in result['log_results'].values())
                    total_infos = sum(len(r['infos']) for r in result['log_results'].values())
                    
                    writer.writerow([block_name, status, total_errors, total_warnings, total_infos, block_dir])
            
            print(f"Summary CSV generated: {csv_file}")
        except Exception as e:
            print(f"Error generating summary CSV: {e}")
    
    # Regex to extract warning class code, e.g:
    #   "Warning: (RTL9.21)"     -> class='RTL9.21',    major='RTL'
    #   "Warning: (LIB_LINT_121)" -> class='LIB_LINT_121', major='LIB'
    _WARN_CLASS_RE = re.compile(r'Warning:\s*\(([A-Za-z][A-Za-z0-9_.]*)')
    _MAJOR_PREFIX_RE = re.compile(r'^([A-Za-z]+)')

    @classmethod
    def _extract_warn_class(cls, line):
        """Return (major_class, class_code) from a warning line, or ('', '') if not found."""
        m = cls._WARN_CLASS_RE.search(line)
        if m:
            code  = m.group(1)                        # e.g. 'RTL9.21' or 'LIB_LINT_121'
            pm = cls._MAJOR_PREFIX_RE.match(code)
            major = pm.group(1) if pm else code       # e.g. 'RTL' or 'LIB'
            return major, code
        return '', ''

    def _generate_detailed_csv(self, result, csv_file):
        """Generate detailed CSV for a single block with all log entries."""
        try:
            with open(csv_file, 'w', newline='') as f:
                writer = csv.writer(f)
                
                # Write header
                writer.writerow(['Block Name', 'Log Type', 'Issue Type', 'Line Number', 'Message', 'Major Class', 'Class'])
                
                block_name = result['block_name']
                
                # Write data for each log file
                for log_type, log_result in result['log_results'].items():
                    if not log_result['exists']:
                        writer.writerow([block_name, log_type, 'N/A', 'N/A', 'File not found', '', ''])
                        continue
                    
                    # Write report sections
                    for report in log_result.get('report_sections', []):
                        content = report['content']
                        writer.writerow([block_name, log_type, 'REPORT', report['name'],
                                         content[:100] + '...' if len(content) > 100 else content,
                                         '', ''])
                    
                    # Write errors
                    for line_num, line in log_result['errors']:
                        writer.writerow([block_name, log_type, 'ERROR', line_num, line, '', ''])
                    
                    # Write warnings — also extract class code
                    for line_num, line in log_result['warnings']:
                        major, code = self._extract_warn_class(line)
                        writer.writerow([block_name, log_type, 'WARNING', line_num, line, major, code])
                    
                    # Write infos
                    for line_num, line in log_result['infos']:
                        writer.writerow([block_name, log_type, 'INFO', line_num, line, '', ''])
            
            print(f"Detailed CSV generated: {csv_file}")
        except Exception as e:
            print(f"Error generating detailed CSV for {result['block_name']}: {e}")

    # ------------------------------------------------------------------
    # HTML status-page generation (mirrors generate_custom_csvs)
    # ------------------------------------------------------------------

    @staticmethod
    def _html_escape(text):
        """Escape HTML special characters."""
        return (str(text)
                .replace('&', '&amp;')
                .replace('<', '&lt;')
                .replace('>', '&gt;')
                .replace('"', '&quot;'))

    def generate_custom_htmls(self, block_results, csv_directives, chip_name, hierarchy,
                               ordered_pattern_names, comments=None, port=8765):
        """Generate an HTML status page for every ``csv:`` directive in pattern.yaml.

        For each ``csv: 'xxx.csv'`` directive a parallel ``xxx.html`` is written
        to the same output directory.  The table mirrors the CSV columns with
        colour-coding and sort / filter controls.
        """
        if not csv_directives:
            return

        for directive in csv_directives:
            csv_filename = directive.get('csv', 'custom_report.csv')
            html_filename = re.sub(r'\.csv$', '.html', csv_filename, flags=re.IGNORECASE)
            if html_filename == csv_filename:
                html_filename = csv_filename + '.html'

            cols_raw = directive.get('cols', '')
            data_raw = directive.get('data', '')

            headers = [c.strip() for c in cols_raw.split(',') if c.strip()]
            exprs   = [c.strip() for c in data_raw.split(',') if c.strip()]
            while len(exprs) < len(headers):
                exprs.append('')
            exprs = exprs[:len(headers)]

            # Index of the 'status' column so we can apply the fallback below
            hdr_lower = [h.lower().strip() for h in headers]
            status_col_idx = next((i for i, h in enumerate(hdr_lower) if h == 'status'), None)

            # Collect rows — identical logic to generate_custom_csvs
            rows = []
            for result in block_results:
                block_name = result['block_name']
                all_sections = []
                for lr in result['log_results'].values():
                    all_sections.extend(lr.get('report_sections', []))

                if chip_name and hierarchy:
                    hier_paths = _find_block_paths(block_name, chip_name, hierarchy)
                else:
                    hier_paths = []
                if not hier_paths:
                    hier_paths = ['']

                for hier_path in hier_paths:
                    row = [
                        _resolve_csv_expr(expr, block_name, hier_path,
                                          all_sections, ordered_pattern_names,
                                          block_dir=result['block_dir'])
                        for expr in exprs
                    ]
                    # If the status cell is blank (LEC status pattern not found in the log),
                    # derive a fallback.  A clean log (no errors/warnings) most likely means
                    # the run is still in progress, so show RUNNING.  Any log-level issues
                    # surface as ERROR / WARNING so the user knows something went wrong.
                    if status_col_idx is not None and not (row[status_col_idx] or '').strip():
                        overall = result.get('overall_status', 'nodata')
                        if overall == 'success':
                            # No LEC result yet and no log issues — job is likely still running.
                            row[status_col_idx] = 'data: RUNNING'
                        else:
                            label = HTMLReportGenerator.STATUS_LABELS.get(overall, overall.upper())
                            row[status_col_idx] = f'data: {label}'
                    rows.append(row)

            out_path = os.path.join(self.output_dir, html_filename)
            try:
                self._write_status_html(out_path, html_filename, headers, rows,
                                        comments=comments or {}, port=port)
                print(f"Status HTML generated: {out_path}")
            except Exception as e:
                print(f"Error generating status HTML '{html_filename}': {e}")

    def _write_status_html(self, out_path, title, headers, rows, comments=None, port=8765):
        """Render *rows* as a styled, sortable, filterable single-page HTML table."""
        comments = comments or {}
        _save_path  = str(comments_file_path(self.output_dir))
        _server_url = f'http://localhost:{port}/api/save_comments'
        header_lower = [h.lower().strip() for h in headers]

        # Identify special columns by name for automatic colour-coding
        status_col = next((i for i, h in enumerate(header_lower) if h == 'status'), None)
        noneq_col  = next((i for i, h in enumerate(header_lower) if 'noneq' in h), None)
        abort_col  = next((i for i, h in enumerate(header_lower) if 'abort' in h), None)
        pass_col   = next((i for i, h in enumerate(header_lower)
                           if h in ('modules pass', 'pass', 'module pass')), None)

        status_col_js = status_col if status_col is not None else -1
        now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')

        # Colour map for "data: <STATUS>" fallback values
        _DATA_STATUS_STYLE = {
            'pass':    'background:#dcedc8;color:#33691e;font-weight:bold;font-style:italic;',
            'running': 'background:#e3f2fd;color:#0d47a1;font-weight:bold;font-style:italic;',
            'error':   'background:#ffcdd2;color:#b71c1c;font-weight:bold;font-style:italic;',
            'warning': 'background:#ffe0b2;color:#e65100;font-weight:bold;font-style:italic;',
            'info':    'background:#fff9c4;color:#f57f17;font-weight:bold;font-style:italic;',
            'missing': 'background:#eeeeee;color:#757575;font-weight:bold;font-style:italic;',
            'no data': 'background:#eeeeee;color:#9e9e9e;font-style:italic;',
        }

        def _cell_style(col_idx, value):
            base = 'padding:8px 12px;border-bottom:1px solid #e0e0e0;'
            if col_idx == status_col:
                v = (value or '').strip().lower()
                if v == 'equivalent':
                    return base + 'background:#c8e6c9;color:#1b5e20;font-weight:bold;'
                elif v.startswith('data: '):
                    # Fallback value — colour by the embedded status keyword
                    keyword = v[len('data: '):].strip()
                    extra = _DATA_STATUS_STYLE.get(keyword, 'font-style:italic;')
                    return base + extra
                elif v:
                    return base + 'background:#ffcdd2;color:#b71c1c;font-weight:bold;'
            elif col_idx in (noneq_col, abort_col):
                try:
                    if int(value or 0) > 0:
                        return base + 'background:#ffcdd2;color:#b71c1c;font-weight:bold;'
                except (ValueError, TypeError):
                    pass
            elif col_idx == pass_col:
                try:
                    if int(value or 0) > 0:
                        return base + 'color:#1b5e20;font-weight:bold;'
                except (ValueError, TypeError):
                    pass
            return base

        # ── Header ────────────────────────────────────────────────────────────
        lines = []
        lines.append('<!DOCTYPE html>')
        lines.append('<html lang="en">')
        lines.append('<head>')
        lines.append('  <meta charset="UTF-8">')
        lines.append('  <meta name="viewport" content="width=device-width, initial-scale=1.0">')
        lines.append(f'  <title>FEV Block Status \u2014 {self._html_escape(title)}</title>')
        lines.append('  <style>')
        lines.append('    body{font-family:"Segoe UI",Tahoma,Geneva,Verdana,sans-serif;margin:20px;background:#f5f5f5;}')
        lines.append('    h1{color:#333;border-bottom:3px solid #007acc;padding-bottom:10px;}')
        lines.append('    .timestamp{color:#888;font-size:.9em;}')
        lines.append('    .controls{display:flex;flex-wrap:wrap;gap:10px;align-items:center;background:white;')
        lines.append('              padding:12px 16px;border-radius:6px;box-shadow:0 2px 4px rgba(0,0,0,.08);margin-bottom:16px;}')
        lines.append('    .controls label{font-size:.9em;color:#555;}')
        lines.append('    #filterInput{padding:6px 10px;border:1px solid #ccc;border-radius:4px;font-size:.9em;width:260px;}')
        lines.append('    #statusFilter{padding:6px 10px;border:1px solid #ccc;border-radius:4px;font-size:.9em;}')
        lines.append('    .stats{display:flex;gap:12px;flex-wrap:wrap;font-size:.88em;}')
        lines.append('    .stat-badge{padding:4px 12px;border-radius:12px;font-weight:bold;}')
        lines.append('    .stat-pass{background:#c8e6c9;color:#1b5e20;}')
        lines.append('    .stat-fail{background:#ffcdd2;color:#b71c1c;}')
        lines.append('    .stat-total{background:#e3f2fd;color:#0d47a1;}')
        lines.append('    table{border-collapse:collapse;width:100%;background:white;')
        lines.append('          box-shadow:0 2px 6px rgba(0,0,0,.1);font-size:.88em;}')
        lines.append('    thead tr{background:#007acc;color:white;}')
        lines.append('    th{padding:10px 14px;text-align:left;cursor:pointer;user-select:none;white-space:nowrap;}')
        lines.append('    th:hover{background:#005fa3;}')
        lines.append('    .sort-arrow{margin-left:4px;font-size:.8em;color:#cce4ff;}')
        lines.append('    tbody tr{transition:background .12s;}')
        lines.append('    tbody tr:hover{background:#f0f7ff;}')
        lines.append('    tbody tr:nth-child(even){background:#fafafa;}')
        lines.append('    tbody tr:nth-child(even):hover{background:#f0f7ff;}')
        lines.append('    td{padding:8px 12px;border-bottom:1px solid #e0e0e0;white-space:nowrap;}')
        lines.append('    td.wrap{white-space:normal;max-width:420px;word-break:break-all;}')
        lines.append('    .hidden{display:none!important;}')
        lines.append('    #rowCount{font-size:.85em;color:#666;}')
        lines.append('    .comment-box{width:100%;min-width:180px;box-sizing:border-box;border:1px solid #ccc;border-radius:4px;padding:4px 6px;font-size:.85em;font-family:inherit;resize:vertical;background:#fffde7;}')
        lines.append('    .comment-box:focus{outline:none;border-color:#007acc;background:#fff;}')
        lines.append('    .action-btn{padding:6px 14px;border:none;border-radius:4px;cursor:pointer;font-size:.88em;font-weight:bold;}')
        lines.append('    .btn-save{background:#43a047;color:white;}')
        lines.append('    .btn-save:hover{background:#2e7d32;}')
        lines.append('    .btn-load{background:#6a1b9a;color:white;}')
        lines.append('    .btn-load:hover{background:#4a0072;}')
        lines.append('    .user-status-sel{width:100%;padding:2px 4px;font-size:.82em;border-radius:3px;border:1px solid #ccc;cursor:pointer;}')
        lines.append('    .us-error{background:#ffcdd2;}')
        lines.append('    .us-warning{background:#fff9c4;}')
        lines.append('    .us-clean{background:#c8e6c9;}')
        lines.append('    .us-known{background:#ffe0b2;}')
        lines.append('    #toast{position:fixed;bottom:24px;right:24px;background:#323232;color:white;padding:10px 20px;border-radius:6px;font-size:.9em;display:none;z-index:9999;}')
        lines.append('  </style>')
        lines.append('</head>')
        lines.append('<body>')
        lines.append('  <h1>FEV Block Status Report</h1>')
        lines.append(f'  <p class="timestamp">Generated: {now} &nbsp;|&nbsp; Source: {self._html_escape(title)}</p>')
        lines.append('  <div class="controls">')
        lines.append('    <label>Search: <input type="text" id="filterInput" placeholder="Filter any column\u2026" oninput="applyFilters()"></label>')
        lines.append('    <label>Status:')
        lines.append('      <select id="statusFilter" onchange="applyFilters()">')
        lines.append('        <option value="">All</option>')
        lines.append('        <option value="equivalent">Equivalent (PASS)</option>')
        lines.append('        <option value="non">Non-Equivalent / Abort (FAIL)</option>')
        lines.append('      </select>')
        lines.append('    </label>')
        lines.append('    <div class="stats" id="statsBar"></div>')
        lines.append('    <span id="rowCount"></span>')
        lines.append('    <button class="action-btn btn-save" onclick="saveCommentsJSON()">&#128190; Save Comments</button>')
        lines.append('    <button class="action-btn btn-load" onclick="loadCommentsJSON()">&#128196; Load Comments</button>')
        lines.append('    <button class="action-btn btn-export" onclick="exportCommentsCSV()">&#128196; Export CSV</button>')
        lines.append('    <input type="file" id="commentFileInput" accept=".json" style="display:none" onchange="_onCommentFileChosen(event)">')
        lines.append('  </div>')
        lines.append('  <div id="toast"></div>')

        # ── Table ─────────────────────────────────────────────────────────────
        lines.append('  <table id="statusTable">')
        notes_col_idx = len(headers)  # Notes column appended after all data columns
        lines.append('    <thead><tr>')
        for i, h in enumerate(headers):
            lines.append(f'      <th onclick="sortTable({i})" data-col="{i}">'
                         f'{self._html_escape(h)} <span class="sort-arrow" id="arrow_{i}">\u21c5</span></th>')
        lines.append(f'      <th data-col="{notes_col_idx}">Notes</th>')
        lines.append('    </tr></thead>')
        lines.append('    <tbody id="tableBody">')

        long_col_start = max(0, len(headers) - 4)   # last few columns wrap (paths)
        for row_data in rows:
            # block_name is always column 0 (per pattern.yaml cols: definition)
            block_key = str(row_data[0]) if row_data else ''
            pre_comment = self._html_escape(
                (comments.get(block_key) or {}).get('comment', '')
            )
            pre_status = (comments.get(block_key) or {}).get('user_status', '')
            lines.append('      <tr>')
            _us_color_map = {'user Error': '#ffcdd2', 'user Warning': '#fff9c4',
                              'user clean': '#c8e6c9', 'user known issue': '#ffe0b2'}
            for ci, val in enumerate(row_data):
                style = _cell_style(ci, val)
                wrap = ' class="wrap"' if ci >= long_col_start else ''
                # "data: RUNNING" / "data: ERROR" etc. → human-friendly label
                display = val if val is not None else ''
                if isinstance(display, str) and display.startswith('data: '):
                    keyword = display[6:].strip().title()
                    display = f'Not Completed \u2014 {keyword}'
                if ci == status_col:
                    # Status column: show original run value + editable user dropdown
                    _so = ''.join(
                        f'<option value="{v}"{" selected" if pre_status == v else ""}>{lbl}</option>'
                        for v, lbl in [
                            ('', '\u2014 (run status) \u2014'),
                            ('user Error', 'user Error'),
                            ('user Warning', 'user Warning'),
                            ('user clean', 'user clean'),
                            ('user known issue', 'user known issue'),
                        ]
                    )
                    _bg = _us_color_map.get(pre_status, '')
                    _td_style = f'background:{_bg};padding:4px 8px;' if _bg else f'{style}padding:4px 8px;'
                    lines.append(
                        f'        <td style="{_td_style}"{wrap}>'
                        f'<small style="color:#555;display:block;margin-bottom:3px">{self._html_escape(display)}</small>'
                        f'<select class="user-status-sel" data-block="{self._html_escape(block_key)}"'
                        f' onchange="_updateStatusColor(this)">{_so}</select></td>'
                    )
                else:
                    lines.append(f'        <td style="{style}"{wrap}>{self._html_escape(display)}</td>')
            # Notes textarea — pre-populated from block_comments.json
            lines.append(
                f'        <td style="padding:4px 8px;white-space:normal;min-width:200px;">'
                f'<textarea class="comment-box" data-block="{self._html_escape(block_key)}"'
                f' rows="2">{pre_comment}</textarea></td>'
            )
            lines.append('      </tr>')

        lines.append('    </tbody>')
        lines.append('  </table>')

        # ── JavaScript ────────────────────────────────────────────────────────
        comments_init_js = json.dumps(
            {k: {'comment': v.get('comment', '') if isinstance(v, dict) else str(v),
                 'user_status': v.get('user_status', '') if isinstance(v, dict) else ''}
             for k, v in comments.items()},
            ensure_ascii=False
        )
        lines.append('  <script>')
        lines.append('    var _sortState = {col: -1, asc: true};')
        lines.append(f'   var STATUS_COL = {status_col_js};')
        lines.append(f'   var COMMENTS_INIT = {comments_init_js};')
        lines.append(f'   var COMMENTS_SERVER_URL = {json.dumps(_server_url)};')
        lines.append(f'   var COMMENTS_SAVE_PATH  = {json.dumps(_save_path)};')
        lines.append('')
        lines.append('    function applyFilters() {')
        lines.append('      var text   = document.getElementById("filterInput").value.toLowerCase();')
        lines.append('      var status = document.getElementById("statusFilter").value.toLowerCase();')
        lines.append('      var rows   = document.getElementById("tableBody").querySelectorAll("tr");')
        lines.append('      var visible = 0;')
        lines.append('      rows.forEach(function(row) {')
        lines.append('        var cells   = row.querySelectorAll("td");')
        lines.append('        var rowText = Array.from(cells).map(function(c){return c.textContent;}).join(" ").toLowerCase();')
        lines.append('        var textOk  = !text || rowText.indexOf(text) !== -1;')
        lines.append('        var stOk    = true;')
        lines.append('        if (status && STATUS_COL >= 0) {')
        lines.append('          var sv = (cells[STATUS_COL] ? cells[STATUS_COL].textContent : "").toLowerCase().trim();')
        lines.append('          if (status === "equivalent")  stOk = sv === "equivalent";')
        lines.append('          else if (status === "non")    stOk = sv !== "equivalent" && sv !== "";')
        lines.append('        }')
        lines.append('        if (textOk && stOk) { row.classList.remove("hidden"); visible++; }')
        lines.append('        else                { row.classList.add("hidden"); }')
        lines.append('      });')
        lines.append('      document.getElementById("rowCount").textContent =')
        lines.append('        "Showing " + visible + " of " + rows.length + " block(s)";')
        lines.append('      updateStats();')
        lines.append('    }')
        lines.append('')
        lines.append('    function sortTable(col) {')
        lines.append('      var tbody = document.getElementById("tableBody");')
        lines.append('      var rows  = Array.from(tbody.querySelectorAll("tr"));')
        lines.append('      var asc   = (_sortState.col === col) ? !_sortState.asc : true;')
        lines.append('      _sortState = {col: col, asc: asc};')
        lines.append('      rows.sort(function(a, b) {')
        lines.append('        var av = a.querySelectorAll("td")[col];')
        lines.append('        var bv = b.querySelectorAll("td")[col];')
        lines.append('        av = av ? av.textContent.trim() : "";')
        lines.append('        bv = bv ? bv.textContent.trim() : "";')
        lines.append('        var an = parseFloat(av), bn = parseFloat(bv);')
        lines.append('        if (!isNaN(an) && !isNaN(bn)) return asc ? an - bn : bn - an;')
        lines.append('        return asc ? av.localeCompare(bv) : bv.localeCompare(av);')
        lines.append('      });')
        lines.append('      rows.forEach(function(r){ tbody.appendChild(r); });')
        lines.append('      document.querySelectorAll(".sort-arrow").forEach(function(el){ el.textContent = "\u21c5"; });')
        lines.append('      var arrow = document.getElementById("arrow_" + col);')
        lines.append('      if (arrow) arrow.textContent = asc ? "\u2191" : "\u2193";')
        lines.append('    }')
        lines.append('')
        lines.append('    function updateStats() {')
        lines.append('      if (STATUS_COL < 0) return;')
        lines.append('      var rows = document.getElementById("tableBody").querySelectorAll("tr:not(.hidden)");')
        lines.append('      var pass = 0, fail = 0;')
        lines.append('      rows.forEach(function(row) {')
        lines.append('        var cells = row.querySelectorAll("td");')
        lines.append('        if (!cells[STATUS_COL]) return;')
        lines.append('        var sv = cells[STATUS_COL].textContent.trim().toLowerCase();')
        lines.append('        if (sv === "equivalent") pass++;')
        lines.append('        else if (sv) fail++;')
        lines.append('      });')
        lines.append('      document.getElementById("statsBar").innerHTML =')
        lines.append('        \'<span class="stat-badge stat-pass">PASS: \' + pass + \'</span>\' +')
        lines.append('        \'<span class="stat-badge stat-fail">FAIL: \' + fail + \'</span>\' +')
        lines.append('        \'<span class="stat-badge stat-total">Total: \' + (pass+fail) + \'</span>\';')
        lines.append('    }')
        lines.append('')
        lines.append('    function showToast(msg) {')
        lines.append('      var t = document.getElementById("toast");')
        lines.append('      t.textContent = msg; t.style.display = "block";')
        lines.append('      setTimeout(function(){ t.style.display = "none"; }, 2500);')
        lines.append('    }')
        lines.append('')
        lines.append('    function _updateStatusColor(sel) {')
        lines.append('      var td = sel.closest("td");')
        lines.append('      var map = {"user Error":"#ffcdd2","user Warning":"#fff9c4","user clean":"#c8e6c9","user known issue":"#ffe0b2"};')
        lines.append('      if (td) td.style.background = map[sel.value] || "";')
        lines.append('    }')
        lines.append('')
        lines.append('    function _applyCommentData(data) {')
        lines.append('      var count = 0;')
        lines.append('      document.querySelectorAll(".comment-box").forEach(function(ta) {')
        lines.append('        var blk = ta.getAttribute("data-block");')
        lines.append('        if (blk && data[blk] !== undefined) {')
        lines.append('          ta.value = (typeof data[blk] === "object") ? (data[blk].comment || "") : data[blk];')
        lines.append('          count++;')
        lines.append('        }')
        lines.append('      });')
        lines.append('      document.querySelectorAll(".user-status-sel").forEach(function(sel) {')
        lines.append('        var blk = sel.getAttribute("data-block");')
        lines.append('        if (blk && data[blk] !== undefined && typeof data[blk] === "object") {')
        lines.append('          sel.value = data[blk].user_status || "";')
        lines.append('          _updateStatusColor(sel);')
        lines.append('        }')
        lines.append('      });')
        lines.append('      showToast("Loaded comments for " + count + " block(s).");')
        lines.append('    }')
        lines.append('')
        lines.append('    function loadCommentsJSON() {')
        lines.append('      // Try server first; fall back to file picker')
        lines.append('      var apiUrl = COMMENTS_SERVER_URL.replace("/api/save_comments", "/api/comments");')
        lines.append('      fetch(apiUrl)')
        lines.append('        .then(function(r){ return r.ok ? r.json() : Promise.reject(); })')
        lines.append('        .then(function(data) { _applyCommentData(data); })')
        lines.append('        .catch(function() {')
        lines.append('          // Server not running — open file picker')
        lines.append('          var inp = document.getElementById("commentFileInput");')
        lines.append('          inp.value = "";  // reset so same file can be reloaded')
        lines.append('          inp.click();')
        lines.append('        });')
        lines.append('    }')
        lines.append('')
        lines.append('    function _onCommentFileChosen(evt) {')
        lines.append('      var file = evt.target.files[0];')
        lines.append('      if (!file) return;')
        lines.append('      var reader = new FileReader();')
        lines.append('      reader.onload = function(e) {')
        lines.append('        try {')
        lines.append('          var data = JSON.parse(e.target.result);')
        lines.append('          _applyCommentData(data);')
        lines.append('        } catch(err) {')
        lines.append('          showToast("Error: could not parse JSON file.");')
        lines.append('        }')
        lines.append('      };')
        lines.append('      reader.readAsText(file);')
        lines.append('    }')
        lines.append('')
        lines.append('    function saveCommentsJSON() {')
        lines.append('      var now = new Date().toISOString();')
        lines.append('      var data = {};')
        lines.append('      document.querySelectorAll(".comment-box").forEach(function(ta) {')
        lines.append('        var blk = ta.getAttribute("data-block");')
        lines.append('        if (blk) data[blk] = {comment: ta.value, saved_at: now, user_status: ""};')
        lines.append('      });')
        lines.append('      document.querySelectorAll(".user-status-sel").forEach(function(sel) {')
        lines.append('        var blk = sel.getAttribute("data-block");')
        lines.append('        if (blk && data[blk]) data[blk].user_status = sel.value;')
        lines.append('      });')
        lines.append('      var payload = JSON.stringify(data, null, 2);')
        lines.append('      // Try to save directly to filesystem via local server ----------')
        lines.append('      fetch(COMMENTS_SERVER_URL, {')
        lines.append('        method: "POST",')
        lines.append('        headers: {"Content-Type": "application/json"},')
        lines.append('        body: payload')
        lines.append('      }).then(function(r) {')
        lines.append('        if (r.ok) {')
        lines.append('          showToast("Saved to: " + COMMENTS_SAVE_PATH);')
        lines.append('        } else {')
        lines.append('          throw new Error("server returned " + r.status);')
        lines.append('        }')
        lines.append('      }).catch(function() {')
        lines.append('        // Fall back: download to browser Downloads folder ----------')
        lines.append('        var blob = new Blob([payload], {type: "application/json"});')
        lines.append('        var a = document.createElement("a");')
        lines.append('        a.href = URL.createObjectURL(blob);')
        lines.append('        a.download = "block_comments.json";')
        lines.append('        a.click();')
        lines.append('        showToast("Server not running — downloaded to Downloads. Place at: " + COMMENTS_SAVE_PATH);')
        lines.append('      });')
        lines.append('    }')
        lines.append('')
        lines.append('    function csvEscape(val) {')
        lines.append('      var s = (val === null || val === undefined) ? "" : String(val);')
        lines.append('      if (s.indexOf(",") !== -1 || s.indexOf("\\n") !== -1 || s.indexOf(\'\"\') !== -1) {')
        lines.append('        return \'"\' + s.replace(/"/g, \'\"\"\') + \'"\';')
        lines.append('      }')
        lines.append('      return s;')
        lines.append('    }')
        lines.append('')
        lines.append('    function exportCommentsCSV() {')
        lines.append('      var thead = document.querySelector("#statusTable thead tr");')
        lines.append('      var thCells = thead ? Array.from(thead.querySelectorAll("th")) : [];')
        lines.append('      var headerRow = thCells.map(function(th) {')
        lines.append('        return csvEscape(th.textContent.replace(/[\u21c5\u2191\u2193]/g, "").trim());')
        lines.append('      }).join(",");')
        lines.append('      var csvRows = [headerRow];')
        lines.append('      var tbody = document.getElementById("tableBody");')
        lines.append('      var rows = tbody ? Array.from(tbody.querySelectorAll("tr:not(.hidden)")) : [];')
        lines.append('      rows.forEach(function(row) {')
        lines.append('        var cells = Array.from(row.querySelectorAll("td"));')
        lines.append('        var vals = cells.map(function(td) {')
        lines.append('          var ta = td.querySelector(".comment-box");')
        lines.append('          if (ta) return csvEscape(ta.value);')
        lines.append('          var sel = td.querySelector(".user-status-sel");')
        lines.append('          if (sel) { var sm = td.querySelector("small"); var rv = sm ? sm.textContent.trim() : ""; return csvEscape(sel.value ? sel.value : rv); }')
        lines.append('          return csvEscape(td.textContent.trim());')
        lines.append('        });')
        lines.append('        csvRows.push(vals.join(","));')
        lines.append('      });')
        lines.append('      var ts = new Date().toISOString().replace(/[:.]/g, "-").slice(0, 19);')
        lines.append('      var blob = new Blob([csvRows.join("\\n")], {type: "text/csv"});')
        lines.append('      var a = document.createElement("a");')
        lines.append('      a.href = URL.createObjectURL(blob);')
        lines.append('      a.download = "block_comments_" + ts + ".csv";')
        lines.append('      a.click();')
        lines.append('      showToast("CSV exported.");')
        lines.append('    }')
        lines.append('')
        lines.append('    window.onload = function(){')
        lines.append('      applyFilters();')
        lines.append('      // Try to load latest comments from server; fall back to embedded COMMENTS_INIT')
        lines.append('      var apiUrl = COMMENTS_SERVER_URL.replace("/api/save_comments", "/api/comments");')
        lines.append('      fetch(apiUrl)')
        lines.append('        .then(function(r){ return r.ok ? r.json() : Promise.reject(); })')
        lines.append('        .then(function(data) {')
        lines.append('          document.querySelectorAll(".comment-box").forEach(function(ta) {')
        lines.append('            var blk = ta.getAttribute("data-block");')
        lines.append('            if (blk && data[blk]) ta.value = data[blk].comment || data[blk];')
        lines.append('          });')
        lines.append('          document.querySelectorAll(".user-status-sel").forEach(function(sel) {')
        lines.append('            var blk = sel.getAttribute("data-block");')
        lines.append('            if (blk && data[blk] && typeof data[blk] === "object") {')
        lines.append('              sel.value = data[blk].user_status || "";')
        lines.append('              _updateStatusColor(sel);')
        lines.append('            }')
        lines.append('          });')
        lines.append('        })')
        lines.append('        .catch(function() {')
        lines.append('          // Server not running — populate from embedded snapshot')
        lines.append('          document.querySelectorAll(".comment-box").forEach(function(ta) {')
        lines.append('            var blk = ta.getAttribute("data-block");')
        lines.append('            if (blk && COMMENTS_INIT[blk]) ta.value = COMMENTS_INIT[blk].comment || COMMENTS_INIT[blk];')
        lines.append('          });')
        lines.append('          document.querySelectorAll(".user-status-sel").forEach(function(sel) {')
        lines.append('            var blk = sel.getAttribute("data-block");')
        lines.append('            if (blk && COMMENTS_INIT[blk] && typeof COMMENTS_INIT[blk] === "object") {')
        lines.append('              sel.value = COMMENTS_INIT[blk].user_status || "";')
        lines.append('              _updateStatusColor(sel);')
        lines.append('            }')
        lines.append('          });')
        lines.append('        });')
        lines.append('    };')
        lines.append('  </script>')
        lines.append('</body>')
        lines.append('</html>')

        with open(out_path, 'w', encoding='utf-8') as f:
            f.write('\n'.join(lines) + '\n')



def _make_comment_handler(run_location):
    """Return a BaseHTTPRequestHandler subclass bound to run_location."""
    _lock = threading.Lock()

    class _Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):
            pass  # suppress per-request console noise

        def _send(self, code, body=b'', ctype='application/json'):
            self.send_response(code)
            self.send_header('Content-Type', ctype)
            self.send_header('Access-Control-Allow-Origin', '*')
            self.send_header('Access-Control-Allow-Methods', 'GET, POST, OPTIONS')
            self.send_header('Access-Control-Allow-Headers', 'Content-Type')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            if body:
                self.wfile.write(body)

        def do_OPTIONS(self):
            self._send(204)

        def do_GET(self):
            path = urllib.parse.urlparse(self.path).path
            if path in ('/', ''):
                path = '/status.html'
            if path == '/ping':
                self._send(200, b'{"ok":true}')
                return
            if path == '/api/comments':
                cpath = comments_file_path(run_location)
                try:
                    if cpath.is_file():
                        with _lock:
                            with open(cpath, 'r', encoding='utf-8') as fh:
                                data = fh.read().encode('utf-8')
                        self._send(200, data)
                    else:
                        self._send(200, b'{}')
                except Exception as exc:
                    self._send(500, json.dumps({'error': str(exc)}).encode())
                return
            local = os.path.join(run_location, path.lstrip('/'))
            if os.path.isfile(local) and local.endswith('.html'):
                with open(local, 'rb') as fh:
                    data = fh.read()
                self._send(200, data, 'text/html; charset=utf-8')
            else:
                self._send(404, b'{"error":"not found"}')

        def do_POST(self):
            path = urllib.parse.urlparse(self.path).path
            if path == '/api/save_comments':
                length = int(self.headers.get('Content-Length', 0))
                body = self.rfile.read(length)
                try:
                    data = json.loads(body.decode('utf-8'))
                    cpath = comments_file_path(run_location)
                    with _lock:
                        with open(cpath, 'w', encoding='utf-8') as fh:
                            json.dump(data, fh, indent=2, ensure_ascii=False)
                    resp = json.dumps({'ok': True, 'path': str(cpath)}).encode()
                    self._send(200, resp)
                    print(f"[comments] Saved {len(data)} block(s) to {cpath}")
                except Exception as exc:
                    self._send(500, json.dumps({'error': str(exc)}).encode())
            else:
                self._send(404, b'{"error":"unknown endpoint"}')

    return _Handler


def serve_mode(run_location, port):
    """Start the minimal comment-save HTTP server and block until Ctrl-C."""
    handler = _make_comment_handler(run_location)
    server = http.server.HTTPServer(('localhost', port), handler)
    cpath = comments_file_path(run_location)
    sep = '=' * 62
    print(f"\n{sep}")
    print(f"  FEV Comment Server  →  http://localhost:{port}/status.html")
    print(f"  Comments saved to   →  {cpath}")
    print(f"  Press Ctrl+C to stop")
    print(f"{sep}\n")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nServer stopped.")


def comments_file_path(run_location):
    """Return path to the persistent block_comments.json file.

    Stored inside run_location (alongside the block dirs and HTML files).
    Safe from rotation because rotate_and_create_run_dir only touches
    run_location/<block_name>/ subdirs.
    """
    return Path(run_location) / 'block_comments.json'


def load_comments(run_location):
    """Load block comments from block_comments.json.

    Returns a dict: {block_name: {"comment": str, "saved_at": str}}.
    Returns {} if the file does not exist or cannot be parsed.
    """
    cpath = comments_file_path(run_location)
    if not cpath.is_file():
        return {}
    try:
        with open(cpath, 'r', encoding='utf-8') as f:
            data = json.load(f)
        if isinstance(data, dict):
            return data
    except Exception as e:
        print(f"WARNING: Could not load comments from {cpath}: {e}")
    return {}


