"""Unit tests for vcf2jg converter."""

from pathlib import Path
import tempfile
import unittest

from vcf2jg.converter import (
    convert_file,
    convert_vc_tcl,
    fix_riscv_formal_defines,
)

EXAMPLES = Path(__file__).resolve().parents[1] / "examples"
TRAINEE_A5 = Path(
    "/home/Jayasimha/trainee/assignment_05_processor/design/vcf_prove.tcl"
)


class TestConverter(unittest.TestCase):
    def test_fpv_arbiter_basic(self):
        text = (EXAMPLES / "vc_fpv_arbiter.tcl").read_text()
        result = convert_vc_tcl(text, source_name="vc_fpv_arbiter.tcl")
        out = result.jasper_tcl

        self.assertIn("clear -all", out)
        self.assertIn("analyze", out)
        self.assertIn("-f filelist.f", out)
        self.assertIn("elaborate -top arbiter", out)
        self.assertIn("clock clk", out)
        self.assertIn("reset -none", out)
        self.assertIn("assume -reset rst", out)
        self.assertIn("prove -all", out)
        self.assertIn("report", out)
        self.assertTrue(
            any(
                "sim_run" in line and line.strip().startswith("#")
                for line in out.splitlines()
            )
        )
        self.assertGreaterEqual(result.mapped_count, 5)
        self.assertEqual(result.unmapped_count, 0)

    def test_active_low_reset_and_assume(self):
        text = (EXAMPLES / "vc_fpv_with_assume.tcl").read_text()
        result = convert_vc_tcl(text)
        out = result.jasper_tcl

        self.assertIn("elaborate -top ntt_butterfly", out)
        self.assertIn("reset -none", out)
        self.assertIn("assume -reset !rst_n", out)
        self.assertIn("scan_en == 1'b0", out)
        self.assertIn("!test_mode", out)
        self.assertIn("assume", out)
        self.assertIn("prove -all", out)

    def test_multiline_quoted_vcs_var(self):
        text = r'''
set ENV_DIR ..
set RTL_DIR $ENV_DIR/design
set SVA_DIR $ENV_DIR/sva
set vcs "
   ${RTL_DIR}/packet_ctrl.sv
   ${SVA_DIR}/packet_ctrl_checker.sv
"
read_file -sva -top packet_ctrl -format sverilog -vcs "$vcs"
'''
        result = convert_vc_tcl(text)
        out = result.jasper_tcl
        self.assertIn("packet_ctrl.sv", out)
        self.assertIn("packet_ctrl_checker.sv", out)
        self.assertNotIn("no mapping for", out)
        self.assertEqual(result.unmapped_count, 0, msg=str(result.warnings))

    def test_read_file_keeps_sva_beside_filelist(self):
        text = r"""
set design traffic
read_file -top $design -format sverilog -sva \
  -vcs {-f ../design/filelist +define+INLINE_SVA \
   ../sva/traffic.sva ../sva/bind_traffic.sva}
"""
        out = convert_vc_tcl(text).jasper_tcl
        self.assertIn("-f ../design/filelist", out)
        self.assertIn("+define+INLINE_SVA", out)
        self.assertIn("traffic.sva", out)
        self.assertIn("bind_traffic.sva", out)

    def test_variable_expansion(self):
        text = """
set design my_top
set flist ./files.f
read_file -top $design -format sverilog -sva -vcs {-f $flist}
create_clock clk -period 5
create_reset reset -high
check_fv -block
"""
        result = convert_vc_tcl(text)
        self.assertIn("elaborate -top my_top", result.jasper_tcl)
        self.assertIn("-f ./files.f", result.jasper_tcl)
        self.assertIn("assume -reset reset", result.jasper_tcl)

    def test_line_continuation(self):
        text = r"""
set design \
  foo
read_file -top $design -format sverilog -sva -vcs {-f a.f}
"""
        result = convert_vc_tcl(text)
        self.assertIn("elaborate -top foo", result.jasper_tcl)

    def test_unmapped_command_warning(self):
        text = "some_unknown_vc_cmd -foo bar\n"
        result = convert_vc_tcl(text)
        self.assertGreaterEqual(result.unmapped_count, 1)
        self.assertIn("TODO", result.jasper_tcl)
        self.assertTrue(result.warnings)

    def test_create_reset_polarity(self):
        high = convert_vc_tcl("create_reset rst -high\n").jasper_tcl
        low = convert_vc_tcl("create_reset rst_n -low\n").jasper_tcl
        self.assertIn("assume -reset rst", high)
        self.assertIn("assume -reset !rst_n", low)

        sense_high = convert_vc_tcl("create_reset rst -sense high\n").jasper_tcl
        sense_low = convert_vc_tcl("create_reset rst_n -sense low\n").jasper_tcl
        self.assertIn("assume -reset rst", sense_high)
        self.assertIn("assume -reset !rst_n", sense_low)

    def test_stdin_style_empty_has_header(self):
        result = convert_vc_tcl("")
        self.assertIn("vcf2jg", result.jasper_tcl)

    def test_lubis_analyze_elaborate_sim_force(self):
        """Core dialect used by trainee assignment scripts."""
        text = r"""
set SCRIPT_LOCATION [file dirname [file normalize [info script]]]
cd $SCRIPT_LOCATION
set_fml_appmode FPV
set_app_var apply_bind_in_all_units true
set_fml_var fml_witness_on true

analyze -format sverilog -vcs "processor_package.sv"
analyze -format sverilog -vcs "processor.sv"
analyze -format sverilog -vcs "-assert svaext fv_processor.sv"

set top processor
set elaborateOption -verbose
elaborate $top $elaborateOption -sva

create_clock clk -period 2
sim_force rst -apply 1'b1
sim_run -stable
sim_save_reset
check_fv
"""
        result = convert_vc_tcl(text, source_name="vcf_prove.tcl")
        out = result.jasper_tcl
        self.assertIn("clear -all", out)
        self.assertIn("analyze -sv09 processor_package.sv", out)
        self.assertIn("analyze -sv09 processor.sv", out)
        self.assertIn("analyze -sv09 fv_processor.sv", out)
        self.assertIn("elaborate -top processor", out)
        self.assertIn("-create_related_covers {precondition witness}", out)
        self.assertIn("clock clk", out)
        self.assertIn("reset -none", out)
        self.assertIn("assume -reset rst", out)
        self.assertIn("prove -all", out)
        # script location should keep command substitution
        self.assertIn("set SCRIPT_LOCATION [file dirname", out)
        self.assertIn("cd $SCRIPT_LOCATION", out)
        # no leftover unmapped analyze/elaborate/sim_force
        self.assertNotIn("no mapping for 'analyze'", out)
        self.assertNotIn("no mapping for 'elaborate'", out)
        self.assertNotIn("no mapping for 'sim_force'", out)
        self.assertEqual(result.unmapped_count, 0)

    def test_multiline_analyze_brace_list(self):
        text = r"""
analyze -format sverilog -vcs " " {
    lubis_latch.sv
    mmu.sv
}
analyze -format sverilog -vcs " -assert svaext +incdir+../rtl " {
    fv_mmu.sv
}
elaborate mmu -verbose -sva
create_clock clk -period 200
sim_force rst_n -apply 1'b0
check_fv
"""
        out = convert_vc_tcl(text).jasper_tcl
        self.assertIn("analyze -sv09 lubis_latch.sv mmu.sv", out)
        self.assertIn("+incdir+../rtl", out)
        self.assertIn("fv_mmu.sv", out)
        self.assertIn("elaborate -top mmu", out)
        self.assertIn("assume -reset !rst_n", out)

    def test_rvfi_macros_vh_becomes_incdir(self):
        # Jasper VERI-1245: `include "rvfi_macros.vh" needs +incdir, not a source file.
        text = r"""
analyze -format sverilog -vcs -verbose {
    ../../../../checks/rvfi_macros.vh
    defines.sv
    ../../../../checks/rvfi_channel.sv
    ../../../../checks/rvfi_testbench.sv
    ../../../../checks/rvfi_insn_check.sv
    ../../../../insns/insn_jal.v
    ../../wrapper.sv
    ../../nerv.sv
}
elaborate rvfi_testbench -verbose -sva
create_clock clock -period 200
sim_force reset -apply 1'b1
check_fv -block
"""
        out = convert_vc_tcl(text).jasper_tcl
        self.assertIn("analyze -sv12 -mfcu", out)
        self.assertIn("+incdir+.", out)
        self.assertIn("+incdir+../../../../checks", out)
        self.assertIn("+incdir+../../../../insns", out)
        self.assertIn("defines.sv", out)
        self.assertIn("../../wrapper.sv", out)
        self.assertIn("../../nerv.sv", out)
        self.assertNotRegex(out, r"analyze[^\n]*\brvfi_macros\.vh\b")
        self.assertIn("reset reset", out)
        self.assertNotIn("reset -none", out)
        self.assertNotIn("assume -reset", out)
        self.assertNotIn("-create_related_covers", out)
        self.assertIn("elaborate -top rvfi_testbench", out)

    def test_fix_riscv_formal_defines_strips_last_line_backslash(self):
        src = (
            "`define RISCV_FORMAL_CUSTOM_CSR_WIRES \\\n"
            "wire a; \\\n"
            "`define RISCV_FORMAL_CUSTOM_CSR_CONN \\\n"
            ",.p (p) \\\n"
            "`define YOSYS // Hotfix for older Tabby CAD Releases\n"
            '`include "rvfi_macros.vh"\n'
        )
        out = fix_riscv_formal_defines(src)
        self.assertNotIn("`define YOSYS", out)
        self.assertIn("wire a;\n`define RISCV_FORMAL_CUSTOM_CSR_CONN", out)
        self.assertIn(",.p (p)\n`include", out)
        self.assertIn("`define RISCV_FORMAL_CUSTOM_CSR_WIRES \\", out)

    def test_convert_file_patches_sibling_defines_sv(self):
        broken = (
            "`define RISCV_FORMAL\n"
            "`define RISCV_FORMAL_CUSTOM_CSR_WIRES \\\n"
            "wire a; \\\n"
            "`define RISCV_FORMAL_CUSTOM_CSR_CONN \\\n"
            ",.p (p) \\\n"
            "`define YOSYS // Hotfix for older Tabby CAD Releases\n"
            '`include "rvfi_macros.vh"\n'
        )
        tcl = r"""
analyze -format sverilog -vcs -verbose {
    ../../../../checks/rvfi_macros.vh
    defines.sv
    ../../../../checks/rvfi_testbench.sv
    ../../nerv.sv
}
elaborate rvfi_testbench -verbose -sva
create_clock clock -period 200
sim_force reset -apply 1'b1
check_fv -block
"""
        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            (d / "defines.sv").write_text(broken)
            inp = d / "insn_jal_ch0.tcl"
            inp.write_text(tcl)
            outp = d / "insn_jal_ch0_jg.tcl"
            result = convert_file(inp, outp)
            fixed = (d / "defines.sv").read_text()
            self.assertNotIn("`define YOSYS", fixed)
            self.assertIn("wire a;\n`define RISCV_FORMAL_CUSTOM_CSR_CONN", fixed)
            self.assertTrue(outp.is_file())
            jg = outp.read_text()
            self.assertIn("analyze -sv12 -mfcu", jg)
            self.assertIn("reset reset", jg)
            self.assertTrue(any("Patched" in w for w in result.warnings))

    def test_pvalue_parameters(self):
        text = r"""
analyze -format sverilog -vcs "fifo.sv"
elaborate fifo -vcs "-pvalue+DEPTH=4 -pvalue+WIDTH=8" -verbose -sva
create_clock clk -period 2
create_reset reset -sense high
check_fv -block
"""
        out = convert_vc_tcl(text).jasper_tcl
        self.assertIn("-parameter DEPTH 4", out)
        self.assertIn("-parameter WIDTH 8", out)
        self.assertIn("elaborate -top fifo", out)

    def test_if_reset_without_constraint(self):
        text = r"""
set reset_without_constraint 1
set auto_check 1
set auto_witness 0
set exit_after_execution 0
analyze -format sverilog -vcs "mmu.sv"
elaborate mmu -verbose -sva
create_clock clk -period 200
if {$reset_without_constraint} {
    sim_force rst_n -apply 1'b0
} else {
    create_reset rst_n -sense low
}
if {$auto_check} {
    check_fv
}
if {$exit_after_execution} {
    exit
}
"""
        result = convert_vc_tcl(text)
        out = result.jasper_tcl
        self.assertIn("assume -reset !rst_n", out)
        self.assertIn("prove -all", out)
        self.assertNotIn("\nexit\n", out)
        self.assertEqual(result.unmapped_count, 0)

    def test_snip_driver_to_stopat(self):
        text = "snip_driver {a.b c.d}\n"
        out = convert_vc_tcl(text).jasper_tcl
        self.assertIn("stopat a.b c.d", out)

    def test_assignment5_file(self):
        if not TRAINEE_A5.is_file():
            self.skipTest("trainee assignment_05 not present")
        text = TRAINEE_A5.read_text(encoding="utf-8", errors="replace")
        result = convert_vc_tcl(text, source_name=str(TRAINEE_A5))
        out = result.jasper_tcl
        # Match hand-written Documents/trainee jasper.tcl intent
        self.assertIn("clear -all", out)
        self.assertIn("analyze -sv09 processor_package.sv", out)
        self.assertIn("analyze -sv09 processor.sv", out)
        self.assertIn("analyze -sv09 fv_processor.sv", out)
        self.assertIn("elaborate -top processor", out)
        self.assertIn("clock clk", out)
        self.assertIn("reset -none", out)
        self.assertIn("assume -reset rst", out)
        self.assertIn("prove -all", out)
        self.assertEqual(
            result.unmapped_count,
            0,
            msg="unmapped: {}".format(result.warnings),
        )


