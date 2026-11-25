"""
Convert Synopsys VC Formal Tcl scripts to Cadence JasperGold Tcl scripts.

Best-effort FPV translator for VC Formal Property Checking App commands
(analyze / read_file / elaborate / clock / reset / check_fv / fvassume / …)
to Jasper Apps FPV (command reference 2025.03):

    clear -all
    analyze -sv09 <files>
    elaborate -top <top> -create_related_covers {precondition witness}
    clock <clk>
    reset -none
    assume -reset <active_expr>
    prove -all
    report

Not a full Tcl interpreter. Remaining tool-only knobs become short comments.
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

        # App mode / coverage / related covers
        self.appmode = "FPV"
        self.want_cov = False
        self.want_witness_covers = True
        self.want_vacuity_covers = True
        self.cov_init_emitted = False
        self.max_jobs = None  # type: Optional[str]
        self.prove_time_limit = None  # type: Optional[str]
        self.want_check_reset = False

        # Reset: reset -none + assume -reset <expr>
        # expr is active-condition: "rst" (high) or "!rst_n" / "~rst_n" (low)
        self.reset_assume_expr = None  # type: Optional[str]
        self.classic_reset_cmd = None  # type: Optional[str]
        # riscv-formal NERV/RVFI: Jasper native "reset <sig>", patch defines.sv
        self.rvfi_style = False
        self.analyze_files = []  # type: List[str]
        self.non_resettable_regs = None  # type: Optional[str]
        self.reset_max_iterations = None  # type: Optional[str]

        # Elaboration extras
        # -design / modules → elaborate -bbox_m
        # -cells / instances → elaborate -bbox_i
        self.bbox_m = []  # type: List[str]
        self.bbox_i = []  # type: List[str]
        self.bbox_cells = self.bbox_m  # alias used by older helpers
        self.elab_params = []  # type: List[Tuple[str, str]]
        self.disable_auto_bbox = False
        self.pending_elaborate_top = False

        # Analyze language default for VC Formal sverilog
        self.analyze_lang = "-sv09"

        # Simple if-stack: True = execute, False = skip
        self.if_stack = []  # type: List[bool]
        self.if_taken_stack = []  # type: List[bool]  # whether a branch already ran


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_VAR_REF_RE = re.compile(r"\$(\w+)|\$\{(\w+)\}")
_HDL_EXT = r"(?:sva|svh|sv|vh|v|f)"
_FILE_RE = re.compile(
    r"(?:^|[\s\[\{])((?:\$[\w{}]+/)?[\w./+-]+\." + _HDL_EXT + r")\b"
)
_PVALUE_RE = re.compile(r"-pvalue\+([A-Za-z_]\w*)=(\S+)")
_INCDIR_RE = re.compile(r"\+incdir\+(\S+)")
_DEFINE_RE = re.compile(r"\+define\+(\S+)")
_RESET_NAME_RE = re.compile(r"(?i)(^|_)((a|p)?reset|rst)_?n?$")
_GET_PROPS_RE = re.compile(r"\[get_props((?:[^\]]|\[[^\]]*\])*)\]")
_TIME_RE = re.compile(r"^(\d+(?:\.\d+)?)([HhMmSs])$")


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
            nxt = tokens[i + 1]
            if _is_cmd_sub(nxt):
                return nxt
            return _unbrace(nxt)
    return default


def _has_flag(tokens, flag):
    # type: (Sequence[str], str) -> bool
    return flag in tokens


def _scan_nesting(s, in_quote=False):
    # type: (str, bool) -> Tuple[int, int, bool]
    """Brace / bracket net depth and whether a double-quote is still open."""
    brace = 0
    bracket = 0
    i = 0
    n = len(s)
    while i < n:
        ch = s[i]
        if ch == "\\" and i + 1 < n:
            i += 2
            continue
        if ch == '"':
            in_quote = not in_quote
        elif not in_quote:
            if ch == "{":
                brace += 1
            elif ch == "}":
                brace -= 1
            elif ch == "[":
                bracket += 1
            elif ch == "]":
                bracket -= 1
        i += 1
    return brace, bracket, in_quote


def _join_logical_lines(lines):
    # type: (Iterable[str]) -> List[str]
    """
    Join physical lines into logical Tcl commands:
    - backslash continuations
    - multi-line {...}, [...] and "..." groups
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

    # Pass 2: brace / bracket / quote balance join
    logical = []  # type: List[str]
    acc = ""
    brace = bracket = 0
    in_quote = False
    for line in cont:
        if not acc:
            acc = line
        else:
            acc = acc + "\n" + line
        db, dk, in_quote = _scan_nesting(line, in_quote)
        brace += db
        bracket += dk
        if brace <= 0 and bracket <= 0 and not in_quote:
            logical.append(acc)
            acc = ""
            brace = bracket = 0
            in_quote = False
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
    Evaluate simple Tcl conditions used in VC Formal scripts.
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
        r"(?:\$[\w{}]+/|[\w./+-])*[\w.+-]+\." + _HDL_EXT + r"\b", text
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


def _promote_vh_headers(files, extras):
    # type: (List[str], List[str]) -> Tuple[List[str], List[str], bool]
    """
    JasperGold does not inherit VCS include search from a file list.

    ``.vh`` files (e.g. riscv-formal ``rvfi_macros.vh``) are headers:
    add ``+incdir+<dir>`` and do not pass them as analyze sources.
    Without this, `` `include "rvfi_macros.vh" `` fails VERI-1245.
    """
    rtl = []
    extras = list(extras)
    saw = False
    for f in files:
        if re.search(r"\.vh$", f):
            saw = True
            parent = str(Path(f).parent)
            if parent in ("", "."):
                parent = "."
            flag = "+incdir+" + parent
            if flag not in extras:
                extras.append(flag)
        else:
            rtl.append(f)
    if saw:
        if "+incdir+." not in extras:
            extras.insert(0, "+incdir+.")
        for f in rtl:
            norm = f.replace("\\", "/")
            if "/insns/" in norm:
                d = norm[: norm.rfind("/insns/") + len("/insns")]
                flag = "+incdir+" + d
                if flag not in extras:
                    extras.append(flag)
    return rtl, extras, saw


def _looks_rvfi(files):
    # type: (Iterable[str]) -> bool
    for f in files:
        n = f.replace("\\", "/").lower()
        base = n.rsplit("/", 1)[-1]
        if (
            base in ("rvfi_macros.vh", "rvfi_testbench.sv", "nerv.sv")
            or base.startswith("rvfi_")
        ):
            return True
    return False


def _add_path_incdir(extras, files, key):
    # type: (List[str], List[str], str) -> None
    for f in files:
        norm = f.replace("\\", "/")
        if key in norm:
            d = norm[: norm.rfind(key) + len(key) - 1]
            flag = "+incdir+" + d
            if flag not in extras:
                extras.append(flag)


