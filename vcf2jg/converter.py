"""
Convert Synopsys VC Formal Tcl scripts to Cadence JasperGold Tcl scripts.

Best-effort translator covering the two common VC Formal FPV dialects:

1. LUBIS / modern VC Formal:
   analyze / elaborate / create_clock / create_reset -sense /
   sim_force / sim_run / sim_save_reset / check_fv

2. Classic / public FPV flows:
   read_file / create_reset -high|-low / fvassume / report_fv

Output targets the JasperGold FPV style used in LUBIS trainee labs:

    clear -all
    analyze -sv09 <files>
    elaborate -top <top> -create_related_covers {precondition witness}
    clock <clk>
    reset -none
    assume -reset <active_expr>
    prove -all

Not a full Tcl interpreter. Unmapped commands become [vcf2jg TODO] comments.
Compatible with Python 3.6+.
"""

import re
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Tuple


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

class ConversionResult(object):
    """Output of a conversion plus diagnostic messages."""

    def __init__(self, jasper_tcl, warnings=None, mapped_count=0,
                 skipped_count=0, unmapped_count=0):
        self.jasper_tcl = jasper_tcl
        self.warnings = warnings if warnings is not None else []
        self.mapped_count = mapped_count
        self.skipped_count = skipped_count
        self.unmapped_count = unmapped_count


class _Ctx(object):
    """Mutable conversion state."""

    def __init__(self):
        self.vars = {}  # type: Dict[str, str]
        self.top = None  # type: Optional[str]
        self.filelist = None  # type: Optional[str]
        self.analyze_done = False
        self.elaborate_done = False
        self.clock_emitted = False
        self.reset_emitted = False
        self.clear_emitted = False
        self.warnings = []  # type: List[str]
        self.mapped = 0
        self.skipped = 0
        self.unmapped = 0
        self.out = []  # type: List[str]

        # App mode / coverage
        self.appmode = "FPV"
        self.want_cov = False
        self.want_witness_covers = True
        self.cov_init_emitted = False

        # Reset: LUBIS style reset -none + assume -reset <expr>
        # expr is active-condition: "rst" (high) or "!rst_n" / "~rst_n" (low)
        self.reset_assume_expr = None  # type: Optional[str]
        self.reset_style = "lubis"  # lubis | classic
        self.classic_reset_cmd = None  # type: Optional[str]

        # Elaboration extras
        self.bbox_cells = []  # type: List[str]
        self.elab_params = []  # type: List[Tuple[str, str]]
        self.disable_auto_bbox = False
        self.pending_elaborate_top = False

        # Analyze language default (trainee hand scripts use -sv09)
        self.analyze_lang = "-sv09"

        # Simple if-stack: True = execute, False = skip
        self.if_stack = []  # type: List[bool]
        self.if_taken_stack = []  # type: List[bool]  # whether a branch already ran


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_VAR_REF_RE = re.compile(r"\$(\w+)|\$\{(\w+)\}")
_FILE_RE = re.compile(
    r"(?:^|[\s\[\{])((?:\$[\w{}]+/)?[\w./+-]+\.(?:sv|v|svh|vh|f))\b"
)
_PVALUE_RE = re.compile(r"-pvalue\+([A-Za-z_]\w*)=(\S+)")
_INCDIR_RE = re.compile(r"\+incdir\+(\S+)")
_DEFINE_RE = re.compile(r"\+define\+(\S+)")


def _strip_inline_comment(line):
    # type: (str) -> str
    """Remove Tcl # comments outside braces/quotes/brackets."""
    in_brace = in_bracket = 0
    in_quote = False
    for i, ch in enumerate(line):
        if ch == '"' and (i == 0 or line[i - 1] != "\\"):
            in_quote = not in_quote
        elif not in_quote:
            if ch == "{":
                in_brace += 1
            elif ch == "}":
                in_brace = max(0, in_brace - 1)
            elif ch == "[":
                in_bracket += 1
            elif ch == "]":
                in_bracket = max(0, in_bracket - 1)
            elif ch == "#" and in_brace == 0 and in_bracket == 0:
                if i == 0 or line[i - 1].isspace():
                    return line[:i].rstrip()
    return line.rstrip()


def _is_cmd_sub(val):
    # type: (str) -> bool
    """True if value is a Tcl command substitution like [file dirname ...]."""
    v = str(val).strip()
    return len(v) >= 2 and v[0] == "[" and v[-1] == "]"


def _expand_vars(text, variables, expand_cmd_sub=False):
    # type: (str, dict, bool) -> str
    """
    Expand $var / ${var}. By default, variables whose values are Tcl command
    substitutions ([...]) are left as $var so `cd $SCRIPT_LOCATION` stays valid.
    """

    def repl(m):
        name = m.group(1) or m.group(2)
        if name in variables:
            val = str(variables[name])
            if not expand_cmd_sub and _is_cmd_sub(val):
                return m.group(0)
            return val
        return m.group(0)

    return _VAR_REF_RE.sub(repl, text)


def _tokenize(line):
    # type: (str) -> List[str]
    """Lightweight Tcl-ish tokenization (braces, quotes, brackets as words)."""
    s = line.strip()
    if not s:
        return []
    parts = []  # type: List[str]
    i = 0
    n = len(s)
    while i < n:
        while i < n and s[i].isspace():
            i += 1
        if i >= n:
            break
        if s[i] == "{":
            depth = 1
            j = i + 1
            while j < n and depth:
                if s[j] == "{":
                    depth += 1
                elif s[j] == "}":
                    depth -= 1
                j += 1
            parts.append(s[i:j])
            i = j
        elif s[i] == "[":
            depth = 1
            j = i + 1
            while j < n and depth:
                if s[j] == "[":
                    depth += 1
                elif s[j] == "]":
                    depth -= 1
                j += 1
            parts.append(s[i:j])
            i = j
        elif s[i] == '"':
            j = i + 1
            while j < n:
                if s[j] == "\\" and j + 1 < n:
                    j += 2
                    continue
                if s[j] == '"':
                    j += 1
                    break
                j += 1
            parts.append(s[i:j])
            i = j
        else:
            j = i
            while j < n and not s[j].isspace():
                j += 1
            parts.append(s[i:j])
            i = j
    return parts