class TestQuickGuideCommands(unittest.TestCase):
    """Commands from vc-formal-fpv-app-quick-guide_compress.pdf."""

    def test_analyze_design_and_set_constant(self):
        text = r"""
set_fml_appmode FPV
set_blackbox -design secret_mod
set_blackbox -cells top.u_skip
analyze_design -format sverilog -vcs test.v
elaborate -sva my_top
set_constant testmode -value 0
create_clock my_clk -period 100
create_reset my_rst -high
"""
        out = convert_vc_tcl(text).jasper_tcl
        self.assertIn("analyze", out)
        self.assertIn("test.v", out)
        self.assertIn("-bbox_m secret_mod", out)
        self.assertIn("-bbox_i top.u_skip", out)
        self.assertIn("assume -name const_testmode {testmode == 0}", out)
        self.assertIn("clock my_clk", out)
        self.assertIn("assume -reset my_rst", out)

    def test_property_role_and_expr(self):
        text = r"""
fvassume prop_name
fvassert ast_a -clock clk -expr {a |-> b}
fvcover cov_pkt -clock clk -expr {pkt_sop}
fvenable keep_me
fvdisable {skip.me skip.you}
"""
        out = convert_vc_tcl(text).jasper_tcl
        self.assertIn("assume -from_assert {prop_name}", out)
        self.assertIn("assert -name ast_a", out)
        self.assertIn("a |-> b", out)
        self.assertIn("cover -name cov_pkt", out)
        self.assertIn("pkt_sop", out)
        self.assertIn("assert -enable", out)
        self.assertIn("assert -disable", out)

    def test_effort_time_grid_and_check_options(self):
        text = r"""
set_fml_var fml_effort high
set_fml_var fml_max_time 2H
set_fml_var fml_max_memory 8GB
set_grid_usage -type LSF=10
check_fv -block
check_fv -break 2
check_fv -stop
report_fv -list > fpv_results.txt
report_fv -verbose
"""
        result = convert_vc_tcl(text)
        out = result.jasper_tcl
        self.assertIn("set_engine_mode {Ht Hp B I N}", out)
        self.assertIn("set_prove_time_limit 2h", out)
        self.assertIn("8GB", out)
        self.assertIn("set_engine_distributed_mode on", out)
        self.assertIn("set_engine_processes 10", out)
        self.assertIn("prove -all", out)
        self.assertIn("-cex_limit 2", out)
        self.assertIn("prove -stop", out)
        self.assertIn("report -results -file fpv_results.txt -force", out)
        self.assertIn("report -detailed", out)
        self.assertEqual(result.unmapped_count, 0)

    def test_init_state_and_debug(self):
        text = r"""
create_clock clk -period 10
sim_force sig_name -apply 3'b000
sim_run 3 -clk my_clk
sim_set_state -uninitialized -user_only -apply 'b0
sim_save_reset
sim_get net_name
view_trace -reset
view_trace my_prop
report_fv_complexity
report_blackbox
"""
        result = convert_vc_tcl(text)
        out = result.jasper_tcl
        self.assertIn("assume -bound 0 {sig_name == 3'b000}", out)
        self.assertIn("-max_iterations 3", out)
        self.assertIn("-non_resettable_regs", out)
        self.assertIn("get_signal_info net_name", out)
        self.assertIn("reset -analyze", out)
        self.assertIn("visualize -violation -property {my_prop}", out)
        self.assertIn("get_design_info", out)
        self.assertIn("bbox_mod", out)
        self.assertEqual(result.unmapped_count, 0)

    def test_abstractions_snip_drive_tasks_session(self):
        text = r"""
set_abstractions -construct { mul=16 counter=4 }
report_abstractions
snip_drive net_name
fvtask -create task_name2
fvtask task_name2 -copy task_name1
fvtask task_name2 -copy task_name1 -assumes
fvtask task_name2
get_fvtask
save_session -session my_session
compute_reduced_constraints -prop p1
report_reduced_constraints
compute_formal_core -property [get_props -status proven] -block
report_formal_core
quit
"""
        result = convert_vc_tcl(text)
        out = result.jasper_tcl
        self.assertIn("get_design_info -list {multiplier counter}", out)
        self.assertIn("abstract -counter -list", out)
        self.assertIn("stopat net_name", out)
        self.assertIn("task -create task_name2", out)
        self.assertIn("-copy_assumes", out)
        self.assertIn("task -set task_name2", out)
        self.assertIn("\ntask\n", out)
        self.assertIn("save database", out)
        self.assertIn("get_needed_assumptions -property p1", out)
        self.assertIn("get_needed_assumptions -result", out)
        self.assertIn("check_cov -init -type proof", out)
        self.assertIn("[get_property_list -include {status proven}]", out)
        self.assertIn("report -json -config cov", out)
        self.assertIn("exit", out)
        self.assertEqual(result.unmapped_count, 0, msg=str(result.warnings))

    def test_witness_vacuity_related_covers(self):
        text = r"""
set_fml_var fml_witness_on true
set_fml_var fml_vacuity_on false
analyze -format sverilog -vcs "d.sv"
elaborate top -sva
"""
        out = convert_vc_tcl(text).jasper_tcl
        self.assertIn("-create_related_covers {witness}", out)
        self.assertNotIn("precondition witness", out)

    def test_check_fv_run_finish(self):
        text = "check_fv -run_finish { report_fv -list }\n"
        out = convert_vc_tcl(text).jasper_tcl
        self.assertIn("prove -all", out)
        self.assertIn("report -results", out)

    def test_set_change_at_posedge(self):
        text = "set_change_at -default -clock clk -posedge\n"
        out = convert_vc_tcl(text).jasper_tcl
        self.assertIn("clock -rate -default clk", out)
        self.assertFalse(
            any(
                line.startswith("# [vcf2jg skip]") and "set_change_at" in line
                for line in out.splitlines()
            )
        )


if __name__ == "__main__":
    unittest.main()
