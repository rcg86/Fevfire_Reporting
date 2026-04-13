mkdir reports
analyze_library -lowpower
report_rule_check -verbose -lp -attribute > reports/lp_attribute.rpt
report_rule_check -verbose > reports/full_attribute_list.rpt