def _unbrace(tok):
    # type: (str) -> str
    if len(tok) >= 2 and tok[0] == "{" and tok[-1] == "}":
        return tok[1:-1]
    if len(tok) >= 2 and tok[0] == '"' and tok[-1] == '"':
        return tok[1:-1]
    if len(tok) >= 2 and tok[0] == "[" and tok[-1] == "]":
        return tok[1:-1]
    return tok


def _get_opt(tokens, flag, default=None):
    # type: (Sequence[str], str, Optional[str]) -> Optional[str]
    for i, t in enumerate(tokens):
        if t == flag and i + 1 < len(tokens):
            return _unbrace(tokens[i + 1])
    return default


def _has_flag(tokens, flag):
    # type: (Sequence[str], str) -> bool
    return flag in tokens


def _balance_delta(s, open_c, close_c):
    # type: (str, str, str) -> int
    """Net open-count, ignoring content in quotes."""
    depth = 0
    in_quote = False
    for i, ch in enumerate(s):
        if ch == '"' and (i == 0 or s[i - 1] != "\\"):
            in_quote = not in_quote
            continue
        if in_quote:
            continue
        if ch == open_c:
            depth += 1
        elif ch == close_c:
            depth -= 1
    return depth


def _join_logical_lines(lines):
    # type: (Iterable[str]) -> List[str]
    """
    Join physical lines into logical Tcl commands:
    - backslash continuations
    - multi-line {...} and [...] groups
    """
    # Pass 1: backslash join
    cont = []  # type: List[str]
    buf = ""
    for raw in lines:
        line = raw.rstrip("\n")
        if buf:
            line = line.lstrip()
        if line.endswith("\\") and not line.endswith("\\\\"):
            buf += line[:-1]
            continue
        buf += line
        cont.append(buf)
        buf = ""
    if buf:
        cont.append(buf)

    # Pass 2: brace/bracket balance join
    logical = []  # type: List[str]
    acc = ""
    brace = bracket = 0
    for line in cont:
        if not acc:
            acc = line
        else:
            acc = acc + "\n" + line
        brace += _balance_delta(line, "{", "}")
        bracket += _balance_delta(line, "[", "]")
        if brace <= 0 and bracket <= 0:
            logical.append(acc)
            acc = ""
            brace = bracket = 0
    if acc:
        logical.append(acc)
    return logical


def _emit(ctx, line=""):
    # type: (_Ctx, str) -> None
    ctx.out.append(line)


def _map(ctx, line):
    # type: (_Ctx, str) -> None
    ctx.mapped += 1
    _emit(ctx, line)


def _skip(ctx, reason, original):
    # type: (_Ctx, str, str) -> None
    ctx.skipped += 1
    _emit(ctx, "# [vcf2jg skip] {}".format(reason))
    for ol in original.splitlines():
        _emit(ctx, "# {}".format(ol))


def _unmapped(ctx, original, hint=""):
    # type: (_Ctx, str, str) -> None
    ctx.unmapped += 1
    msg = "Unmapped command: {}".format(original.splitlines()[0][:120])
    if hint:
        msg += " ({})".format(hint)
    ctx.warnings.append(msg)
    _emit(ctx, "# [vcf2jg TODO] {}".format(hint or "manual review needed"))
    for ol in original.splitlines():
        _emit(ctx, "# {}".format(ol))


def _active():
    """Whether current if-stack allows emission (module-level helper via ctx)."""
    return True  # replaced by ctx method below — kept for clarity


def _in_active_branch(ctx):
    # type: (_Ctx) -> bool
    return all(ctx.if_stack) if ctx.if_stack else True


def _ensure_clear(ctx):
    # type: (_Ctx) -> None
    if not ctx.clear_emitted:
        _emit(ctx, "clear -all")
        ctx.clear_emitted = True


def _truthy(val):
    # type: (str) -> bool
    v = str(val).strip().lower()
    if v in ("0", "false", "no", "off", ""):
        return False
    if v in ("1", "true", "yes", "on"):
        return True
    # non-empty string treated as true
    try:
        return int(v) != 0
    except ValueError:
        return bool(v)


def _eval_condition(ctx, cond):
    # type: (_Ctx, str) -> Optional[bool]
    """
    Evaluate simple Tcl conditions used in LUBIS scripts.
    Returns None if unknown (keep both branches commented).
    """
    c = cond.strip()

    # string equal [info hostname] X  → unknown (host-specific)
    if "info hostname" in c or "info script" in c:
        return None

    # $var  (resolve before expanding so we still see the name)
    m = re.fullmatch(r"\$(\w+)", c)
    if m:
        name = m.group(1)
        if name in ctx.vars:
            return _truthy(ctx.vars[name])
        return None

    # $a && $b
    m = re.fullmatch(r"\$(\w+)\s*&&\s*\$(\w+)", c)
    if m:
        a, b = m.group(1), m.group(2)
        if a in ctx.vars and b in ctx.vars:
            return _truthy(ctx.vars[a]) and _truthy(ctx.vars[b])
        return None

    # $a || $b
    m = re.fullmatch(r"\$(\w+)\s*\|\|\s*\$(\w+)", c)
    if m:
        a, b = m.group(1), m.group(2)
        if a in ctx.vars and b in ctx.vars:
            return _truthy(ctx.vars[a]) or _truthy(ctx.vars[b])
        return None

    # Expand remaining vars and try numeric / bare literal
    expanded = _expand_vars(c, ctx.vars)
    if re.fullmatch(r"-?\d+", expanded.strip()):
        return _truthy(expanded.strip())
    if expanded.strip().lower() in ("true", "false", "yes", "no", "on", "off"):
        return _truthy(expanded.strip())

    # [string equal $x y] etc. — skip
    if c.startswith("[") or expanded.startswith("["):
        return None

    return None


def _extract_files_from_text(text):
    # type: (str) -> List[str]
    """Pull HDL / filelist paths out of a VC Formal -vcs blob or brace list."""
    files = []  # type: List[str]
    # Prefer word-like paths ending in known extensions
    for m in re.finditer(
        r"(?:\$[\w{}]+/|[\w./+-])*[\w.+-]+\.(?:sv|v|svh|vh|f)\b", text
    ):
        f = m.group(0)
        # skip pure flags
        if f.startswith("+") or f.startswith("-"):
            continue
        files.append(f)
    return files


