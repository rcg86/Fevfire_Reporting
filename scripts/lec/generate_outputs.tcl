
define_proc t_i_lec_dump_outputs -description "dump netlist, power intent, and do_lec script to output directories" {
} {

    set desName [get_db designs .name]
    write_netlist -top_module_first lecOutputs/${desName}.v
    python3 /proj/work/ramapriya/scripts_rel/blockRunFire/latest/correctFeedthroughNetlist.pyc --netlist lecOutputs/${desName}.v --top ${desName} --keep-module feedthrough_buffer_module_${desName} --output ${desName}_feedthrough.v
    write_power_intent -1801 lecOutputs/${desName}.upf

    write_do_lec -1801_golden golden/${desName}.upf -1801_revised revised/${desName}.upf -checkpoint ${desName}_checkpoint -golden_design golden/${desName}.v -revised_design reveised/${desName}.v -log_file pnr_pnr.log -no_lp -no_insert_iso_in_dof -verbose pnr_pnr.do
    exec mv fv lecOutputs/

    

}
