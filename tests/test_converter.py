"""Unit tests for vcf2jg converter."""

from pathlib import Path
import unittest

from vcf2jg.converter import convert_vc_tcl

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
        self.assertIn("report_proofs", out)
        self.assertTrue(
            any(
                "sim_run" in line and line.strip().startswith("#")
                for line in out.splitlines()
            )
        )
        self.assertGreaterEqual(result.mapped_count, 5)

    def test_active_low_reset_and_assume(self):
        text = (EXAMPLES / "vc_fpv_with_assume.tcl").read_text()
        result = convert_vc_tcl(text)
        out = result.jasper_tcl

        self.assertIn("elaborate -top ntt_butterfly", out)
        self.assertIn("reset -none", out)
        self.assertIn("assume -reset !rst_n", out)
        self.assertIn("assume {scan_en == 1'b0}", out)
        self.assertIn("assume {!test_mode}", out)
        self.assertIn("prove -all", out)

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


if __name__ == "__main__":
    unittest.main()
