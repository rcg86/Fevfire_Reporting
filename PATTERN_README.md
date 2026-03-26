# Pattern Configuration Guide

## Overview

The `generateReport.py` script uses pattern files to determine what to extract as errors, warnings, and info messages, as well as what to ignore from log files.

## Pattern File Locations

### Global Pattern File
- **Location**: `pattern.yaml` in the script directory (or specify with `--pattern_file`)
- **Purpose**: Define default patterns for all blocks
- **Usage**: 
  ```bash
  python3 generateReport.py --run_location /path/to/runs --pattern_file /path/to/pattern.yaml
  ```

### Block-Specific Pattern File
- **Location**: `<run_location>/<block_name>/pattern.yaml`
- **Purpose**: Define additional patterns specific to a block
- **Behavior**: Block patterns **extend** (not replace) global patterns

## Pattern File Format

```yaml
# Error patterns - flagged as RED
error_patterns:
  - '\s+//\s+Error:'
  - 'FAILED'
  - 'Abort'

# Warning patterns - flagged as ORANGE
warning_patterns:
  - 'WARNING'
  - 'Warning:'

# Info patterns - flagged as YELLOW
info_patterns:
  - 'INFO:'
  - 'Note:'

# Ignore patterns - completely skipped
ignore_patterns:
  - 'Debug:'
  - '^\s*$'  # Empty lines
```

## Pattern Syntax

All patterns use **Python regular expressions** (regex). Examples:

| Pattern | Description |
|---------|-------------|
| `ERROR` | Matches the word ERROR anywhere in the line |
| `^\s*ERROR` | Matches ERROR at the start of the line (with optional whitespace) |
| `\s+//\s+Error:` | Matches "// Error:" with surrounding whitespace |
| `Missing:` | Matches "Missing:" anywhere in the line |
| `^\s*$` | Matches empty lines |
| `WARN\|WARNING` | Matches either WARN or WARNING |

## How Patterns Work

1. **Priority**: Ignore patterns are checked first
   - If a line matches an ignore pattern, it's skipped completely
   
2. **Pattern Checking**: For non-ignored lines:
   - Check against error patterns → Flag as ERROR (Red)
   - Check against warning patterns → Flag as WARNING (Orange)
   - Check against info patterns → Flag as INFO (Yellow)
   - No matches → Line is not reported

3. **Block-Specific Patterns**: 
   - Block patterns are **added** to global patterns
   - Both sets of patterns are applied together

## Report Patterns and Hierarchical Sub-Patterns

`report_patterns` extract multi-line structured sections from log files using `re.DOTALL` regex and render them with colored, formatted titles.

### Basic report_pattern entry

```yaml
report_patterns:
  - name: 'Overview'
    pattern: 'Running Module (\S+) and (\S+).*?Processed (\d+) out of (\d+) module pairs\s+EQ: (\d+)\s+NEQ: (\d+)\s+ABORT: (\d+)'
    title_format: '{occurrence} Golden: {group1}  Revised: {group2}  {group3}/{group4}  EQ={group5} NEQ={group6} ABORT={group7}'
    color_rules:
      - condition: 'group6 > 0 or group7 > 0'
        color: 'red'
      - condition: 'group6 == 0 and group7 == 0'
        color: 'green'
```

### Hierarchical Sub-Patterns (`subpatterns`)

Sub-patterns are **child regexes applied to the main match text only** — not the full log file. They are declared as a `subpatterns` list; each child may itself nest a deeper `subpatterns` list, making the hierarchy arbitrarily deep.

#### YAML schema

```
- name: 'My Pattern'
  pattern: '<main regex>'         # capture groups → {group1}, {group2}, ...
  subpatterns:
    - pattern: '<child 1 regex>'  # → pattern1.group1, pattern1.group2, ...
      subpatterns:
        - pattern: '<grandchild regex>'  # → pattern1.pattern1.group1, ...
    - pattern: '<child 2 regex>'  # → pattern2.group1, pattern2.group2, ...
  title_format: '...'
  color_rules: [...]
```

Children are numbered by their position in the list: the first item is always `pattern1`, the second `pattern2`, and so on.

#### Referencing child groups

| Level | In `title_format` | In `color_rules` condition |
|-------|-------------------|---------------------------|
| Main pattern | `{group1}` | `group1` |
| Child 1 | `{pattern1.group2}` | `pattern1.group2` |
| Child 2 | `{pattern2.group1}` | `pattern2.group1` |
| Grandchild 1 of child 1 | `{pattern1.pattern1.group3}` | `pattern1.pattern1.group3` |

