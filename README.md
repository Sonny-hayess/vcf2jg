# vcf2jg — VC Formal Tcl → JasperGold Tcl

Python converter that translates **Synopsys VC Formal** run scripts into
**Cadence JasperGold** Tcl.

This is a **best-effort FPV flow translator**, not a full Tcl interpreter.
Tool-specific apps (FTA, AEP, SEC) and advanced options need human review
(see `[vcf2jg TODO]` comments in the output). Compatible with Python 3.6+.

SVA modules, bind files, and RTL are **not** rewritten — only the tool
control script.

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

## Typical conversion

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

becomes:

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

`read_file -top … -vcs {-f filelist.f}` is converted the same way (`analyze -f` +
`elaborate -top`). Reset from `sim_force` / `create_reset` is:

```tcl
reset -none
assume -reset <active_condition>
```

- Active high (`sim_force rst -apply 1'b1` or `-sense high`) → `assume -reset rst`
- Active low (`sim_force rst_n -apply 1'b0` or `-sense low`) → `assume -reset !rst_n`

riscv-formal / RVFI checks use `analyze -sv12 -mfcu` with `+incdir`, Jasper
`reset <sig>`, and a patched sibling `defines.sv`.

## Command mapping

Mappings follow the **VC Formal Property Checking App Quick Guide** and
Jasper Apps Command Reference 2025.03.

| VC Formal | JasperGold |
|-----------|------------|
| `set_fml_appmode FPV` | Comment (FPV-style flow) |
| `set_fml_appmode COV` / `FCA` | Enables `check_cov -init` / `-measure` |
| `set_app_var fml_auto_save` | `set_database_auto_save_restore` |
| `set_app_var` (other) | Comment (no 1:1 Jasper var) |
| `set_fml_var fml_witness_on` / `fml_vacuity_on` | `elaborate -create_related_covers {witness\|precondition}` |
| `set_fml_var fml_effort high\|bounded\|bug_hunting\|discovery` | `set_engine_mode {Ht Hp B I N}` / `{B}` / `{Bm J}` / `{D}` |
| `set_fml_var fml_max_time 2H` | `set_prove_time_limit 2h` |
| `set_fml_var fml_progress_time_limit` | `set_prove_time_limit` |
| `set_fml_var fml_reset_property_check true` | `check_reset` |
| `analyze` / `analyze_design -format sverilog -vcs "a.sv"` | `analyze -sv09 a.sv` |
| `analyze ... -vcs "-assert svaext f.sv"` | `analyze -sv09 f.sv` |
| `analyze ... { multi-line files }` | `analyze -sv09 files...` |
| `analyze ... +incdir+PATH` | `analyze -sv09 +incdir+PATH ...` |
| `analyze ... rvfi_macros.vh ...` | `analyze -sv12 -mfcu +incdir+<dir> ...` (header) |
| sibling `defines.sv` (riscv-formal) | last-line CSR `\` stripped; `` `define YOSYS `` dropped |
| `read_file -top X -vcs {-f flist.f}` | `clear -all` / `analyze -f flist.f` / `elaborate -top X` |
| `elaborate $top -verbose -sva` | `elaborate -top $top -create_related_covers {precondition witness}` |
| `elaborate t -vcs "-pvalue+A=1"` | `elaborate -top t -parameter A 1 ...` |
| `set_blackbox -design M` | `elaborate -bbox_m M` |
| `set_blackbox -cells inst` | `elaborate -bbox_i inst` |
| `report_blackbox` | `get_design_info -list {bbox_mod bbox_inst}` |
| `set_constant sig -value 0` | `assume -name const_sig {sig == 0}` |
| `create_clock clk -period N` | `clock clk` |
| `create_reset rst -sense high` / `-high` | `reset -none` + `assume -reset rst` |
| `create_reset rst_n -sense low` / `-low` | `reset -none` + `assume -reset !rst_n` |
| `sim_force rst -apply 1'b1` | `reset -none` + `assume -reset rst` |
| `sim_force rst_n -apply 1'b0` | `reset -none` + `assume -reset !rst_n` |
| `sim_force sig -apply val` (non-reset) | `assume -bound 0 {sig == val}` |
| `sim_run -stable` / `sim_save_reset` | Covered by Jasper reset analysis |
| `sim_run N -clk clk` | `reset -none -max_iterations N` |
| `sim_set_state -uninitialized -apply 'b0` | `reset -non_resettable_regs 0` |
| `sim_get net` | `get_signal_info net` |
| `report_fv_complexity` | `get_design_info` |
| `set_change_at -default -clock clk -posedge` | `clock -rate -default clk` |
| `set_grid_usage -type LSF=N` | `set_engine_processes N` + `prove -max_jobs N` |
| `fvassume prop` | `assume -from_assert {prop}` |
| `fvassume -expr {...}` | `assume {...}` |
| `fvassert name -expr {...}` | `assert -name name {...}` |
| `fvcover name -expr {...}` | `cover -name name {...}` |
| `fvenable` / `fvdisable` | `assert/cover/assume -enable` / `-disable` |
| `check_fv` / `check_fv -block` | `prove -all` |
| `check_fv -subtype witness` | `prove -all -covers` |
| `check_fv -break N` | `prove -all -cex_limit N` |
| `check_fv -stop` | `prove -stop` |
| `check_fv -run_finish {report_fv}` | `prove -all` then converted callback |
| `report_fv` / `-list` / `-verbose` | `report` / `report -results` / `report -detailed` |
| `view_trace prop` | `visualize -violation -property {prop} -new_window` |
| `view_trace -reset` | `reset -analyze` |
| `snip_drive(r) {a b}` | `stopat a b` |
| `set_abstractions -construct {mul=N counter=M}` | `get_design_info -list {multiplier counter}` + `abstract -counter` |
| `compute_reduced_constraints -prop p` | `get_needed_assumptions -property p` |
| `compute_formal_core -property ...` | `check_cov -init/-measure -type proof` |
| `save_session` | `save database -include session_data` |
| `fvtask -create T` | `task -create T` |
| `fvtask T -copy S` | `task -create T -source_task S -copy_all` |
| `fvtask T` / `get_fvtask` | `task -set T` / `task` |
| `get_props -status proven` | `get_property_list -include {status proven}` |
| `help` / `man` | `help` |
| `cd $SCRIPT_LOCATION` | Kept |
| Simple `if {$flag} { ... }` | Evaluated when flag is a known 0/1 |
| `fta_init` / `compute_fta` | TODO (no 1:1 FPV mapping) |

## Tests

```bash
PYTHONPATH=. python3 -m unittest tests.test_converter -v
```