def _parse_vcs_blob(vcs):
    # type: (str) -> Tuple[List[str], List[str]]
    """
    Parse VC Formal -vcs string into (analyze_extra_flags, files).
    Handles -assert svaext, +incdir+, +define+, -f, bare files.
    """
    extras = []  # type: List[str]
    files = []  # type: List[str]

    for m in _INCDIR_RE.finditer(vcs):
        extras.append("+incdir+{}".format(m.group(1)))
    for m in _DEFINE_RE.finditer(vcs):
        extras.append("+define+{}".format(m.group(1)))

    # -f filelist
    m = re.search(r"-f\s+(\S+)", vcs)
    if m:
        files.append(m.group(1).strip("'\""))

    # strip known flags then collect files
    cleaned = vcs
    cleaned = re.sub(r"-assert\s+\S+", " ", cleaned)
    cleaned = re.sub(r"\+incdir\+\S+", " ", cleaned)
    cleaned = re.sub(r"\+define\+\S+", " ", cleaned)
    cleaned = re.sub(r"-f\s+\S+", " ", cleaned)
    files.extend(_extract_files_from_text(cleaned))

    # de-dup preserve order
    seen = set()
    uniq_files = []
    for f in files:
        if f not in seen:
            seen.add(f)
            uniq_files.append(f)
    return extras, uniq_files


def _parse_pvalues(text):
    # type: (str) -> List[Tuple[str, str]]
    return [(m.group(1), m.group(2).rstrip("\"'")) for m in _PVALUE_RE.finditer(text)]


def _fmt_lang(fmt):
    # type: (Optional[str]) -> str
    if not fmt:
        return "-sv09"
    f = fmt.lower()
    if "vhdl" in f:
        return "-vhdl"
    if f in ("verilog", "v2k", "v95"):
        return "-v2k"
    if "sv12" in f or "sverilog12" in f:
        return "-sv12"
    if "sv17" in f:
        return "-sv17"
    # default SystemVerilog for trainee / VC Formal sverilog
    return "-sv09"


def _reset_expr_from_force(signal, value):
    # type: (str, str) -> str
    """
    sim_force rst -apply 1'b1  → active-high → assume -reset rst
    sim_force rst_n -apply 1'b0 → active-low  → assume -reset !rst_n
    """
    v = value.replace(" ", "").lower()
    active_high_val = v in ("1", "1'b1", "1'h1", "1'b1", "true")
    active_low_val = v in ("0", "1'b0", "1'h0", "false")
    if active_high_val:
        return signal
    if active_low_val:
        return "!{}".format(signal)
    # unknown value: treat non-zero as active level of signal
    return signal


def _emit_reset_block(ctx):
    # type: (_Ctx) -> None
    if ctx.reset_emitted:
        return
    if ctx.reset_assume_expr:
        _map(ctx, "reset -none")
        _map(ctx, "assume -reset {}".format(ctx.reset_assume_expr))
        ctx.reset_emitted = True
    elif ctx.classic_reset_cmd:
        _map(ctx, ctx.classic_reset_cmd)
        ctx.reset_emitted = True


def _emit_cov_init(ctx):
    # type: (_Ctx) -> None
    if ctx.want_cov and not ctx.cov_init_emitted:
        _map(
            ctx,
            "check_cov -init -type all -model "
            "{branch toggle statement expression fsm property}",
        )
        ctx.cov_init_emitted = True


# ---------------------------------------------------------------------------
# Command handlers
# ---------------------------------------------------------------------------

def _handle_if_structure(ctx, tokens, original):
    # type: (_Ctx, List[str], str) -> bool
    """Handle if / elseif / else / closing braces for simple control flow."""
    cmd = tokens[0]

    # standalone closing brace of an if body — already consumed by join;
    # but lines that are only "}" appear after multi-line if joined as one unit.
    # With brace-joining, whole if {...} is one logical line. Handle that form.
    if cmd == "if":
        # if {$cond} { body }  [elseif {$c2} { body2 }]... [else { body3 }]
        return _handle_if_block(ctx, original)

    return False


def _split_if_chain(text):
    # type: (str) -> List[Tuple[str, Optional[str], str]]
    """
    Split `if {c} {b} elseif {c2} {b2} else {b3}` into
    list of (kind, condition|None, body).
    kind in if|elseif|else
    """
    s = text.strip()
    parts = []  # type: List[Tuple[str, Optional[str], str]]
    i = 0
    n = len(s)

    def skip_ws(j):
        while j < n and s[j].isspace():
            j += 1
        return j

    def read_braced(j):
        j = skip_ws(j)
        if j >= n or s[j] != "{":
            return None, j
        depth = 1
        k = j + 1
        while k < n and depth:
            if s[k] == "{":
                depth += 1
            elif s[k] == "}":
                depth -= 1
            k += 1
        return s[j + 1 : k - 1], k

    i = skip_ws(i)
    # leading "if"
    if not s.startswith("if"):
        return parts
    i = 2
    cond, i = read_braced(i)
    body, i = read_braced(i)
    if cond is not None and body is not None:
        parts.append(("if", cond, body))
    while True:
        i = skip_ws(i)
        if i >= n:
            break
        if s.startswith("elseif", i):
            i += 6
            cond, i = read_braced(i)
            body, i = read_braced(i)
            if cond is not None and body is not None:
                parts.append(("elseif", cond, body))
            continue
        if s.startswith("else", i):
            i += 4
            body, i = read_braced(i)
            if body is not None:
                parts.append(("else", None, body))
            break
        break
    return parts


def _handle_if_block(ctx, original):
    # type: (_Ctx, str) -> bool
    chain = _split_if_chain(original)
    if not chain:
        return False

    # Try to pick exactly one branch
    chosen_body = None  # type: Optional[str]
    known = True
    for kind, cond, body in chain:
        if kind in ("if", "elseif"):
            val = _eval_condition(ctx, cond or "")
            if val is None:
                known = False
                break
            if val:
                chosen_body = body
                break
        else:  # else
            chosen_body = body
            break

    if known and chosen_body is not None:
        # Execute chosen body lines
        for line in _join_logical_lines(chosen_body.splitlines()):
            _dispatch(ctx, line)
        ctx.mapped += 1
        return True

    if known and chosen_body is None:
        # all conditions false, no else
        ctx.skipped += 1
        _emit(ctx, "# [vcf2jg skip] if-chain: no branch taken")
        return True

    # Unknown condition: comment whole construct, still try to convert both
    # reset-related branches heuristically
    _emit(ctx, "# [vcf2jg] unresolved if — expanding branches as comments/guess")
    for kind, cond, body in chain:
        label = kind if cond is None else "{} {{{}}}".format(kind, cond)
        _emit(ctx, "# --- {} ---".format(label))
        for line in _join_logical_lines(body.splitlines()):
            stripped = _strip_inline_comment(line).strip()
            if not stripped or stripped.startswith("#"):
                continue
            # Prefer converting reset-related commands even inside unknown if
            tokens = _tokenize(_expand_vars(stripped, ctx.vars))
            if tokens and tokens[0] in (
                "sim_force",
                "create_reset",
                "check_fv",
                "exit",
                "sim_run",
                "sim_save_reset",
            ):
                _dispatch(ctx, line)
            else:
                _emit(ctx, "# {}".format(stripped))
    ctx.mapped += 1
    return True


