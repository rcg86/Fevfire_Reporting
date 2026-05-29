#!/usr/bin/env python3
"""
correctFeedthroughNetlist.py

Read a Verilog netlist and produce a feedthrough netlist that contains:
  - The top-level module with its ports and wires intact, but with all
    instance statements removed EXCEPT instances of the user-specified
    "keep" module.
  - The definition of the user-specified "keep" module.
All other module definitions and all other instances are stripped.
"""

import argparse
import re
import sys


MODULE_RE = re.compile(r'\bmodule\b\s+(\w+)\b', re.MULTILINE)
ENDMODULE_RE = re.compile(r'\bendmodule\b', re.MULTILINE)


def strip_comments(text):
    text = re.sub(r'/\*.*?\*/', '', text, flags=re.DOTALL)
    text = re.sub(r'//[^\n]*', '', text)
    return text


def split_modules(text):
    """Yield (module_name, full_module_text) for each module in text."""
    modules = []
    pos = 0
    while True:
        m = MODULE_RE.search(text, pos)
        if not m:
            break
        name = m.group(1)
        start = m.start()
        end_match = ENDMODULE_RE.search(text, m.end())
        if not end_match:
            raise ValueError(f"Module '{name}' has no matching endmodule")
        end = end_match.end()
        modules.append((name, text[start:end]))
        pos = end
    return modules


STATEMENT_KEYWORDS = (
    'input', 'output', 'inout',
    'wire', 'reg', 'tri', 'wand', 'wor',
    'supply0', 'supply1',
    'parameter', 'localparam',
    'assign', 'defparam', 'specify', 'generate',
    'always', 'initial', 'function', 'task',
    'genvar', 'integer', 'real', 'time',
)


def split_top_statements(body):
    """Split a module body into top-level statements (depth-0 ';' or block ends).

    Tracks (), [], {} and begin/end / case/endcase / generate/endgenerate
    nesting so semicolons inside those don't terminate a statement.
    """
    stmts = []
    buf = []
    i = 0
    paren = 0
    bracket = 0
    brace = 0
    n = len(body)
    while i < n:
        c = body[i]
        if c == '(':
            paren += 1
        elif c == ')':
            paren -= 1
        elif c == '[':
            bracket += 1
        elif c == ']':
            bracket -= 1
        elif c == '{':
            brace += 1
        elif c == '}':
            brace -= 1
        buf.append(c)
        if c == ';' and paren == 0 and bracket == 0 and brace == 0:
            stmts.append(''.join(buf))
            buf = []
        i += 1
    tail = ''.join(buf)
    if tail.strip():
        stmts.append(tail)
    return stmts


IDENT = r'(?:\\\S+|\w+)'  # plain or escaped Verilog identifier

INSTANCE_RE = re.compile(
    r'^\s*(' + IDENT + r')\s+'      # module type
    r'(?:#\s*\([^;]*?\)\s*)?'       # optional parameter override
    r'(' + IDENT + r')\s*'          # instance name (may be escaped)
    r'(?:\[[^\]]*\]\s*)?'           # optional instance array range
    r'\(',                          # opening paren of port list
    re.DOTALL,
)


def classify_statement(stmt):
    """Return ('decl'|'instance'|'other', module_type_or_None)."""
    stripped = stmt.lstrip()
    # strip attributes (* ... *)
    while stripped.startswith('(*'):
        end = stripped.find('*)')
        if end == -1:
            break
        stripped = stripped[end + 2:].lstrip()
    first_word_m = re.match(r'(\w+)', stripped)
    if not first_word_m:
        return ('other', None)
    first = first_word_m.group(1)
    if first in STATEMENT_KEYWORDS:
        return ('decl', None)
    m = INSTANCE_RE.match(stripped)
    if m:
        return ('instance', m.group(1))
    return ('other', None)


def filter_top_module(mod_text, keep_module):
    """Remove every instance from mod_text except instances of keep_module."""
    header_end = mod_text.find(';')
    if header_end == -1:
        raise ValueError("Top module header has no ';'")
    header = mod_text[:header_end + 1]

    endm = ENDMODULE_RE.search(mod_text)
    body = mod_text[header_end + 1:endm.start()]
    tail = mod_text[endm.start():]

    out_stmts = []
    for stmt in split_top_statements(body):
        kind, mtype = classify_statement(stmt)
        if kind == 'instance' and mtype != keep_module:
            continue
        out_stmts.append(stmt)

    return header + ''.join(out_stmts) + '\n' + tail


def main():
    ap = argparse.ArgumentParser(
        description="Strip a Verilog netlist down to the top module (ports/wires "
                    "intact) plus a single kept sub-module and its instances."
    )
    ap.add_argument('--netlist', required=True, help='Input Verilog netlist')
    ap.add_argument('--top', required=True, help='Top-level module name')
    ap.add_argument('--keep-module', required=True,
                    help='Module to keep (along with its instance in top)')
    ap.add_argument('--output', default='feedthrough.v',
                    help='Output netlist (default: feedthrough.v)')
    args = ap.parse_args()

    with open(args.netlist) as f:
        raw = f.read()

    cleaned = strip_comments(raw)
    modules = split_modules(cleaned)
    by_name = {name: text for name, text in modules}

    if args.top not in by_name:
        sys.exit(f"ERROR: top module '{args.top}' not found in {args.netlist}")
    if args.keep_module not in by_name:
        sys.exit(f"ERROR: keep module '{args.keep_module}' not found in {args.netlist}")

    new_top = filter_top_module(by_name[args.top], args.keep_module)
    keep_def = by_name[args.keep_module]

    with open(args.output, 'w') as f:
        f.write("// Generated by correctFeedthroughNetlist.py\n")
        f.write(f"// Source : {args.netlist}\n")
        f.write(f"// Top    : {args.top}\n")
        f.write(f"// Kept   : {args.keep_module}\n\n")
        f.write(new_top.strip() + '\n\n')
        f.write(keep_def.strip() + '\n')

    print(f"Wrote feedthrough netlist: {args.output}")


if __name__ == '__main__':
    main()