def _apply_rvfi_analyze(ctx, files, extras, lang):
    # type: (_Ctx, List[str], List[str], str) -> Tuple[List[str], List[str], str]
    """
    JasperGold RVFI/NERV analyze: -sv12 -mfcu, +incdir for checks/insns,
    and rvfi_macros.vh as a header rather than a source file.
    """
    orig = list(files)
    ctx.analyze_files.extend(orig)
    files, extras, saw_vh = _promote_vh_headers(files, extras)
    if saw_vh or _looks_rvfi(orig) or _looks_rvfi(files):
        ctx.rvfi_style = True
        lang = "-sv12 -mfcu"
        if "+incdir+." not in extras:
            extras.insert(0, "+incdir+.")
        _add_path_incdir(extras, orig + files, "/checks/")
        _add_path_incdir(extras, orig + files, "/insns/")
    return files, extras, lang


_YOSYS_DEFINE_RE = re.compile(
    r"^[ \t]*`define[ \t]+YOSYS\b[^\n]*\n?", re.MULTILINE
)
_PREPROC_START_RE = re.compile(
    r"^`(define|include|ifdef|ifndef|elsif|else|endif|undef)\b"
)


def fix_riscv_formal_defines(text):
    # type: (str) -> str
    """
    Make a generated riscv-formal defines.sv parse in JasperGold.

    The last line of each multi-line CSR `` `define `` must not end with ``\\``,
    or Jasper splices WIRES into CONN/SIGNALS/OUTPUTS (VERI-1137).
    Drop `` `define YOSYS `` so ``rvformal_rand_reg`` stays a wire.
    """
    text = _YOSYS_DEFINE_RE.sub("", text)
    lines = text.splitlines(True)
    out = []  # type: List[str]
    n = len(lines)
    for i, line in enumerate(lines):
        if line.endswith("\r\n"):
            nl, core = "\r\n", line[:-2]
        elif line.endswith("\n"):
            nl, core = "\n", line[:-1]
        else:
            nl, core = "", line
        if not core.rstrip().endswith("\\"):
            out.append(line)
            continue
        nxt = lines[i + 1] if i + 1 < n else ""
        if i + 1 >= n or _PREPROC_START_RE.match(nxt.lstrip()):
            core = core.rstrip()
            if core.endswith("\\"):
                core = core[:-1].rstrip()
            out.append(core + nl)
        else:
            out.append(line)
    return "".join(out)


def patch_companion_defines(tcl_path, result=None):
    # type: (Path, Optional[ConversionResult]) -> List[Path]
    """
    Patch sibling / listed defines.sv files next to a VC Formal Tcl script.
    Returns paths that were written.
    """
    tcl_path = Path(tcl_path)
    root = tcl_path.parent
    seen = set()  # type: set
    candidates = []  # type: List[Path]

    def _add(p):
        # type: (Path) -> None
        try:
            rp = p.resolve()
        except (OSError, RuntimeError):
            return
        if rp in seen or not rp.is_file():
            return
        seen.add(rp)
        candidates.append(rp)

    _add(root / "defines.sv")
    if tcl_path.is_file():
        src = tcl_path.read_text(encoding="utf-8", errors="replace")
        for m in re.finditer(
            r"(?:^|[\s\{])((?:[\w./+-]+/)?defines\.sv)\b", src
        ):
            _add(root / m.group(1))
    patched = []  # type: List[Path]
    for p in candidates:
        original = p.read_text(encoding="utf-8", errors="replace")
        if "RISCV_FORMAL" not in original and "`define YOSYS" not in original:
            continue
        fixed = fix_riscv_formal_defines(original)
        if fixed == original:
            continue
        p.write_text(fixed, encoding="utf-8")
        patched.append(p)
        if result is not None:
            result.warnings.append(
                "Patched {} for JasperGold "
                "(CSR `define last-line '\\', `define YOSYS)".format(p)
            )
    return patched


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
    # default SystemVerilog for VC Formal -format sverilog
    return "-sv09"


def _vcf_time_to_jg(val):
    # type: (str) -> str
    """Convert VC Formal 2H / 5M / 10s to Jasper 2h / 5m / 10s."""
    v = str(val).strip().strip("'\"")
    m = _TIME_RE.match(v)
    if m:
        return m.group(1) + m.group(2).lower()
    if re.fullmatch(r"\d+(?:\.\d+)?", v):
        return v + "s"
    return v


def _is_reset_like(name):
    # type: (str) -> bool
    leaf = name.split(".")[-1]
    return bool(_RESET_NAME_RE.search(leaf))


def _glob_to_regexp(pat):
    # type: (str) -> str
    """Convert a VC Formal glob (foo.*.bar*) to a Tcl regexp."""
    out = []
    for ch in pat:
        if ch == "*":
            out.append(".*")
        elif ch == "?":
            out.append(".")
        elif ch in r".^$+()[]{}|\\":
            out.append("\\" + ch)
        else:
            out.append(ch)
    return "".join(out)


def _looks_glob(name):
    # type: (str) -> bool
    return "*" in name or "?" in name


def _rewrite_vc_cmd_subs(text):
    # type: (str) -> str
    """Rewrite VC Formal command substitutions that Jasper understands."""

    def repl_props(m):
        args = m.group(1) or ""
        sm = re.search(r"-status\s+(\S+)", args)
        if sm:
            st = sm.group(1).strip("{}")
            return "[get_property_list -include {{status {}}}]".format(st)
        return "[get_property_list]"

    text = _GET_PROPS_RE.sub(repl_props, text)
    text = text.replace("[get_fvtask]", "[task]")
    return text


def _clock_from_tokens(tokens):
    # type: (Sequence[str]) -> Optional[str]
    return _get_opt(tokens, "-clock") or _get_opt(tokens, "-clk")


def _prop_body(expr, clock):
    # type: (str, Optional[str]) -> str
    expr = expr.strip()
    if clock and "@" not in expr:
        return "{{@(posedge {}) {}}}".format(clock, expr)
    return "{{{}}}".format(expr)


def _name_list_from_tokens(tokens, skip_flags=None):
    # type: (Sequence[str], Optional[Sequence[str]]) -> List[str]
    """Collect positional / braced names, skipping known flags and their args."""
    skip = set(skip_flags or ())
    flags_with_arg = {
        "-expr",
        "-clock",
        "-clk",
        "-name",
        "-task",
        "-type",
        "-filter",
        "-prop",
        "-property",
        "-session",
        "-copy",
        "-depth",
        "-subtype",
        "-run_finish",
        "-break",
        "-value",
        "-apply",
        "-construct",
        "-design",
        "-cells",
        "-cell",
        "-module",
        "-modules",
        "-par_task",
        "-db_name",
        "-file",
        "-format",
        "-vcs",
        "-top",
        "-period",
        "-sense",
    }
    names = []  # type: List[str]
    i = 1
    n = len(tokens)
    while i < n:
        t = tokens[i]
        if t in skip:
            i += 1
            continue
        if t in flags_with_arg:
            i += 2
            continue
        if t.startswith("-"):
            i += 1
            continue
        body = _unbrace(t)
        parts = body.split() if (" " in body or "\n" in body) else [body]
        for p in parts:
            p = p.strip()
            if p:
                names.append(p)
        i += 1
    return names