def _handle_set(ctx, tokens, original):
    # type: (_Ctx, List[str], str) -> bool
    if len(tokens) < 2 or tokens[0].lower() != "set":
        return False
    name = tokens[1]
    raw_value = " ".join(tokens[2:]) if len(tokens) > 2 else ""
    # Keep command-substitutions intact for script_path / SCRIPT_LOCATION
    if raw_value.startswith("[") or "file dirname" in raw_value or "info script" in raw_value:
        ctx.vars[name] = raw_value  # unexpanded cmd sub
        _map(ctx, "set {} {}".format(name, raw_value))
        return True

    value = _unbrace(raw_value)
    value = _expand_vars(value, ctx.vars)
    ctx.vars[name] = value

    if name in ("design", "top", "TOP", "DESIGN"):
        ctx.top = value
        _emit(ctx, "# VC Formal: set {} {}  →  elaborate -top".format(name, value))
        ctx.mapped += 1
        return True

    # Control flags used by LUBIS templates — keep as set for readability
    if name in (
        "auto_check",
        "auto_witness",
        "exit_after_execution",
        "reset_without_constraint",
        "ipc_run",
        "SCRIPT_LOCATION",
        "script_path",
        "elaborateOption",
    ):
        if name == "elaborateOption":
            _emit(ctx, "# VC Formal elaborate option: {}".format(value))
            ctx.mapped += 1
            return True
        _map(ctx, "set {} {}".format(name, value))
        return True

    if re.match(r"^[A-Za-z_]\w*$", name):
        # Don't pollute JG with intermediate VC-only vars if value is a flag
        if value.startswith("-") and name.lower().endswith("option"):
            _emit(ctx, "# set {} {}".format(name, value))
            ctx.mapped += 1
            return True
        _map(ctx, "set {} {{{}}}".format(name, value))
        return True

    _skip(ctx, "set {} (not needed for JasperGold)".format(name), original)
    return True


def _handle_set_app_var(ctx, tokens, original):
    # type: (_Ctx, List[str], str) -> bool
    if tokens[0] != "set_app_var":
        return False
    name = tokens[1] if len(tokens) > 1 else ""
    if "cov" in name.lower():
        ctx.want_cov = True
    _skip(ctx, "VC Formal app var (no JasperGold equivalent)", original)
    return True


def _handle_set_fml_var(ctx, tokens, original):
    # type: (_Ctx, List[str], str) -> bool
    if tokens[0] != "set_fml_var":
        return False
    name = tokens[1] if len(tokens) > 1 else ""
    val = tokens[2] if len(tokens) > 2 else ""
    if "witness" in name and _truthy(val):
        ctx.want_witness_covers = True
    if "cov" in name:
        ctx.want_cov = True
    _skip(ctx, "VC Formal fml_var (no JasperGold equivalent)", original)
    return True


def _handle_set_fml_appmode(ctx, tokens, original):
    # type: (_Ctx, List[str], str) -> bool
    if tokens[0] != "set_fml_appmode":
        return False
    mode = tokens[1].upper() if len(tokens) > 1 else "FPV"
    ctx.appmode = mode
    app_map = {
        "FPV": "Formal Property Verification (default FPV flow)",
        "AEP": "Auto formal / structural checks — review JasperGold Auto Formal",
        "COV": "Coverage → JasperGold check_cov flow",
        "FCA": "Formal Coverage Analyzer → JasperGold check_cov",
        "FTA": "Fault Tree Analysis — no 1:1 JasperGold FPV command",
        "SEQ": "Sequential equivalence → JasperGold SEC app",
        "CCAP": "Connectivity apps — review JasperGold apps",
    }
    note = app_map.get(mode, "App mode {}".format(mode))
    _emit(ctx, "# VC Formal appmode: {} → {}".format(mode, note))
    if mode in ("COV", "FCA"):
        ctx.want_cov = True
    if mode not in ("FPV", "COV", "FCA"):
        ctx.warnings.append(
            "App mode {} has no 1:1 JasperGold FPV mapping.".format(mode)
        )
    ctx.mapped += 1
    return True


def _handle_cd(ctx, tokens, original):
    # type: (_Ctx, List[str], str) -> bool
    if tokens[0] != "cd":
        return False
    # Keep cd — JasperGold Tcl supports it; expand known vars
    rest = " ".join(tokens[1:])
    rest = _expand_vars(rest, ctx.vars)
    # If still a $var that we set to a cmd-sub, keep $var form
    _map(ctx, "cd {}".format(rest if rest else "[pwd]"))
    return True