The `matched` boolean is also available: `pattern1.matched`.

#### Conditional blocks (optional children)

Use the `{?key|content}` syntax to collapse a block when a value is empty (i.e. child pattern did not match):

```
{?pattern2.group1|Processed: {pattern2.group1}}
```

#### Unmatched children

If a child pattern does **not** match inside the parent match text:
- All `groupN` attributes resolve to `''` (empty string) — never raises an error.
- `matched` is `False`.
- Conditions like `pattern2.group1 == 0` evaluate as `'' == 0 → False` (safe).
- Grandchildren of an unmatched child also degrade silently.

#### Full example

```yaml
report_patterns:
  - name: 'Overview'
    pattern: 'Running Module (\S+) and (\S+).*?Processed (\d+) out of (\d+) module pairs\s+EQ: (\d+)\s+NEQ: (\d+)\s+ABORT: (\d+)'
    subpatterns:
      # pattern1 — always expected inside the main match
      - pattern: 'EQ:\s+(\d+)\s+NEQ:\s+(\d+)\s+ABORT:\s+(\d+)'
      # pattern2 — may or may not appear; silently empty when absent
      - pattern: 'Processed\s+(\d+)\s+out of\s+(\d+)'
      # pattern3 — with a grandchild
      - pattern: '===.*?Unmapped points:.*?Golden:.*?Unreachable\s+(\d+)\s+(\d+)\s+(\d+)(.*?)==='
        subpatterns:
          - pattern: 'Not-mapped\s+(\d+)'  # → pattern3.pattern1.group1
    title_format: >-
      {occurrence}
      Golden: {group1} :::: Revised: {group2}
      {group3}/{group4}
      EQ={group5} : NONEQ={group6} : ABORT={group7}
      ABORT count from pattern1: {pattern1.group3}
      Processed from pattern2: {pattern2.group1} of {pattern2.group2}
      Unmapped Golden: {pattern3.group3}
      Not-mapped (grandchild): {pattern3.pattern1.group1}
    color_rules:
      - condition: 'group6 > 0 or group7 > 0'
        color: 'red'
      # Use child group in a color condition
      - condition: 'pattern1.group2 == 0 and group7 == 0'
        color: 'green'
```

#### How scoping works

```
Full log file
└─ main pattern match.group(0)
   ├─ pattern1 searches HERE only   (child scoped to parent's match text)
   ├─ pattern2 searches HERE only
   └─ pattern3 searches HERE only
      └─ pattern3.pattern1 searches inside pattern3 match.group(0) only
```

Child patterns **cannot** accidentally match content outside their parent match, even when the same text repeats elsewhere in the log.

## Example Scenarios

### Scenario 1: Global Patterns Only
```bash
# Use default pattern.yaml in script directory
python3 generateReport.py --run_location /path/to/runs
```

### Scenario 2: Custom Global Patterns
```bash
# Use custom pattern file
python3 generateReport.py --run_location /path/to/runs --pattern_file /custom/pattern.yaml
```

### Scenario 3: Block-Specific Patterns
Create `<run_location>/myblock/pattern.yaml`:
```yaml
# Additional patterns for myblock only
error_patterns:
  - 'MYBLOCK_SPECIFIC_ERROR'

ignore_patterns:
  - 'Known issue in myblock'
```

This will use global patterns + myblock-specific patterns when analyzing myblock.

## Best Practices

1. **Start with defaults**: Use the provided `pattern.yaml` as a template
2. **Use ignore patterns liberally**: Filter out known false positives
3. **Test patterns**: Run the report on a small set first
4. **Document block-specific patterns**: Add comments explaining why patterns are needed
5. **Use anchors**: Use `^` and `$` for more precise matching

## Examples

### Ignore Expected Messages
```yaml
ignore_patterns:
  - 'This is an expected warning'
  - 'Debug information'
  - 'FYI.*not a real issue'
```

### Catch Specific Error Formats
```yaml
error_patterns:
  - 'Error \d+:'  # Matches "Error 123:"
  - 'FAILURE.*comparison'  # Matches "FAILURE during comparison"
  - '^\*\*\* Error'  # Matches "*** Error" at start of line
```

### Block-Specific Tuning
For a block that has noisy warnings:
```yaml
# In <run_location>/noisyblock/pattern.yaml
ignore_patterns:
  - 'WARNING: Clock domain.*expected'
  - 'Known timing issue'
```
