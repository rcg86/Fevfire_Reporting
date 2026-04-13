tclmode
set x [lindex [split [get_version] " "] 0]
set y [clock format [clock seconds] -format {%b%d_%H:%M:%S}]
set_log_file ${top_name}_${x}_${y}.log

set constraints_loc /proj/work/ramapriya/scripts_rel/blockRunFire/latest/


file mkdir reports
#**************************************************************************
# Copyright (C) 2012-2021 Cadence Design Systems, Inc.
# All rights reserved.
#**************************************************************************

#**************************************************************************
# title: LEC: RTL versus RTL verification 
# tags:
#**************************************************************************

#**************************************************************************
# The following illustrates a sample dofile for running
# a RTL versus RTL Comparison
# Optional lines of code are commented out. Uncomment if needed.
# Note: For more information on the commands/options used in
# this sample dofile, use the MAN command within the relevant product,
# or refer to that product's reference manual.
#**************************************************************************

#**************************************************************************
# Sets up the log file and instructs the tool to display usage information
#**************************************************************************
#set_log_file rtl2rtl.log_$env(LEC_VERSION) -replace
usage -auto -elapse

#**************************************************************************
# Specifies the LEC project that will collect and consolidate
# information from multiple LEC runs
#**************************************************************************
set_project_name ${top_name}
set_hdl_option -UNSIZED_CONSTANT_TRUNCATE off




set_screen_display -noprogress
set_dofile_abort exit

### RTL names flow is enabled. ###

# Turns on the flowgraph datapath solver.
set wlec_analyze_dp_flowgraph true
# Indicates that resource sharing datapath optimization is present.
set share_dp_analysis         false

tcl_set_command_name_echo on

usage -auto -elapse

set_mapping_method -sensitive

set_verification_information rtl_fv_map_db

set_parallel_option -threads 1,4 -norelease_license
set_compare_options -threads 1,4

set_lowpower_option -native_1801
set_lowpower_option -golden_analysis_style  pre_synthesis
set_lowpower_option -revised_analysis_style post_synthesis

set env(RC_VERSION)     "25.11-s095_1"
set env(CDN_SYNTH_ROOT) "/proj/vendors/cadence/DDI25.11.001/GENUS251/tools.lnx86"
set CDN_SYNTH_ROOT      "/proj/vendors/cadence/DDI25.11.001/GENUS251/tools.lnx86"
set env(CW_DIR) "/proj/vendors/cadence/DDI25.11.001/GENUS251/tools.lnx86/lib/chipware"
set CW_DIR      "/proj/vendors/cadence/DDI25.11.001/GENUS251/tools.lnx86/lib/chipware"
    set env(CW_DIR_SIM) "/proj/vendors/cadence/DDI25.11.001/GENUS251/tools.lnx86/lib/chipware/sim"
    set CW_DIR_SIM      "/proj/vendors/cadence/DDI25.11.001/GENUS251/tools.lnx86/lib/chipware/sim"
set_multiplier_implementation boothrca -both

# default is to error out when module definitions are missing
set_undefined_cell black_box -noascend -both

set_hdl_option -UNSIZED_CONSTANT_TRUNCATE off

# ILM modules: 

add_search_path . /proj/vendors/cadence/DDI25.11.001/GENUS251/tools.lnx86/lib/tech -library -both

set_undriven_signal 0 -both
set_naming_style genus -both
set_naming_rule "" -parameter -both
set_naming_rule "%s\[%d\]" -instance_array -both
set_naming_rule "%s_reg" -register -both
set_naming_rule "%L.%s" "%L\[%d\].%s" "%s" -instance -both
set_naming_rule "%L.%s" "%L\[%d\].%s" "%s" -variable -both
set_naming_rule -mdportflatten -both
set_naming_rule -ungroup_separator {/} -both
set_naming_rule -ignore_case_gen_name