def _handle_analyze(ctx, tokens, original):
    # type: (_Ctx, List[str], str) -> bool
    if tokens[0] != "analyze":
        return False

    fmt = _get_opt(tokens, "-format", "sverilog")
    lang = _fmt_lang(fmt)
    ctx.analyze_lang = lang

    extras = []  # type: List[str]
    files = []  # type: List[str]

    vcs = _get_opt(tokens, "-vcs", "")
    if vcs is not None and vcs != "":
        e, f = _parse_vcs_blob(vcs)
        extras.extend(e)
        files.extend(f)

    # Positional / braced file lists and [list ...] after options
    i = 1
    while i < len(tokens):
        t = tokens[i]
        if t in ("-format", "-vcs", "-library", "-work"):
            i += 2
            continue
        if t.startswith("-") and not t.startswith("-f"):
            # flag with optional arg
            if i + 1 < len(tokens) and not tokens[i + 1].startswith("-"):
                i += 2
            else:
                i += 1
            continue
        if t == "-f" and i + 1 < len(tokens):
            files.append(_unbrace(tokens[i + 1]))
            i += 2
            continue
        body = _unbrace(t)
        # [list a b c] or brace list of files
        if t.startswith("[") or t.startswith("{"):
            # strip leading "list "
            body2 = re.sub(r"^\s*list\s+", "", body)
            found = _extract_files_from_text(body2)
            if found:
                files.extend(found)
            else:
                # whitespace-separated tokens that look like files
                for w in body2.split():
                    if re.search(r"\.(sv|v|svh|vh|f)$", w):
                        files.append(w)
            i += 1
            continue
        if re.search(r"\.(sv|v|svh|vh|f)$", t):
            files.append(_unbrace(t))
        i += 1

    # Also scan full original for files (multi-line brace bodies)
    if not files:
        files = _extract_files_from_text(original)

    # de-dup
    seen = set()
    uniq = []
    for f in files:
        f2 = _expand_vars(f, ctx.vars)
        if f2 not in seen:
            seen.add(f2)
            uniq.append(f2)
    files = uniq

    _ensure_clear(ctx)
    _emit_cov_init(ctx)

    if not files and not extras:
        _unmapped(ctx, original, "analyze without recognizable files")
        return True

    parts = ["analyze", lang] + extras + files
    _map(ctx, " ".join(parts))
    ctx.analyze_done = True
    return True


def _handle_read_file(ctx, tokens, original):
    # type: (_Ctx, List[str], str) -> bool
    if tokens[0] != "read_file":
        return False

    top = _get_opt(tokens, "-top")
    fmt = _get_opt(tokens, "-format", "sverilog")
    vcs = _get_opt(tokens, "-vcs", "") or ""
    filelist = None
    rtl_files = []  # type: List[str]
    extras = []  # type: List[str]

    if vcs:
        extras, rtl_files = _parse_vcs_blob(vcs)
        m = re.search(r"-f\s+(\S+)", vcs)
        if m:
            filelist = m.group(1).strip("'\"")

    f_opt = _get_opt(tokens, "-f")
    if f_opt:
        filelist = f_opt

    i = 1
    while i < len(tokens):
        t = tokens[i]
        if t.startswith("-"):
            if t in ("-sva", "-aep"):
                i += 1 if t == "-sva" else 2
                continue
            i += 2 if i + 1 < len(tokens) else 1
            continue
        if t.endswith(".f"):
            filelist = _unbrace(t)
        elif re.search(r"\.(sv|v|svh|vh)$", t):
            rtl_files.append(_unbrace(t))
        i += 1

    if top:
        top = _expand_vars(top, ctx.vars)
        ctx.top = top
        ctx.vars.setdefault("design", top)

    lang = _fmt_lang(fmt)
    # classic examples in this repo used -sv12
    if filelist and lang == "-sv09":
        lang = "-sv12"

    _ensure_clear(ctx)
    if filelist:
        filelist = _expand_vars(filelist, ctx.vars)
        ctx.filelist = filelist
        _map(ctx, "analyze {} {} -f {}".format(lang, " ".join(extras), filelist).replace("  ", " ").strip())
    elif rtl_files:
        _map(ctx, "analyze {} {}".format(lang, " ".join(rtl_files)))
    else:
        _unmapped(ctx, original, "read_file without filelist or RTL files")
        return True

    ctx.analyze_done = True
    if _has_flag(tokens, "-sva"):
        _emit(ctx, "# SVA enabled (properties in design / bind files)")

    if ctx.top and not ctx.elaborate_done:
        _emit_elaborate(ctx, ctx.top, original)
    elif not ctx.top:
        ctx.pending_elaborate_top = True
        _emit(ctx, "# [vcf2jg] elaborate -top <TOP>  ;# set after design/top is known")
    return True


def _emit_elaborate(ctx, top, original=""):
    # type: (_Ctx, str, str) -> None
    if ctx.elaborate_done:
        return
    top = _expand_vars(top, ctx.vars)
    ctx.top = top

    # parameters from elaborate -vcs "-pvalue+..."
    params = list(ctx.elab_params)
    params.extend(_parse_pvalues(original))
    # de-dup
    seen = set()
    param_args = []
    for k, v in params:
        if k not in seen:
            seen.add(k)
            param_args.extend(["-parameter", k, v])

    bits = ["elaborate", "-top", top]
    if ctx.disable_auto_bbox:
        bits.insert(1, "-disable_auto_bbox")
    for cell in ctx.bbox_cells:
        bits.extend(["-bbox_m", cell])
    bits.extend(param_args)
    if ctx.want_witness_covers:
        bits.append("-create_related_covers {precondition witness}")
    _map(ctx, " ".join(bits))
    ctx.elaborate_done = True
    ctx.pending_elaborate_top = False


def _handle_elaborate(ctx, tokens, original):
    # type: (_Ctx, List[str], str) -> bool
    if tokens[0] != "elaborate":
        return False

    # elaborate $top $elaborateOption -sva
    # elaborate top -verbose -sva
    # elaborate top -vcs "-pvalue+A=1" -verbose -sva
    # elaborate top -cov all -verbose -sva
    top = None
    vcs = _get_opt(tokens, "-vcs", "") or ""
    if vcs:
        ctx.elab_params.extend(_parse_pvalues(vcs))

    if _has_flag(tokens, "-cov") or " -cov " in " {} ".format(original):
        ctx.want_cov = True
        _ensure_clear(ctx)
        _emit_cov_init(ctx)

    if _has_flag(tokens, "-inject_fault") or "-inject_fault" in original:
        ctx.warnings.append(
            "elaborate -inject_fault has no direct JasperGold FPV mapping (FTA)."
        )
        _emit(ctx, "# [vcf2jg TODO] -inject_fault / FTA — use Jasper fault apps if needed")

    skip_flags = {
        "-sva",
        "-verbose",
        "-cov",
        "-inject_fault",
        "-vcs",
    }
    i = 1
    while i < len(tokens):
        t = tokens[i]
        if t.startswith("-"):
            # skip known VC-only flags and their args
            if t in ("-vcs", "-cov", "-inject_fault", "-library"):
                i += 2
                continue
            if t in ("-sva", "-verbose"):
                i += 1
                continue
            # unknown flag: skip one token
            i += 1
            continue
        # skip values that are expanded elaborateOption (-verbose etc.)
        if t in ("-verbose", "-sva"):
            i += 1
            continue
        if top is None and not t.startswith("-"):
            top = _expand_vars(_unbrace(t), ctx.vars)
        i += 1

    if top is None:
        top = ctx.top or ctx.vars.get("top") or ctx.vars.get("design")

    if not top:
        _unmapped(ctx, original, "elaborate without top module")
        return True

    # strip residual flag-looking top from elaborateOption expansion
    if top.startswith("-"):
        top = ctx.top or ctx.vars.get("top") or ctx.vars.get("design")
        if not top:
            _unmapped(ctx, original, "elaborate top unresolved")
            return True

    _ensure_clear(ctx)
    _emit_elaborate(ctx, top, original)
    return True


