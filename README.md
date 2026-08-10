# vcf2jg — VC Formal Tcl → JasperGold Tcl

Python converter that translates common **Synopsys VC Formal** run scripts into
**Cadence JasperGold** Tcl.

This is a **best-effort FPV flow translator**, not a full Tcl interpreter.
Tool-specific apps (FTA, AEP, SEC) and advanced options need human review
(see `[vcf2jg TODO]` comments in the output).

## Install

```bash
cd vcf2jg
pip install -e .
# or run without install:
PYTHONPATH=. python3 -m vcf2jg path/to/vc_script.tcl -o /tmp/out.tcl -v
```

## Usage

```bash
# Write <input>_jg.tcl next to the input
vcf2jg path/to/vc_script.tcl

# Explicit output path
vcf2jg path/to/vc_script.tcl -o jg_run.tcl

# Stdout
vcf2jg path/to/vc_script.tcl --stdout

# From stdin
vcf2jg - < vc_script.tcl -o jg_run.tcl

# Verbose: stats + warnings on stderr
vcf2jg path/to/vc_script.tcl -v
```

## Supported dialects

### 1. LUBIS / modern VC Formal (trainee labs)

```tcl
analyze -format sverilog -vcs "design.sv"
analyze -format sverilog -vcs "-assert svaext fv_design.sv"
set top design
elaborate $top -verbose -sva
create_clock clk -period 2
sim_force rst -apply 1'b1
sim_run -stable
sim_save_reset
check_fv
```

→

```tcl
clear -all
analyze -sv09 design.sv
analyze -sv09 fv_design.sv
elaborate -top design -create_related_covers {precondition witness}
clock clk
reset -none
assume -reset rst
prove -all
```

### 2. Classic `read_file` FPV flow

```tcl
read_file -top $design -format sverilog -sva -vcs {-f filelist.f}
create_reset rst -high
```

## Command mapping

| VC Formal | JasperGold |
|-----------|------------|
| `set_fml_appmode FPV` | Comment (FPV-style flow) |
| `set_fml_appmode COV` / `FCA` | Enables `check_cov -init` / `-measure` |
| `set_app_var` / `set_fml_var` | Skipped |
| `analyze -format sverilog -vcs "a.sv"` | `analyze -sv09 a.sv` |
| `analyze ... -vcs "-assert svaext f.sv"` | `analyze -sv09 f.sv` |
| `analyze ... { multi-line files }` | `analyze -sv09 files...` |
| `analyze ... +incdir+PATH` | `analyze -sv09 +incdir+PATH ...` |
| `read_file -top X -vcs {-f flist.f}` | `clear -all` / `analyze -f flist.f` / `elaborate -top X` |
| `elaborate $top -verbose -sva` | `elaborate -top $top -create_related_covers {precondition witness}` |
| `elaborate t -vcs "-pvalue+A=1"` | `elaborate -top t -parameter A 1 ...` |
| `create_clock clk -period N` | `clock clk` |
| `create_reset rst -sense high` / `-high` | `reset -none` + `assume -reset rst` |
| `create_reset rst_n -sense low` / `-low` | `reset -none` + `assume -reset !rst_n` |
| `sim_force rst -apply 1'b1` | `reset -none` + `assume -reset rst` |
| `sim_force rst_n -apply 1'b0` | `reset -none` + `assume -reset !rst_n` |
| `sim_run -stable` / `sim_save_reset` | Skipped (covered by reset setup) |
| `check_fv` / `check_fv -block` | `prove -all` |
| `check_fv -subtype witness` | `prove -all -covers` |
| `report_fv -list > file` | `report_proofs -verbose > file` |
| `fvassume -expr {...}` | `assume {...}` |
| `set_blackbox -cells X` | `elaborate -bbox_m X` |
| `snip_driver {a b}` | `stopat a b` |
| `set_change_at ...` | Skipped (review `clock -rate`) |
| `cd $SCRIPT_LOCATION` | Kept |
| Simple `if {$flag} { ... }` | Evaluated when flag is a known 0/1 |
| `fta_init` / `compute_fta` | TODO (no 1:1 FPV mapping) |

SVA modules, bind files, and RTL are **not** rewritten — only the tool control script.

## Reset style

LUBIS hand-written Jasper scripts use:

```tcl
reset -none
assume -reset <active_condition>
```

This matches VC Formal `sim_force` / `reset_without_constraint` (inform the tool
about reset without permanently tying the pin inactive after reset).

- Active high (`sim_force rst -apply 1'b1` or `-sense high`) → `assume -reset rst`
- Active low (`sim_force rst_n -apply 1'b0` or `-sense low`) → `assume -reset !rst_n`

## Example (assignment_05 processor)

```bash
vcf2jg trainee/assignment_05_processor/design/vcf_prove.tcl -o jg_prove.tcl -v
```

## Tests

```bash
PYTHONPATH=. python3 -m unittest tests.test_converter -v
```