def _safe_prop_ident(name):
    # type: (str) -> str
    ident = re.sub(r"[^A-Za-z0-9_]", "_", name)
    if ident and ident[0].isdigit():
        ident = "p_" + ident
    return ident or "prop"


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
    extra = []
    if ctx.reset_max_iterations:
        extra.append("-max_iterations {}".format(ctx.reset_max_iterations))
    if ctx.non_resettable_regs is not None:
        extra.append("-non_resettable_regs {}".format(ctx.non_resettable_regs))
    extra_s = (" " + " ".join(extra)) if extra else ""
    if ctx.rvfi_style and ctx.classic_reset_cmd:
        _map(ctx, "{}{}".format(ctx.classic_reset_cmd, extra_s))
        ctx.reset_emitted = True
        return
    if ctx.reset_assume_expr:
        _map(ctx, "reset -none{}".format(extra_s))
        _map(ctx, "assume -reset {}".format(ctx.reset_assume_expr))
        ctx.reset_emitted = True
    elif ctx.classic_reset_cmd:
        _map(ctx, ctx.classic_reset_cmd)
        ctx.reset_emitted = True
    elif extra:
        _map(ctx, "reset -none{}".format(extra_s))
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
        # all conditions false, no else (e.g. auto_check 0)
        _emit(ctx, "# if-chain: no branch taken (flags evaluated false; interactive session)")
        ctx.mapped += 1
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
                "fvassume",
                "fvassert",
                "fvcover",
                "fvenable",
                "fvdisable",
                "set_constant",
                "snip_driver",
                "snip_drive",
                "set_blackbox",
                "view_trace",
                "report_fv",
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

    # Common VC Formal control flags — keep as set for readability
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
    val = tokens[2] if len(tokens) > 2 else ""
    lname = name.lower()
    if "cov" in lname:
        ctx.want_cov = True
        _emit(ctx, "# VC Formal {} {} → Jasper check_cov flow".format(name, val))
        ctx.mapped += 1
        return True
    if lname in ("fml_auto_save", "save_session_default"):
        on = "on" if _truthy(val) or str(val).lower() in ("default", "true", "on") else "off"
        _map(ctx, "set_database_auto_save_restore {}".format(on))
        return True
    if lname == "fml_mode_on":
        _emit(ctx, "# VC Formal fml_mode_on {} — Jasper FPV is always formal".format(val))
        ctx.mapped += 1
        return True
    if lname == "apply_bind_in_all_units":
        _emit(
            ctx,
            "# VC Formal apply_bind_in_all_units {} — analyze bind files before elaborate".format(
                val
            ),
        )
        ctx.mapped += 1
        return True
    if lname == "fml_composite_trace":
        _emit(
            ctx,
            "# VC Formal fml_composite_trace {} — Jasper visualize uses a single composed trace".format(
                val
            ),
        )
        ctx.mapped += 1
        return True
    if lname == "analyze_skip_translate_body":
        _emit(ctx, "# VC Formal analyze_skip_translate_body {}".format(val))
        ctx.mapped += 1
        return True
    _emit(ctx, "# VC Formal set_app_var {} {}".format(name, val))
    ctx.mapped += 1
    return True