def _handle_create_clock(ctx, tokens, original):
    # type: (_Ctx, List[str], str) -> bool
    if tokens[0] != "create_clock":
        return False
    period = _get_opt(tokens, "-period")
    name = _get_opt(tokens, "-name")
    clk = name
    for t in tokens[1:]:
        if t.startswith("-") or t.startswith("["):
            continue
        clk = _expand_vars(_unbrace(t), ctx.vars)
        break
    if not clk:
        _unmapped(ctx, original, "create_clock without clock name")
        return True
    note = ""
    if period:
        note = "  ;# period {} (JG FPV ignores absolute period)".format(period)
    _map(ctx, "clock {}{}".format(clk, note))
    ctx.clock_emitted = True
    # Emit reset soon after clock if already known
    _emit_reset_block(ctx)
    return True


def _handle_create_reset(ctx, tokens, original):
    # type: (_Ctx, List[str], str) -> bool
    if tokens[0] != "create_reset":
        return False

    # VC: create_reset rst -high | -low
    # VC: create_reset rst -sense high | -sense low
    # VC: create_reset -name rst -sense high
    sense = _get_opt(tokens, "-sense")
    active_high = _has_flag(tokens, "-high") or (
        sense is not None and sense.lower() in ("high", "1", "active_high")
    )
    active_low = _has_flag(tokens, "-low") or (
        sense is not None and sense.lower() in ("low", "0", "active_low")
    )
    name = _get_opt(tokens, "-name")
    rst = name
    for t in tokens[1:]:
        if t.startswith("-"):
            continue
        # skip sense value if already parsed as opt
        if sense and t == sense:
            continue
        rst = _expand_vars(_unbrace(t), ctx.vars)
        break
    if not rst:
        _unmapped(ctx, original, "create_reset without reset name")
        return True

    if not active_high and not active_low:
        # Infer from name
        if re.search(r"(n|N|_n|_N)$", rst):
            active_low = True
            ctx.warnings.append(
                "Reset '{}' polarity inferred as active-low from name.".format(rst)
            )
        else:
            active_high = True

    # LUBIS jasper style: reset -none + assume -reset <active expr>
    # Also keep classic form available for simple public examples.
    if active_low:
        ctx.reset_assume_expr = "!{}".format(rst)
        ctx.classic_reset_cmd = "reset ~{}".format(rst)
    else:
        ctx.reset_assume_expr = rst
        ctx.classic_reset_cmd = "reset {}".format(rst)

    # Prefer LUBIS style (matches trainee hand scripts)
    if not ctx.reset_emitted:
        if ctx.clock_emitted:
            _emit_reset_block(ctx)
        else:
            _emit(
                ctx,
                "# reset will be: reset -none; assume -reset {}".format(
                    ctx.reset_assume_expr
                ),
            )
            ctx.mapped += 1
    return True


def _handle_sim_force(ctx, tokens, original):
    # type: (_Ctx, List[str], str) -> bool
    if tokens[0] != "sim_force":
        return False
    # sim_force rst -apply 1'b1
    sig = None
    value = None
    apply = _get_opt(tokens, "-apply")
    if apply is not None:
        value = apply
    for t in tokens[1:]:
        if t.startswith("-"):
            continue
        if sig is None:
            sig = _expand_vars(_unbrace(t), ctx.vars)
        elif value is None:
            value = _unbrace(t)
    if not sig:
        _unmapped(ctx, original, "sim_force without signal")
        return True
    if value is None:
        value = "1'b1"

    ctx.reset_assume_expr = _reset_expr_from_force(sig, value)
    # classic equivalent when forced active
    if ctx.reset_assume_expr.startswith("!"):
        ctx.classic_reset_cmd = "reset ~{}".format(sig)
    else:
        ctx.classic_reset_cmd = "reset {}".format(sig)

    _emit(
        ctx,
        "# VC Formal sim_force {} -apply {} → reset -none; assume -reset {}".format(
            sig, value, ctx.reset_assume_expr
        ),
    )
    ctx.mapped += 1
    if ctx.clock_emitted and not ctx.reset_emitted:
        _emit_reset_block(ctx)
    return True


def _handle_sim_run(ctx, tokens, original):
    # type: (_Ctx, List[str], str) -> bool
    if tokens[0] != "sim_run":
        return False
    # Ensure reset is emitted before prove
    if ctx.reset_assume_expr and not ctx.reset_emitted:
        _emit_reset_block(ctx)
    _skip(
        ctx,
        "sim_run: JasperGold applies reset via 'reset' / assume -reset",
        original,
    )
    return True


def _handle_sim_save_reset(ctx, tokens, original):
    # type: (_Ctx, List[str], str) -> bool
    if tokens[0] != "sim_save_reset":
        return False
    if ctx.reset_assume_expr and not ctx.reset_emitted:
        _emit_reset_block(ctx)
    _skip(
        ctx,
        "sim_save_reset: covered by JasperGold reset -none + assume -reset",
        original,
    )
    return True


def _handle_sim_set_state(ctx, tokens, original):
    # type: (_Ctx, List[str], str) -> bool
    if tokens[0] != "sim_set_state":
        return False
    # sim_set_state -all -apply x  → free / X start state ≈ reset -none
    if _has_flag(tokens, "-all") or "x" in original.lower():
        _emit(ctx, "# VC Formal sim_set_state -all x → free start (reset -none)")
        if not ctx.reset_emitted:
            ctx.reset_assume_expr = ctx.reset_assume_expr  # keep if any
            if not ctx.reset_assume_expr:
                _map(ctx, "reset -none")
                ctx.reset_emitted = True
            else:
                _emit_reset_block(ctx)
        else:
            ctx.mapped += 1
        return True
    _unmapped(ctx, original, "sim_set_state variant needs review")
    return True