# Align LEC's treatment of mismatched port widths with constant
# connections with Genus's. Genus message CDFG-467 and LEC message
# HRC3.6 may indicate the presence of this issue.
set_hdl_options -const_port_extend
set_hdl_options -unsigned_conversion_overflow on
# Root attribute 'hdl_resolve_instance_with_libcell' was set to true in Genus.
set_hdl_options -use_library_first on
# Align LEC's treatment of libext in command files with Genus's.
# Only available with LEC 19.20-d138 or later.
set_hdl_option -v_to_vd on

# This command is only available with LEC 20.10-p100 or later.
    set_hdl_options -VERILOG_INCLUDE_DIR "cwd:incdir:src:yyd:sep"
add_search_path . -design -both

######
set _con_file ${constraints_loc}/constraints/rtl_rtl/common_library.tcl
if {[file exists $_con_file]} {
    puts "INFO: common library file found for ${top_name} - loading library: $_con_file"
    source -echo -verbose $_con_file
} else {
    puts "INFO: No common library file found for ${top_name} (looked for: $_con_file) - skipping."
}


### block specfic library

set _con_file ${constraints_loc}/constraints/rtl_rtl/${top_name}/${top_name}_library.tcl
if {[file exists $_con_file]} {
    puts "INFO: Block-specific constraints file found for ${top_name} - loading constraints: $_con_file"
    source -echo -verbose $_con_file
} else {
    puts "INFO: No block-specific constraints file found for ${top_name} (looked for: $_con_file) - skipping."
}



#**************************************************************************
# Reads in the design files
#**************************************************************************
read_design -enumconstraint -define SYNTHESIS  -merge bbox -golden -lastmod -noelab  -sv09 -f $goldenFlist
elaborate_design -root $top_name -golden -rootonly

read_design -enumconstraint -define SYNTHESIS  -merge bbox -revised -lastmod -noelab  -sv09 -f $revisedFlist
elaborate_design -root $top_name -revised -rootonly

##### modelling section
set_flatten_model -seq_constant

#### additional block level constraints

set _con_file ${constraints_loc}/constraints/rtl_rtl/common.con
if {[file exists $_con_file]} {
    puts "INFO: common constraints file found for ${top_name} - loading constraints: $_con_file"
    source -echo -verbose $_con_file
} else {
    puts "INFO: No common constraints file found for ${top_name} (looked for: $_con_file) - skipping."
}


set _con_file ${constraints_loc}/constraints/rtl_rtl/${top_name}/${top_name}.con
if {[file exists $_con_file]} {
    puts "INFO: Block-specific constraints file found for ${top_name} - loading constraints: $_con_file"
    source -echo -verbose $_con_file
} else {
    puts "INFO: No block-specific constraints file found for ${top_name} (looked for: $_con_file) - skipping."
}

#####

report_design_data > reports/report_design_data.rpt
report_black_box -detail > reports/report_black_box.rpt

#**************************************************************************
# Specifies the number of threads to enable multithreading
#**************************************************************************
set_parallel_option -threads 4

#************************************************************************
# Setting x assignment conversion to E gate
#**************************************************************************
set_x_conversion E -both

#**************************************************************************
# Generates the hierarchical dofile script for hierarchical comparison
#**************************************************************************
write_hier_compare_dofile hier_r2r.do -replace -usage \
  -prepend_string "report_design_data; usage ; report_unmapped_points -summary; report_unmapped_points -notmapped; analyze_datapath -module -verbose; analyze_datapath -verbose"

#************************************************************************
# Executes the hier.do script
#**************************************************************************
run_hier_compare hier_r2r.do -dynamic_hierarchy

if {{[get_compare_points -NONequivalent -count] > 0} || {[get_compare_points -abort -count] > 0 }} {
    checkpoint debugCheckPoint
}


#**************************************************************************
# Generates the reports for all compared hierarchical modules
#**************************************************************************
report_hier_compare_result -all -usage > reports/report_hier_compare_result.rpt
report_hier_compare_result -abort -usage > reports/report_hier_compare_result_abort.rpt
report_hier_compare_result -noneq -usage > reports/report_hier_compare_result_noneq.rpt
report_verification -hier -verbose < reports/report_verification.rpt

exit