def _handle_set_fml_var(ctx, tokens, original):
    # type: (_Ctx, List[str], str) -> bool
    if tokens[0] != "set_fml_var":
        return False
    name = tokens[1] if len(tokens) > 1 else ""
    val = tokens[2] if len(tokens) > 2 else ""
    lname = name.lower()

    if "witness" in lname:
        ctx.want_witness_covers = _truthy(val)
        _emit(
            ctx,
            "# VC Formal {} {} → elaborate -create_related_covers {{witness}}".format(
                name, val
            ),
        )
        ctx.mapped += 1
        return True
    if "vacuity" in lname:
        ctx.want_vacuity_covers = _truthy(val)
        _emit(
            ctx,
            "# VC Formal {} {} → elaborate -create_related_covers {{precondition}}".format(
                name, val
            ),
        )
        ctx.mapped += 1
        return True
    if "cov" in lname:
        ctx.want_cov = True
        _emit(ctx, "# VC Formal {} {} → Jasper check_cov".format(name, val))
        ctx.mapped += 1
        return True
    if lname in ("fml_effort",):
        effort = str(val).strip().lower()
        effort_map = {
            "high": "set_engine_mode {Ht Hp B I N}",
            "bounded": "set_engine_mode {B}",
            "bug_hunting": "set_engine_mode {Bm J}",
            "discovery": "set_engine_mode {D}",
            "low": "set_engine_mode {B}",
            "medium": "set_engine_mode {Ht B N}",
        }
        cmd = effort_map.get(effort)
        if cmd:
            _emit(ctx, "# VC Formal fml_effort {}".format(val))
            _map(ctx, cmd)
            if effort == "bounded":
                _emit(
                    ctx,
                    "# For a bounded proof also set: set_max_trace_length <N>; "
                    "set_prove_target_bound <N>",
                )
        else:
            _emit(ctx, "# VC Formal fml_effort {} — review set_engine_mode".format(val))
            ctx.mapped += 1
        return True
    if lname in ("fml_max_time", "fml_max_time_limit"):
        jg = _vcf_time_to_jg(val)
        ctx.prove_time_limit = jg
        _map(ctx, "set_prove_time_limit {}".format(jg))
        return True
    if lname in ("fml_progress_time_limit",):
        jg = _vcf_time_to_jg(val)
        _map(ctx, "set_prove_time_limit {}".format(jg))
        _emit(
            ctx,
            "# per-property cap: prove -per_property_time_limit {}".format(jg),
        )
        return True
    if lname in ("fml_max_memory", "fml_max_mem"):
        _emit(
            ctx,
            "# VC Formal {} {} — cap memory in the job scheduler / ulimit "
            "(no global Jasper equivalent)".format(name, val),
        )
        ctx.mapped += 1
        return True
    if lname == "fml_reset_property_check":
        ctx.want_check_reset = _truthy(val)
        _emit(
            ctx,
            "# VC Formal fml_reset_property_check {} → check_reset after reset setup".format(
                val
            ),
        )
        ctx.mapped += 1
        return True
    _emit(ctx, "# VC Formal set_fml_var {} {}".format(name, val))
    ctx.mapped += 1
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
    # Quick Guide: analyze_design -format sverilog -vcs test.v
    if tokens[0] not in ("analyze", "analyze_design"):
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
                    if re.search(r"\.(sva|svh|sv|vh|v|f)$", w):
                        files.append(w)
            i += 1
            continue
        if re.search(r"\.(sva|svh|sv|vh|v|f)$", t):
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
    files, extras, lang = _apply_rvfi_analyze(ctx, files, extras, lang)

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
        ctx.elab_params.extend(_parse_pvalues(vcs))
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
        elif re.search(r"\.(sva|svh|sv|vh|v)$", t):
            rtl_files.append(_unbrace(t))
        i += 1

    if top:
        top = _expand_vars(top, ctx.vars)
        ctx.top = top
        ctx.vars.setdefault("design", top)

    lang = _fmt_lang(fmt)
    # filelist flows commonly use SystemVerilog 2012
    if filelist and lang == "-sv09":
        lang = "-sv12"

    _ensure_clear(ctx)
    extra_rtl = []
    fl_exp = _expand_vars(filelist, ctx.vars) if filelist else ""
    for f in rtl_files:
        f2 = _expand_vars(f, ctx.vars)
        if fl_exp and f2 == fl_exp:
            continue
        extra_rtl.append(f2)
    extra_rtl, extras, lang = _apply_rvfi_analyze(ctx, extra_rtl, extras, lang)
    if filelist:
        ctx.filelist = fl_exp
        _map(
            ctx,
            "analyze {} {} -f {}".format(lang, " ".join(extras), fl_exp)
            .replace("  ", " ")
            .strip(),
        )
        if extra_rtl:
            _map(ctx, "analyze {} {}".format(lang, " ".join(extra_rtl)))
    elif extra_rtl:
        extra_flags = " ".join(extras)
        cmd = "analyze {} {} {}".format(lang, extra_flags, " ".join(extra_rtl))
        _map(ctx, re.sub(r" +", " ", cmd).strip())
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
    for mod in ctx.bbox_m:
        bits.extend(["-bbox_m", mod])
    for inst in ctx.bbox_i:
        bits.extend(["-bbox_i", inst])
    bits.extend(param_args)
    covers = []
    if not ctx.rvfi_style:
        if ctx.want_vacuity_covers:
            covers.append("precondition")
        if ctx.want_witness_covers:
            covers.append("witness")
    if covers:
        bits.append("-create_related_covers {{{}}}".format(" ".join(covers)))
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

    # Jasper FPV: reset -none + assume -reset <active expr>
    if active_low:
        ctx.reset_assume_expr = "!{}".format(rst)
        ctx.classic_reset_cmd = "reset ~{}".format(rst)
    else:
        ctx.reset_assume_expr = rst
        ctx.classic_reset_cmd = "reset {}".format(rst)

    # Prefer assume -reset so the pin is not tied after the reset phase
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
    # Quick Guide also uses sim_force for sequential state override
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

    treat_as_reset = _is_reset_like(sig) or (
        ctx.reset_assume_expr is not None
        and sig in ctx.reset_assume_expr
        and not ctx.reset_emitted
    )
    # First sim_force of a reset-like net, or no reset yet and name looks like reset
    if treat_as_reset or (ctx.reset_assume_expr is None and _is_reset_like(sig)):
        ctx.reset_assume_expr = _reset_expr_from_force(sig, value)
        if ctx.reset_assume_expr.startswith("!"):
            ctx.classic_reset_cmd = "reset ~{}".format(sig)
        else:
            ctx.classic_reset_cmd = "reset {}".format(sig)
        if ctx.rvfi_style and ctx.classic_reset_cmd:
            _emit(
                ctx,
                "# VC Formal sim_force {} -apply {} → {}".format(
                    sig, value, ctx.classic_reset_cmd
                ),
            )
        else:
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

    # Non-reset sequential override → init-cycle constraint
    _emit(ctx, "# VC Formal sim_force {} -apply {} (init state)".format(sig, value))
    _map(ctx, "assume -bound 0 {{{} == {}}}".format(sig, value))
    return True


def _handle_sim_run(ctx, tokens, original):
    # type: (_Ctx, List[str], str) -> bool
    if tokens[0] != "sim_run":
        return False
    # sim_run -stable  OR  sim_run 3 -clk my_clk
    cycles = None
    clk = _clock_from_tokens(tokens)
    for t in tokens[1:]:
        if t.startswith("-"):
            continue
        if re.fullmatch(r"\d+", t):
            cycles = t
            break
    if cycles:
        ctx.reset_max_iterations = cycles
        if clk:
            _emit(
                ctx,
                "# VC Formal sim_run {} -clk {} → reset -max_iterations {}".format(
                    cycles, clk, cycles
                ),
            )
        else:
            _emit(
                ctx,
                "# VC Formal sim_run {} → reset -max_iterations {}".format(
                    cycles, cycles
                ),
            )
        if not ctx.reset_emitted:
            _emit_reset_block(ctx)
        else:
            _map(ctx, "reset -none -max_iterations {}".format(cycles))
        return True

    if ctx.reset_assume_expr and not ctx.reset_emitted:
        _emit_reset_block(ctx)
    _emit(
        ctx,
        "# sim_run -stable: Jasper reset analysis holds reset until the design is stable",
    )
    ctx.mapped += 1
    return True


def _handle_sim_save_reset(ctx, tokens, original):
    # type: (_Ctx, List[str], str) -> bool
    if tokens[0] != "sim_save_reset":
        return False
    if ctx.reset_assume_expr and not ctx.reset_emitted:
        _emit_reset_block(ctx)
    _emit(
        ctx,
        "# sim_save_reset: captured by Jasper 'reset' / 'assume -reset' above",
    )
    ctx.mapped += 1
    if ctx.want_check_reset:
        _map(ctx, "check_reset")
        ctx.want_check_reset = False
    return True