def _handle_check_fv(ctx, tokens, original):
    # type: (_Ctx, List[str], str) -> bool
    if tokens[0] != "check_fv":
        return False
    if ctx.reset_assume_expr and not ctx.reset_emitted:
        _emit_reset_block(ctx)

    if _has_flag(tokens, "-subtype"):
        sub = _get_opt(tokens, "-subtype", "")
        if sub and "witness" in sub.lower():
            _map(ctx, "prove -all -covers")
            return True

    if _has_flag(tokens, "-block") or _has_flag(tokens, "-bg"):
        _map(ctx, "prove -all")
    else:
        props = [t for t in tokens[1:] if not t.startswith("-")]
        if props:
            prop_list = " ".join(
                _expand_vars(_unbrace(p), ctx.vars) for p in props
            )
            _map(ctx, "prove -property {{{}}}".format(prop_list))
        else:
            _map(ctx, "prove -all")

    if ctx.want_cov:
        _map(ctx, "check_cov -measure -type all")
    return True


def _handle_report_fv(ctx, tokens, original):
    # type: (_Ctx, List[str], str) -> bool
    if tokens[0] != "report_fv":
        return False
    redirect = None
    m = re.search(r">\s*(\S+)", original)
    if m:
        redirect = m.group(1)
    if redirect:
        _map(ctx, "report_proofs -verbose > {}".format(redirect))
    else:
        _map(ctx, "report_proofs -verbose")
    return True


def _handle_fvassume(ctx, tokens, original):
    # type: (_Ctx, List[str], str) -> bool
    if tokens[0] not in ("fvassume", "assume"):
        return False
    # Don't re-process our own assume -reset if re-fed
    if _has_flag(tokens, "-reset"):
        _map(ctx, original.strip())
        return True
    expr = _get_opt(tokens, "-expr")
    if not expr:
        if len(tokens) >= 2:
            body = " ".join(tokens[1:])
            if body.startswith("{"):
                _map(ctx, "assume {}".format(body))
            else:
                _map(ctx, "assume {{{}}}".format(_unbrace(body)))
            return True
        _unmapped(ctx, original, "fvassume without -expr")
        return True
    expr = _expand_vars(expr, ctx.vars)
    _map(ctx, "assume {{{}}}".format(expr))
    depth = _get_opt(tokens, "-depth")
    if depth:
        _emit(
            ctx,
            "# note: VC -depth {} on assume has no direct 1:1; review if needed".format(
                depth
            ),
        )
    return True


def _handle_fvassert(ctx, tokens, original):
    # type: (_Ctx, List[str], str) -> bool
    if tokens[0] not in ("fvassert", "assert"):
        return False
    expr = _get_opt(tokens, "-expr")
    if expr:
        expr = _expand_vars(expr, ctx.vars)
        _map(ctx, "assert -name auto_assert {{{}}}".format(expr))
        return True
    if len(tokens) >= 2:
        body = " ".join(tokens[1:])
        _map(ctx, "assert {}".format(body if body.startswith("{") else "{{{}}}".format(body)))
        return True
    _unmapped(ctx, original)
    return True


def _handle_set_blackbox(ctx, tokens, original):
    # type: (_Ctx, List[str], str) -> bool
    if tokens[0] != "set_blackbox":
        return False
    # set_blackbox -cells path.to.inst
    cell = _get_opt(tokens, "-cells") or _get_opt(tokens, "-cell")
    if not cell:
        for t in tokens[1:]:
            if not t.startswith("-"):
                cell = _unbrace(t)
                break
    if cell:
        cell = _expand_vars(cell, ctx.vars)
        ctx.bbox_cells.append(cell)
        if ctx.elaborate_done:
            _map(ctx, "bbox_m {}".format(cell))
        else:
            _emit(
                ctx,
                "# blackbox cell '{}' → elaborate -bbox_m {}".format(cell, cell),
            )
            ctx.mapped += 1
        return True
    _unmapped(ctx, original, "set_blackbox without cell")
    return True


def _handle_snip_driver(ctx, tokens, original):
    # type: (_Ctx, List[str], str) -> bool
    if tokens[0] != "snip_driver":
        return False
    # snip_driver {a b c}  → stopat a b c
    sigs = []
    for t in tokens[1:]:
        body = _unbrace(t)
        sigs.extend(body.split())
    if not sigs:
        _unmapped(ctx, original, "snip_driver without signals")
        return True
    _map(ctx, "stopat {}".format(" ".join(sigs)))
    return True


def _handle_set_change_at(ctx, tokens, original):
    # type: (_Ctx, List[str], str) -> bool
    if tokens[0] != "set_change_at":
        return False
    # VC Formal sampling edge — Jasper uses clock domain / clock -rate
    _skip(
        ctx,
        "set_change_at (sampling) — review clock -rate / default sampling in JasperGold",
        original,
    )
    return True


def _handle_set_grid_usage(ctx, tokens, original):
    # type: (_Ctx, List[str], str) -> bool
    if tokens[0] != "set_grid_usage":
        return False
    _skip(ctx, "set_grid_usage — use JasperGold set_max_jobs / farm setup", original)
    return True


def _handle_fta(ctx, tokens, original):
    # type: (_Ctx, List[str], str) -> bool
    if tokens[0] not in ("fta_init", "compute_fta", "configure_fta_props"):
        return False
    _unmapped(
        ctx,
        original,
        "FTA (Fault Tree Analysis) has no 1:1 JasperGold FPV command",
    )
    return True


def _handle_current_design(ctx, tokens, original):
    # type: (_Ctx, List[str], str) -> bool
    if tokens[0] != "current_design":
        return False
    if len(tokens) > 1:
        top = _expand_vars(_unbrace(tokens[1]), ctx.vars)
        ctx.top = top
        if ctx.analyze_done and not ctx.elaborate_done:
            _emit_elaborate(ctx, top, original)
        else:
            _emit(ctx, "# current_design {}".format(top))
            ctx.mapped += 1
    return True


