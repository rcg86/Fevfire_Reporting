
define_proc t_i_lec_dump_outputs -description "dump netlist, power intent, and do_lec script to output directories" {
}

proc t_i_lec_dump_outputs {} {
    set desName [get_db designs .name]
    write_netlist -top_module_first lecOutputs/${desName}.v
    write_power_intent -1801 lecOutputs/${desName}.upf

    write_do_lec -1801_golden golden/${desName}.upf -1801_revised revised/${desName}.upf -checkpoint ${desName}_checkpoint -golden_design golden/${desName}.v -revised_design reveised/${desName}.v -log_file pnr_pnr.log -no_lp -no_insert_iso_in_dof -verbose pnr_pnr.do
}