def _handle_sim_set_state(ctx, tokens, original):
    # type: (_Ctx, List[str], str) -> bool
    if tokens[0] != "sim_set_state":
        return False
    apply = _get_opt(tokens, "-apply") or ""
    apply_l = apply.replace(" ", "").lower()
    uninit = _has_flag(tokens, "-uninitialized") or _has_flag(
        tokens, "-unitialized"
    )  # Quick Guide typo
    # sim_set_state -uninitialized -user_only -apply 'b0
    if uninit or apply_l in ("'b0", "1'b0", "0", "0'b0"):
        ctx.non_resettable_regs = "0" if ("0" in apply_l or not apply_l) else apply
        _emit(
            ctx,
            "# VC Formal sim_set_state → reset -non_resettable_regs {}".format(
                ctx.non_resettable_regs
            ),
        )
        ctx.mapped += 1
        if not ctx.reset_emitted:
            _emit_reset_block(ctx)
        else:
            _map(
                ctx,
                "reset -none -non_resettable_regs {}".format(ctx.non_resettable_regs),
            )
        return True
    # sim_set_state -all -apply x  → free / X start state ≈ reset -none
    if _has_flag(tokens, "-all") or "x" in original.lower():
        _emit(ctx, "# VC Formal sim_set_state -all x → free start (reset -none)")
        if not ctx.reset_emitted:
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
    if ctx.want_check_reset:
        _map(ctx, "check_reset")
        ctx.want_check_reset = False

    if _has_flag(tokens, "-stop"):
        _map(ctx, "prove -stop")
        return True

    bits = ["prove"]
    if _has_flag(tokens, "-subtype"):
        sub = (_get_opt(tokens, "-subtype", "") or "").lower()
        if "witness" in sub or "cover" in sub:
            bits.extend(["-all", "-covers"])
        else:
            bits.append("-all")
    else:
        props = _name_list_from_tokens(
            tokens,
            skip_flags=("-block", "-bg", "-stop"),
        )
        # strip leftover option values
        props = [p for p in props if p not in ("witness", "cover", "assert")]
        if props:
            bits.extend(["-property", "{{{}}}".format(" ".join(props))])
        else:
            bits.append("-all")

    if _has_flag(tokens, "-break"):
        n = _get_opt(tokens, "-break", "1")
        bits.extend(["-cex_limit", n or "1"])

    # VC default check_fv is non-blocking; -block waits. Batch scripts prefer blocking.
    if _has_flag(tokens, "-bg") and not _has_flag(tokens, "-block"):
        bits.append("-bg")

    if ctx.max_jobs and "-max_jobs" not in bits:
        bits.extend(["-max_jobs", ctx.max_jobs])

    _map(ctx, " ".join(bits))

    finish = _get_opt(tokens, "-run_finish")
    if finish:
        _emit(ctx, "# VC Formal check_fv -run_finish callback:")
        for line in _join_logical_lines(finish.splitlines()):
            _dispatch(ctx, line)

    if ctx.want_cov:
        _map(ctx, "check_cov -measure -type all")
    return True


def _handle_report_fv(ctx, tokens, original):
    # type: (_Ctx, List[str], str) -> bool
    if tokens[0] != "report_fv":
        return False
    # Jasper 2025.03 command is `report` (report_proofs is not in the reference)
    bits = ["report"]
    if _has_flag(tokens, "-verbose"):
        bits.append("-detailed")
    elif _has_flag(tokens, "-list"):
        bits.append("-results")
    redirect = None
    m = re.search(r">\s*(\S+)", original)
    if m:
        redirect = m.group(1)
    if redirect:
        bits.extend(["-file", redirect, "-force"])
    _map(ctx, " ".join(bits))
    return True


def _emit_tcl_property(ctx, kind, tokens, original):
    # type: (_Ctx, str, List[str], str) -> bool
    """Map fvassume / fvassert / fvcover / assume / assert / cover."""
    # Pass through already-Jasper forms (assume -reset, assert -disable, ...)
    if kind == "assume" and _has_flag(tokens, "-reset"):
        _map(ctx, original.strip())
        return True
    jasper_flags = (
        "-disable",
        "-enable",
        "-from_assert",
        "-from_assume",
        "-list",
        "-show",
        "-remove",
        "-clear",
        "-replace",
        "-env",
        "-bound",
        "-constant",
        "-generate",
        "-helper",
        "-set_helper",
    )
    if tokens[0] in ("assume", "assert", "cover") and any(
        _has_flag(tokens, f) for f in jasper_flags
    ):
        _map(ctx, original.strip())
        return True

    expr = _get_opt(tokens, "-expr")
    clock = _clock_from_tokens(tokens)
    name = _get_opt(tokens, "-name")
    # Brace that looks like an expression (not a property name list)
    if expr is None:
        for t in tokens[1:]:
            if t.startswith("-"):
                continue
            body = _unbrace(t)
            if t.startswith("{") and re.search(r"==|!=|&&|\|\||\|->|>=|<=", body):
                expr = body
                break
    names = _name_list_from_tokens(tokens)
    if expr is not None:
        # drop the expression body from the name list
        names = [n for n in names if n not in expr.split()]
    if names and not name:
        # first positional is the property name when -expr is present
        name = names[0]

    if expr is not None:
        expr = _expand_vars(expr, ctx.vars)
        body = _prop_body(expr, clock)
        bits = [kind]
        if name:
            bits.extend(["-name", name])
        bits.append(body)
        _map(ctx, " ".join(bits))
        depth = _get_opt(tokens, "-depth")
        if depth:
            _emit(
                ctx,
                "# VC -depth {} → assume -bound {} if this should be a bounded constraint".format(
                    depth, depth
                ),
            )
        return True

    # No -expr: treat remaining names as existing properties whose role changes
    if names:
        joined = " ".join(names)
        if kind == "assume":
            _map(ctx, "assume -from_assert {{{}}}".format(joined))
        elif kind == "assert":
            _map(ctx, "assert -from_assume {{{}}}".format(joined))
        else:
            # cover-from-assert is not a Jasper switch; disable assert and add a cover note
            _map(ctx, "assert -disable {{{}}}".format(joined))
            _emit(
                ctx,
                "# VC fvcover {} — original is now disabled as assert; "
                "add cover -name if a cover expression is needed".format(joined),
            )
            ctx.mapped += 1
        return True

    _unmapped(ctx, original, "{} without -expr or property name".format(tokens[0]))
    return True


def _handle_fvassume(ctx, tokens, original):
    # type: (_Ctx, List[str], str) -> bool
    if tokens[0] not in ("fvassume", "assume"):
        return False
    return _emit_tcl_property(ctx, "assume", tokens, original)


def _handle_fvassert(ctx, tokens, original):
    # type: (_Ctx, List[str], str) -> bool
    if tokens[0] not in ("fvassert", "assert"):
        return False
    return _emit_tcl_property(ctx, "assert", tokens, original)


def _handle_fvcover(ctx, tokens, original):
    # type: (_Ctx, List[str], str) -> bool
    if tokens[0] not in ("fvcover", "cover"):
        return False
    return _emit_tcl_property(ctx, "cover", tokens, original)


def _handle_fvenable_disable(ctx, tokens, original):
    # type: (_Ctx, List[str], str) -> bool
    if tokens[0] not in ("fvenable", "fvdisable"):
        return False
    enable = tokens[0] == "fvenable"
    op = "-enable" if enable else "-disable"
    names = _name_list_from_tokens(tokens)
    if not names:
        _unmapped(ctx, original, "{} without property name".format(tokens[0]))
        return True
    use_re = any(_looks_glob(n) for n in names)
    if use_re:
        alts = "|".join(_glob_to_regexp(n) for n in names)
        pat = "{{{}}}".format(alts)
        _map(ctx, "assert {} {} -regexp".format(op, pat))
        _map(ctx, "cover {} {} -regexp".format(op, pat))
        _map(ctx, "assume {} {} -regexp".format(op, pat))
    else:
        body = "{{{}}}".format(" ".join(names))
        _emit(ctx, "# VC Formal {} {}".format(tokens[0], body))
        _map(
            ctx,
            "foreach _p {} {{ catch {{assert {} $_p}}; "
            "catch {{cover {} $_p}}; catch {{assume {} $_p}} }}".format(
                body, op, op, op
            ),
        )
    return True