def _handle_source(ctx, tokens, original):
    # type: (_Ctx, List[str], str) -> bool
    if tokens[0] != "source":
        return False
    path = _expand_vars(_unbrace(tokens[1]), ctx.vars) if len(tokens) > 1 else "?"
    _emit(ctx, "# VC Formal sourced: {}".format(path))
    _emit(
        ctx,
        "# Consider: source {{{}}}  if the sourced file is also converted".format(
            path
        ),
    )
    ctx.warnings.append(
        "'source {}' not auto-inlined; convert that file separately.".format(path)
    )
    ctx.unmapped += 1
    return True


def _handle_puts(ctx, tokens, original):
    # type: (_Ctx, List[str], str) -> bool
    if tokens[0] != "puts":
        return False
    _map(ctx, original.strip())
    return True


def _handle_quit_exit(ctx, tokens, original):
    # type: (_Ctx, List[str], str) -> bool
    if tokens[0] in ("quit", "exit"):
        _map(ctx, "exit")
        return True
    return False


def _handle_setenv(ctx, tokens, original):
    # type: (_Ctx, List[str], str) -> bool
    if tokens[0] != "setenv":
        return False
    _skip(ctx, "setenv — set in shell before launching jg", original)
    return True


# Order matters: more specific first
_HANDLERS = [
    _handle_set_app_var,
    _handle_set_fml_var,
    _handle_set_fml_appmode,
    _handle_set_blackbox,
    _handle_set_change_at,
    _handle_set_grid_usage,
    _handle_read_file,
    _handle_analyze,
    _handle_elaborate,
    _handle_create_clock,
    _handle_create_reset,
    _handle_sim_force,
    _handle_sim_run,
    _handle_sim_save_reset,
    _handle_sim_set_state,
    _handle_check_fv,
    _handle_report_fv,
    _handle_fvassume,
    _handle_fvassert,
    _handle_snip_driver,
    _handle_fta,
    _handle_current_design,
    _handle_source,
    _handle_cd,
    _handle_puts,
    _handle_setenv,
    _handle_quit_exit,
    _handle_set,
]


def _dispatch(ctx, original):
    # type: (_Ctx, str) -> None
    raw = original.rstrip()
    line = _strip_inline_comment(raw)
    if not line.strip():
        # preserve blank lines sparingly
        if raw.strip() == "":
            _emit(ctx, "")
        elif raw.lstrip().startswith("#"):
            _emit(ctx, raw)
        return

    if line.lstrip().startswith("#"):
        _emit(ctx, raw)
        return

    # Multi-line if handled as one logical line
    stripped = line.strip()
    if stripped.startswith("if ") or stripped.startswith("if{"):
        if _handle_if_block(ctx, stripped):
            return

    expanded = _expand_vars(line, ctx.vars)
    tokens = _tokenize(expanded)
    if not tokens:
        _emit(ctx, "")
        return

    cmd = tokens[0]

    # Skip lone closing braces / structural leftovers
    if cmd in ("}", "{", "]", "else", "elseif"):
        return

    for handler in _HANDLERS:
        if handler(ctx, tokens, expanded):
            return

    if cmd in (
        "set_message_info",
        "suppress_message",
        "unsuppress_message",
        "set_host_options",
        "start_gui",
        "gui_start",
        "define_proc_attributes",
    ):
        _skip(ctx, "{} not used in JasperGold batch FPV".format(cmd), expanded)
        return

    # prove already Jasper-like
    if cmd == "prove":
        _map(ctx, expanded.strip())
        return
    if cmd == "clock" and len(tokens) >= 2:
        _map(ctx, expanded.strip())
        ctx.clock_emitted = True
        return

    _unmapped(ctx, expanded, "no mapping for '{}'".format(cmd))


def convert_vc_tcl(text, source_name="<input>"):
    # type: (str, str) -> ConversionResult
    """
    Convert VC Formal Tcl source text to JasperGold Tcl.

    Parameters
    ----------
    text:
        Full contents of a VC Formal .tcl file.
    source_name:
        Name used in the generated header comment.
    """
    ctx = _Ctx()
    logical_lines = _join_logical_lines(text.splitlines())

    header = [
        "# =============================================================================",
        "# Auto-generated by vcf2jg from: {}".format(source_name),
        "# Review [vcf2jg TODO] and [vcf2jg skip] comments before running in JasperGold.",
        "# =============================================================================",
        "",
    ]
    for h in header:
        _emit(ctx, h)

    for line in logical_lines:
        _dispatch(ctx, line)

    # Deferred elaborate
    if ctx.pending_elaborate_top and ctx.top and not ctx.elaborate_done:
        _emit(ctx, "")
        _emit_elaborate(ctx, ctx.top)

    # Deferred reset (e.g. sim_force before clock, never flushed)
    if ctx.reset_assume_expr and not ctx.reset_emitted:
        _emit(ctx, "")
        _emit_reset_block(ctx)

    # Coverage measure if COV mode and prove was never called
    if ctx.want_cov and ctx.cov_init_emitted:
        body_txt = "\n".join(ctx.out)
        if "check_cov -measure" not in body_txt and "prove" in body_txt:
            pass  # already handled in check_fv
        elif "check_cov -measure" not in body_txt and ctx.appmode in ("COV", "FCA"):
            _emit(ctx, "check_cov -measure -type all")
            ctx.mapped += 1

    if not ctx.analyze_done:
        ctx.warnings.append(
            "No analyze/read_file found. JasperGold script may be incomplete."
        )

    # Collapse excessive blank lines
    cleaned = []  # type: List[str]
    blank_run = 0
    for ln in ctx.out:
        if ln.strip() == "":
            blank_run += 1
            if blank_run <= 1:
                cleaned.append("")
        else:
            blank_run = 0
            cleaned.append(ln)

    body = "\n".join(cleaned)
    if not body.endswith("\n"):
        body += "\n"

    return ConversionResult(
        jasper_tcl=body,
        warnings=ctx.warnings,
        mapped_count=ctx.mapped,
        skipped_count=ctx.skipped,
        unmapped_count=ctx.unmapped,
    )


def convert_file(input_path, output_path=None):
    # type: (Path, Optional[Path]) -> ConversionResult
    """Convert a VC Formal Tcl file; optionally write JasperGold output."""
    text = input_path.read_text(encoding="utf-8", errors="replace")
    result = convert_vc_tcl(text, source_name=str(input_path))
    if output_path is None:
        output_path = input_path.with_name(input_path.stem + "_jg.tcl")
    output_path.write_text(result.jasper_tcl, encoding="utf-8")
    return result