def _handle_set_blackbox(ctx, tokens, original):
    # type: (_Ctx, List[str], str) -> bool
    if tokens[0] != "set_blackbox":
        return False
    # Quick Guide:
    #   set_blackbox -design <module_names>   → elaborate -bbox_m
    #   set_blackbox -cells <instance_names>  → elaborate -bbox_i
    is_module = _has_flag(tokens, "-design") or _has_flag(tokens, "-module") or _has_flag(
        tokens, "-modules"
    )
    is_inst = _has_flag(tokens, "-cells") or _has_flag(tokens, "-cell") or _has_flag(
        tokens, "-instance"
    ) or _has_flag(tokens, "-instances")

    names = []  # type: List[str]
    for key in (
        "-design",
        "-module",
        "-modules",
        "-cells",
        "-cell",
        "-instance",
        "-instances",
    ):
        v = _get_opt(tokens, key)
        if v:
            names.extend(_unbrace(v).split())
    if not names:
        names = _name_list_from_tokens(tokens)

    if not names:
        _unmapped(ctx, original, "set_blackbox without module/instance")
        return True

    if not is_module and not is_inst:
        # Infer: hierarchical path → instance, bare identifier → module
        is_inst = any("." in n for n in names)
        is_module = not is_inst

    for raw in names:
        name = _expand_vars(raw, ctx.vars)
        if is_inst:
            if name not in ctx.bbox_i:
                ctx.bbox_i.append(name)
            flag = "-bbox_i"
        else:
            if name not in ctx.bbox_m:
                ctx.bbox_m.append(name)
            flag = "-bbox_m"
        if ctx.elaborate_done:
            _map(ctx, "elaborate {} {}".format(flag, name))
            ctx.warnings.append(
                "set_blackbox after elaborate: re-run elaborate with {} {}".format(
                    flag, name
                )
            )
        else:
            _emit(
                ctx,
                "# blackbox {} '{}' → elaborate {}".format(
                    "instance" if is_inst else "module", name, flag
                ),
            )
            ctx.mapped += 1
    return True


def _handle_snip_driver(ctx, tokens, original):
    # type: (_Ctx, List[str], str) -> bool
    # Quick Guide spells it snip_drive; scripts use snip_driver
    if tokens[0] not in ("snip_driver", "snip_drive"):
        return False
    # snip_driver {a b c}  → stopat a b c
    sigs = []
    for t in tokens[1:]:
        if t.startswith("-"):
            continue
        body = _unbrace(t)
        sigs.extend(body.split())
    if not sigs:
        _unmapped(ctx, original, "snip_drive(r) without signals")
        return True
    _map(ctx, "stopat {}".format(" ".join(sigs)))
    return True


def _handle_set_change_at(ctx, tokens, original):
    # type: (_Ctx, List[str], str) -> bool
    if tokens[0] != "set_change_at":
        return False
    # set_change_at -default -clock clk -posedge
    clk = _clock_from_tokens(tokens)
    neg = _has_flag(tokens, "-negedge") or _has_flag(tokens, "-both")
    both = _has_flag(tokens, "-both_edges") or _has_flag(tokens, "-both")
    if not clk:
        _emit(
            ctx,
            "# set_change_at: Jasper samples on the clock edge from the 'clock' command",
        )
        ctx.mapped += 1
        return True
    if both:
        _map(ctx, "clock {} -both_edges".format(clk))
        return True
    if neg:
        _map(ctx, "clock -rate -default ~{}".format(clk))
        return True
    # posedge is Jasper default after `clock clk`
    _map(ctx, "clock -rate -default {}".format(clk))
    return True


def _handle_set_grid_usage(ctx, tokens, original):
    # type: (_Ctx, List[str], str) -> bool
    if tokens[0] != "set_grid_usage":
        return False
    # set_grid_usage -type RSH|LSF|SGE|RTDA=10
    n = None
    raw = " ".join(tokens[1:])
    m = re.search(r"=(\d+)", raw)
    if m:
        n = m.group(1)
    if not n:
        n = _get_opt(tokens, "-n") or _get_opt(tokens, "-jobs")
    farm = "local"
    low = raw.lower()
    if "lsf" in low:
        farm = "LSF"
    elif "sge" in low or "oge" in low:
        farm = "SGE/OGE"
    elif "rtda" in low:
        farm = "RTDA"
    elif "rsh" in low:
        farm = "RSH"
    if n:
        ctx.max_jobs = n
        _emit(ctx, "# VC Formal set_grid_usage ({}, {} jobs)".format(farm, n))
        _map(ctx, "set_engine_distributed_mode on")
        _map(ctx, "set_engine_processes {}".format(n))
        _emit(
            ctx,
            "# prove -max_jobs {} (also applied to check_fv). Configure Jasper farm for {}.".format(
                n, farm
            ),
        )
    else:
        _emit(ctx, "# VC Formal set_grid_usage — configure Jasper farm / prove -max_jobs")
        ctx.mapped += 1
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
    _map(ctx, "source {{{}}}".format(path))
    _emit(
        ctx,
        "# Convert the sourced VC Formal file with vcf2jg before running in Jasper",
    )
    ctx.warnings.append(
        "'source {}' kept; convert that file separately if it is VC Formal Tcl.".format(
            path
        )
    )
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
    if len(tokens) >= 3:
        _map(ctx, "set ::env({}) {}".format(tokens[1], " ".join(tokens[2:])))
    else:
        _emit(ctx, "# setenv {} — set in the shell before launching jg".format(
            " ".join(tokens[1:])
        ))
        ctx.mapped += 1
    return True


def _handle_set_constant(ctx, tokens, original):
    # type: (_Ctx, List[str], str) -> bool
    if tokens[0] != "set_constant":
        return False
    # set_constant testmode -value 0
    value = _get_opt(tokens, "-value")
    if value is None:
        value = _get_opt(tokens, "-apply")
    names = _name_list_from_tokens(tokens)
    if not names:
        _unmapped(ctx, original, "set_constant without signal")
        return True
    sig = names[0]
    if value is None:
        _map(ctx, "assume -constant {}".format(sig))
        return True
    _map(
        ctx,
        "assume -name const_{} {{{} == {}}}".format(_safe_prop_ident(sig), sig, value),
    )
    return True


def _handle_report_blackbox(ctx, tokens, original):
    # type: (_Ctx, List[str], str) -> bool
    if tokens[0] != "report_blackbox":
        return False
    _map(ctx, "get_design_info -list {bbox_mod bbox_inst}")
    return True


def _handle_report_fv_complexity(ctx, tokens, original):
    # type: (_Ctx, List[str], str) -> bool
    if tokens[0] != "report_fv_complexity":
        return False
    _map(ctx, "get_design_info")
    return True


def _handle_sim_get(ctx, tokens, original):
    # type: (_Ctx, List[str], str) -> bool
    if tokens[0] != "sim_get":
        return False
    names = _name_list_from_tokens(tokens)
    if not names:
        _unmapped(ctx, original, "sim_get without net")
        return True
    _map(ctx, "get_signal_info {}".format(names[0]))
    return True


def _handle_view_trace(ctx, tokens, original):
    # type: (_Ctx, List[str], str) -> bool
    if tokens[0] != "view_trace":
        return False
    if _has_flag(tokens, "-reset"):
        _map(ctx, "reset -analyze")
        return True
    names = _name_list_from_tokens(tokens)
    if names:
        target = names[0]
        if re.fullmatch(r"\d+", target):
            _map(
                ctx,
                "visualize -violation -property [lindex [get_property_list] {}] -new_window".format(
                    target
                ),
            )
        else:
            _map(
                ctx,
                "visualize -violation -property {{{}}} -new_window".format(target),
            )
        return True
    _map(ctx, "visualize -violation -new_window")
    return True


def _handle_save_restore_session(ctx, tokens, original):
    # type: (_Ctx, List[str], str) -> bool
    if tokens[0] not in ("save_session", "restore_session"):
        return False
    sess = _get_opt(tokens, "-session") or _get_opt(tokens, "-name")
    if tokens[0] == "save_session":
        if sess:
            _map(ctx, "save database -include session_data -force")
            _emit(ctx, "# VC Formal session name '{}' stored in the Jasper project db".format(sess))
        else:
            _map(ctx, "save database -include session_data -force")
        return True
    # restore_session
    if sess:
        _map(ctx, "restore database -session {}".format(sess))
    else:
        _map(ctx, "restore database")
    return True


def _handle_report_fml_status(ctx, tokens, original):
    # type: (_Ctx, List[str], str) -> bool
    if tokens[0] not in ("report_fml_engines", "report_fml_jobs", "report_fml_hosts"):
        return False
    if tokens[0] == "report_fml_hosts":
        _emit(ctx, "# VC Formal report_fml_hosts — Jasper farm hosts are configured outside Tcl")
        ctx.mapped += 1
        return True
    _map(ctx, "prove -status")
    _map(ctx, "get_status [get_property_list]")
    return True


def _handle_set_abstractions(ctx, tokens, original):
    # type: (_Ctx, List[str], str) -> bool
    if tokens[0] != "set_abstractions":
        return False
    blob = _get_opt(tokens, "-construct") or " ".join(
        _unbrace(t) for t in tokens[1:] if not t.startswith("-")
    )
    _emit(ctx, "# VC Formal set_abstractions -construct {{{}}}".format(blob.strip()))
    mul = re.search(r"mul\s*=\s*(\d+)", blob, re.I)
    cnt = re.search(r"counter\s*=\s*(\d+)", blob, re.I)
    _map(ctx, "get_design_info -list {multiplier counter}")
    if mul:
        _emit(
            ctx,
            "# mul={} : Jasper blackboxes multipliers at elaborate (-bbox_mul); "
            "review get_design_info -list multiplier".format(mul.group(1)),
        )
    if cnt:
        _emit(
            ctx,
            "# counter={} : after identifying counters, run "
            "abstract -counter <signal>".format(cnt.group(1)),
        )
    ctx.mapped += 1
    return True


def _handle_report_abstractions(ctx, tokens, original):
    # type: (_Ctx, List[str], str) -> bool
    if tokens[0] != "report_abstractions":
        return False
    _map(ctx, "abstract -counter -list")
    _map(ctx, "get_design_info -list {counter multiplier bbox_mod bbox_inst stopat}")
    return True


def _handle_reduced_constraints(ctx, tokens, original):
    # type: (_Ctx, List[str], str) -> bool
    if tokens[0] not in ("compute_reduced_constraints", "report_reduced_constraints"):
        return False
    if tokens[0] == "report_reduced_constraints":
        _map(ctx, "get_needed_assumptions -result")
        return True
    prop = _get_opt(tokens, "-prop") or _get_opt(tokens, "-property")
    bits = ["get_needed_assumptions"]
    if prop:
        bits.extend(["-property", prop])
    else:
        bits.extend(["-property", "[lindex [get_property_list -include {status proven}] 0]"])
    _map(ctx, " ".join(bits))
    return True


def _handle_formal_core(ctx, tokens, original):
    # type: (_Ctx, List[str], str) -> bool
    if tokens[0] not in (
        "compute_formal_core",
        "report_formal_core",
        "compute_formal_core_coverage",
        "save_formal_core_results",
        "save_cov_exclusion",
    ):
        return False
    cmd = tokens[0]
    prop = _get_opt(tokens, "-property") or _get_opt(tokens, "-prop")
    if cmd == "report_formal_core":
        _map(ctx, "report -json -config cov")
        return True
    if cmd == "save_formal_core_results":
        db = _get_opt(tokens, "-db_name") or "formal_core_cov"
        _map(ctx, "check_cov -export {}".format(db))
        return True
    if cmd == "save_cov_exclusion":
        fn = _get_opt(tokens, "-file") or "uncoverables.el"
        _map(ctx, "check_cov -export {}".format(fn))
        _emit(ctx, "# Review Jasper coverage waivers for uncoverable items")
        return True
    # compute_formal_core / compute_formal_core_coverage
    if not ctx.cov_init_emitted:
        _map(
            ctx,
            "check_cov -init -type proof -model "
            "{statement branch expression toggle fsm property}",
        )
        ctx.cov_init_emitted = True
        ctx.want_cov = True
    bits = ["check_cov", "-measure", "-type", "proof"]
    if prop:
        bits.extend(["-property", prop])
    if _has_flag(tokens, "-bg"):
        bits.append("-bg")
    _map(ctx, " ".join(bits))
    return True


def _handle_fvtask(ctx, tokens, original):
    # type: (_Ctx, List[str], str) -> bool
    if tokens[0] not in ("fvtask", "get_fvtask"):
        return False
    if tokens[0] == "get_fvtask":
        _map(ctx, "task")
        return True
    # fvtask -create name
    if _has_flag(tokens, "-create"):
        name = _get_opt(tokens, "-create")
        if not name:
            names = _name_list_from_tokens(tokens)
            name = names[0] if names else None
        if not name:
            _unmapped(ctx, original, "fvtask -create without name")
            return True
        bits = ["task", "-create", name]
        if _has_flag(tokens, "-copy"):
            src = _get_opt(tokens, "-copy")
            if src:
                bits.extend(["-source_task", src])
            copy_map = {
                "-assumes": "-copy_assumes",
                "-asserts": "-copy_asserts",
                "-covers": "-copy_covers",
                "-snips": "-copy_stopats",
                "-constants": "-copy_assumes",
                "-changeats": "",
            }
            copied = False
            for vc, jg in copy_map.items():
                if _has_flag(tokens, vc) and jg:
                    bits.append(jg)
                    copied = True
            if not copied:
                bits.append("-copy_all")
        _map(ctx, " ".join(bits))
        return True
    # fvtask name -copy src
    if _has_flag(tokens, "-copy"):
        src = _get_opt(tokens, "-copy")
        names = _name_list_from_tokens(tokens)
        dest = names[0] if names else None
        if dest and src:
            bits = ["task", "-create", dest, "-source_task", src]
            if _has_flag(tokens, "-assumes"):
                bits.append("-copy_assumes")
            elif _has_flag(tokens, "-asserts"):
                bits.append("-copy_asserts")
            elif _has_flag(tokens, "-covers"):
                bits.append("-copy_covers")
            elif _has_flag(tokens, "-snips"):
                bits.append("-copy_stopats")
            else:
                bits.append("-copy_all")
            _map(ctx, " ".join(bits))
            return True
    # fvtask name  → switch current task
    names = _name_list_from_tokens(tokens)
    if names:
        _map(ctx, "task -set {}".format(names[0]))
        return True
    _map(ctx, "task")
    return True


def _handle_help_man(ctx, tokens, original):
    # type: (_Ctx, List[str], str) -> bool
    if tokens[0] not in ("help", "man"):
        return False
    rest = " ".join(tokens[1:]).strip()
    # rewrite well-known VC command names
    repl = {
        "report_fv": "report",
        "check_fv": "prove",
        "fvassume": "assume",
        "fvassert": "assert",
        "fvcover": "cover",
        "fvtask": "task",
        "snip_drive": "stopat",
        "snip_driver": "stopat",
        "view_trace": "visualize",
        "save_session": "save",
        "set_blackbox": "elaborate",
        "set_constant": "assume",
        "create_clock": "clock",
        "create_reset": "reset",
        "read_file": "analyze",
        "analyze_design": "analyze",
    }
    for vc, jg in repl.items():
        if rest == vc or rest.startswith(vc + " "):
            rest = jg + rest[len(vc):]
            break
    _map(ctx, "help {}".format(rest).rstrip())
    return True


def _handle_report_vars(ctx, tokens, original):
    # type: (_Ctx, List[str], str) -> bool
    if tokens[0] not in ("report_fml_var", "report_app_var"):
        return False
    _emit(ctx, "# VC Formal {} → Jasper: help / set_* configuration commands".format(tokens[0]))
    _map(ctx, "help set_engine_mode")
    return True


def _handle_get_props(ctx, tokens, original):
    # type: (_Ctx, List[str], str) -> bool
    if tokens[0] != "get_props":
        return False
    status = _get_opt(tokens, "-status")
    if status:
        _map(ctx, "get_property_list -include {{status {}}}".format(status))
    else:
        _map(ctx, "get_property_list")
    return True


# Order matters: more specific first
_HANDLERS = [
    _handle_set_app_var,
    _handle_set_fml_var,
    _handle_set_fml_appmode,
    _handle_set_blackbox,
    _handle_set_constant,
    _handle_set_change_at,
    _handle_set_grid_usage,
    _handle_set_abstractions,
    _handle_read_file,
    _handle_analyze,
    _handle_elaborate,
    _handle_create_clock,
    _handle_create_reset,
    _handle_sim_force,
    _handle_sim_run,
    _handle_sim_save_reset,
    _handle_sim_set_state,
    _handle_sim_get,
    _handle_check_fv,
    _handle_report_fv,
    _handle_report_fv_complexity,
    _handle_report_blackbox,
    _handle_report_abstractions,
    _handle_report_fml_status,
    _handle_report_vars,
    _handle_fvassume,
    _handle_fvassert,
    _handle_fvcover,
    _handle_fvenable_disable,
    _handle_fvtask,
    _handle_snip_driver,
    _handle_view_trace,
    _handle_save_restore_session,
    _handle_reduced_constraints,
    _handle_formal_core,
    _handle_get_props,
    _handle_help_man,
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

    expanded = _rewrite_vc_cmd_subs(_expand_vars(line, ctx.vars))
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
        "define_proc_attributes",
    ):
        _emit(ctx, "# VC Formal {} (Jasper batch FPV does not use this)".format(cmd))
        ctx.mapped += 1
        return

    if cmd in ("start_gui", "gui_start"):
        _map(ctx, "gui")
        return

    # Jasper-native commands already in the input: keep them
    if cmd in (
        "prove",
        "report",
        "visualize",
        "task",
        "stopat",
        "abstract",
        "get_design_info",
        "get_property_list",
        "get_needed_assumptions",
        "get_status",
        "get_signal_info",
        "check_cov",
        "check_reset",
        "set_engine_mode",
        "set_prove_time_limit",
        "set_max_trace_length",
        "set_engine_processes",
        "set_database_auto_save_restore",
        "save",
        "restore",
        "clear",
    ):
        _map(ctx, expanded.strip())
        return
    if cmd == "clock" and len(tokens) >= 2:
        _map(ctx, expanded.strip())
        ctx.clock_emitted = True
        return
    if cmd == "reset" and len(tokens) >= 2:
        _map(ctx, expanded.strip())
        ctx.reset_emitted = True
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

    if ctx.want_check_reset:
        _map(ctx, "check_reset")
        ctx.want_check_reset = False

    # Coverage measure if COV mode and prove was never called
    if ctx.want_cov and ctx.cov_init_emitted:
        body_txt = "\n".join(ctx.out)
        if "check_cov -measure" not in body_txt and "prove" in body_txt:
            pass  # already handled in check_fv
        elif "check_cov -measure" not in body_txt and ctx.appmode in ("COV", "FCA"):
            _emit(ctx, "check_cov -measure -type all")
            ctx.mapped += 1

    if ctx.rvfi_style:
        ctx.warnings.append(
            "RVFI/NERV: analyze -sv12 -mfcu +incdir; reset <sig>; "
            "convert_file patches sibling defines.sv (CSR last-line '\\', YOSYS)."
        )

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
    """Convert a VC Formal Tcl file; optionally write JasperGold output.

    Also patches a sibling riscv-formal ``defines.sv`` so JasperGold does not
    hit VERI-1137 from a trailing ``\\`` on CSR macros.
    """
    input_path = Path(input_path)
    text = input_path.read_text(encoding="utf-8", errors="replace")
    result = convert_vc_tcl(text, source_name=str(input_path))
    patch_companion_defines(input_path, result)
    if output_path is None:
        output_path = input_path.with_name(input_path.stem + "_jg.tcl")
    output_path.write_text(result.jasper_tcl, encoding="utf-8")
    return result
