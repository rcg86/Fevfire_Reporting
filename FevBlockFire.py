#!/usr/bin/env python3
"""
FevBlockFire.py

Launch block runs (rtl_rtl, rtl_syn, syn_pnr) using defaults from fevConfig.yaml.
Creates a run directory at <run_location>/<block_name>, moves previous runs to old_runs/,
copies fv/ for rtl_syn, edits rtl_to_fv_map.do inline, generates a .sh and optionally executes it.

Usage:
  python3 FevBlockFire.py --block_name <block> [--type <rtl_rtl|rtl_syn|syn_pnr>] [--location <path>] [--run_location <path>] [--config <path>] [--no_exec]
"""
import os
import sys
import argparse
import logging
import subprocess
import shutil
import glob
import yaml
import re
from datetime import datetime
from pathlib import Path


def setup_logging(block_name, run_type, run_location):
    logs_dir = os.path.join(run_location, "logs")
    os.makedirs(logs_dir, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    log_file = os.path.join(logs_dir, f"{block_name}_{run_type}_{ts}.log")
    
    # Clear any existing handlers and configure logging
    logger = logging.getLogger()
    logger.setLevel(logging.INFO)
    # Remove all existing handlers
    for handler in logger.handlers[:]:
        logger.removeHandler(handler)
    
    # Add file and console handlers
    file_handler = logging.FileHandler(log_file)
    console_handler = logging.StreamHandler(sys.stdout)
    
    formatter = logging.Formatter("%(asctime)s - %(levelname)s - %(message)s")
    file_handler.setFormatter(formatter)
    console_handler.setFormatter(formatter)
    
    logger.addHandler(file_handler)
    logger.addHandler(console_handler)
    
    logging.info(f"Starting run: block={block_name}, type={run_type}")
    logging.info(f"Log file: {log_file}")
    return log_file


def load_config(config_path):
    print(f"Loading config from: {config_path}")
    if not os.path.exists(config_path):
        print(f"ERROR: Config file not found: {config_path}")
        sys.exit(1)
    with open(config_path, 'r') as f:
        cfg = yaml.safe_load(f) or {}
    
    # Extract default type
    default_type = cfg.get('type', 'rtl_syn')
    
    skylp_config_path = cfg.get('skylp_config_yaml', '')
    skylp_config_data = {}
    if skylp_config_path:
        if os.path.isfile(skylp_config_path):
            with open(skylp_config_path, 'r') as _sf:
                skylp_config_data = yaml.safe_load(_sf) or {}
            print(f"Loaded skylp config from: {skylp_config_path}")
        else:
            print(f"WARNING: skylp_config_yaml not found: {skylp_config_path}")

    config = {
        'type': default_type,
        'rtl_syn': cfg.get('rtl_syn', {}),
        'rtl_rtl': cfg.get('rtl_rtl', {}),
        'syn_pnr': cfg.get('syn_pnr', {}),
        'pnr_pnr': cfg.get('pnr_pnr', {}),
        'skylp_config_data': skylp_config_data
    }
    print(f"Config loaded with default type: {default_type}")
    return config


def find_flist_for_block(block_name, skylp_config_data):
    """
    Search the skylp.config.yaml hierarchy to find the flist string for block_name.
    Returns (flist_str, wrapper_name) e.g. ('ucb.synth.flist VAR=upa CHIPLET=skylp WRAP=1', 'upaw')
    or None if not found.
    The wrapper_name is the key under cf_xxx.instances and is the name used by make for the
    output file (e.g. make produces upaw.synth.flist, not ucb.synth.flist).
    Matches either the wrapper name directly or any sub-instance of a wrapper.
    """
    top = skylp_config_data.get('skylp', {})
    for cf_name, cf_data in top.items():
        if not isinstance(cf_data, dict):
            continue
        instances = cf_data.get('instances', {}) or {}
        for wrapper_name, wrapper_data in instances.items():
            flist_str = None
            if isinstance(wrapper_data, dict):
                flist_str = wrapper_data.get('flist')
            if not flist_str:
                continue
            # Direct match on the wrapper itself
            if wrapper_name == block_name:
                return (flist_str, wrapper_name)
            # Match on any sub-instance of this wrapper
            if isinstance(wrapper_data, dict):
                sub_instances = wrapper_data.get('instances', {}) or {}
                if block_name in sub_instances:
                    return (flist_str, wrapper_name)
    return None


def get_autoblackbox_modules(block_name, skylp_config_data):
    """
    Given block_name and the parsed skylp.config.yaml, return a sorted list of
    sibling/cousin module names that should be black-boxed when running block_name.

    Algorithm:
    - Find the wrapper (direct child of a cf_xxx entry) whose sub-instance tree
      contains block_name.
    - Trace the ancestor path: wrapper_instances -> ... -> block_name.
    - Collect every module NOT on the ancestor path and NOT a descendant of
      block_name (they are siblings/cousins that should be blackboxed).
    - Ancestors are excluded because they are parent modules that must stay
      transparent (e.g. running tmu_mma: tmu is ancestor -> NOT blackboxed).
    - Descendants are excluded because they are sub-modules already contained
      inside block_name (e.g. running tmu: tmu_mma is descendant -> NOT blackboxed).
    - If block_name IS the wrapper itself, returns [] (nothing to blackbox).
    """

    def find_path_and_data(instances_dict, target):
        """DFS: return (path, target_node_data) or None if not found."""
        if not instances_dict:
            return None
        if target in instances_dict:
            return ([target], instances_dict[target])
        for mod_name, mod_data in instances_dict.items():
            if not isinstance(mod_data, dict):
                continue
            result = find_path_and_data(mod_data.get('instances') or {}, target)
            if result is not None:
                path, data = result
                return ([mod_name] + path, data)
        return None

    def collect_all_names(instances_dict):
        """Recursively collect every module name in a subtree."""
        result = []
        for mod_name, mod_data in (instances_dict or {}).items():
            result.append(mod_name)
            if isinstance(mod_data, dict):
                result.extend(collect_all_names(mod_data.get('instances') or {}))
        return result

    def collect_non_ancestors(instances_dict, ancestors_set):
        """Recursively collect all module names that are NOT in ancestors_set."""
        result = []
        for mod_name, mod_data in (instances_dict or {}).items():
            sub = mod_data.get('instances') or {} if isinstance(mod_data, dict) else {}
            if mod_name in ancestors_set:
                # Ancestor – keep transparent, but recurse into its children
                result.extend(collect_non_ancestors(sub, ancestors_set))
            else:
                result.append(mod_name)
                result.extend(collect_non_ancestors(sub, ancestors_set))
        return result

    top = skylp_config_data.get('skylp', {})
    for cf_name, cf_data in top.items():
        if not isinstance(cf_data, dict):
            continue
        for wrapper_name, wrapper_data in (cf_data.get('instances', {}) or {}).items():
            if wrapper_name == block_name:
                # Running the wrapper itself – blackbox everything inside it
                if isinstance(wrapper_data, dict):
                    return sorted(collect_all_names(wrapper_data.get('instances') or {}))
                return []
            if not isinstance(wrapper_data, dict):
                continue
            sub_instances = wrapper_data.get('instances') or {}
            if not sub_instances:
                continue
            result = find_path_and_data(sub_instances, block_name)
            if result is None:
                continue
            path, target_data = result
            # ancestors = path nodes excluding block_name (the leaf)
            ancestors_set = set(path[:-1])
            # descendants of block_name are part of its own design – do not blackbox
            descendants_set = set()
            if isinstance(target_data, dict):
                descendants_set = set(collect_all_names(target_data.get('instances') or {}))
            modules = collect_non_ancestors(sub_instances, ancestors_set)
            exclude = descendants_set | {block_name}
            return sorted(m for m in modules if m not in exclude)
    return []


def find_block(base_path, block_name, location_override=None):
    """
    If location_override provided, validate and return it unchanged.
    Else search for base_path/*/<block_name>/ver_* and return the most recently modified match
    that contains an 'fv' directory.
    """
    if location_override:
        logging.info(f"Using user-provided location override: {location_override}")
        if not os.path.isdir(location_override):
            logging.error(f"Provided location does not exist or is not a directory: {location_override}")
            return None
        return location_override

    logging.info(f"Searching for latest block under: {base_path}")
    pattern = os.path.join(base_path, "*", block_name , "ver_*")
    candidates = glob.glob(pattern)
    logging.info(f"Found {len(candidates)} candidate(s) for pattern: {pattern}")
    
    # Sort candidates by modification time (newest first)
    candidates_with_mtime = []
    for c in candidates:
        if os.path.isdir(c):
            m = os.path.getmtime(c)
            candidates_with_mtime.append((c, m))
            logging.info(f"  candidate: {c} (mtime={m})")
    
    # Sort by mtime descending (newest first)
    candidates_with_mtime.sort(key=lambda x: x[1], reverse=True)
    
    # Find the first directory with 'fv' folder
    selected = None
    for c, m in candidates_with_mtime:
        fv_path = os.path.join(c, "fv")
        if os.path.isdir(fv_path):
            selected = c
            logging.info(f"Selected directory: {selected} (has fv folder)")
            break
        else:
            logging.debug(f"Skipping {c} - no fv folder found")
    
    if selected:
        # Check if we had to skip newer directories
        if candidates_with_mtime and candidates_with_mtime[0][0] != selected:
            latest_dir = candidates_with_mtime[0][0]
            logging.warning(f"Latest directory {latest_dir} does not have 'fv' folder")
            logging.warning(f"Using older directory with 'fv' folder: {selected}")
        logging.info(f"Selected latest directory with fv: {selected}")
    else:
        logging.warning("No matching block directory with 'fv' folder found")
    
    return selected


def rotate_and_create_run_dir(run_location, block_name):
    run_dir = os.path.join(run_location, block_name)
    if os.path.exists(run_dir):
        old_runs = os.path.join(run_location, "old_runs")
        os.makedirs(old_runs, exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        dest = os.path.join(old_runs, f"{block_name}_{ts}")
        logging.info(f"Moving existing run dir {run_dir} -> {dest}")
        shutil.move(run_dir, dest)
    os.makedirs(run_dir, exist_ok=True)
    logging.info(f"Created run dir: {run_dir}")
    return run_dir


def generate_run_script(run_dir, block_name, run_type, commands):
    script_path = os.path.join(run_dir, f"{block_name}_{run_type}_run.sh")
    header = f"#!/bin/bash\n# Generated for {block_name} {run_type} at {datetime.now().isoformat()}\n\n\n"
    content = header + commands + "\n"
    with open(script_path, 'w') as f:
        f.write(content)
    os.chmod(script_path, 0o755)
    logging.info(f"Generated run script: {script_path}")
    return script_path


def execute_script(script_path):
    logging.info(f"Executing script: {script_path}")
    res = subprocess.run([script_path], cwd=os.path.dirname(script_path), capture_output=True, text=True)
    if res.stdout:
        logging.info(res.stdout)
    if res.stderr:
        logging.warning(res.stderr)
    if res.returncode != 0:
        logging.error(f"Script failed with exit code {res.returncode}")
        sys.exit(res.returncode)
    logging.info("Script finished successfully")


def execute_script_live(script_path):
    """Run script_path without capturing stdout/stderr, so it inherits this
    process's terminal. Needed for srun --pty --x11 (e.g. Innovus), which
    requires an attached tty and won't produce visible output otherwise."""
    logging.info(f"Executing script (live): {script_path}")
    res = subprocess.run([script_path], cwd=os.path.dirname(script_path))
    if res.returncode != 0:
        logging.error(f"Script failed with exit code {res.returncode}")
        sys.exit(res.returncode)
    logging.info("Script finished successfully")


def dump_info_yaml(run_dir, info):
    """Write info.yaml to run_dir with run metadata for later inspection."""
    info_path = os.path.join(run_dir, 'info.yaml')
    try:
        with open(info_path, 'w') as f:
            yaml.dump(info, f, default_flow_style=False, sort_keys=False)
        logging.info(f"Wrote info.yaml: {info_path}")
    except Exception as e:
        logging.warning(f"Failed to write info.yaml: {e}")


def generate_flist_from_tag(tag_name, block_name, golden_releases_location, golden_release_block, work_dir=None, flist_type='synth', make_target_str=None, expected_filename=None):
    """
    Generate flist file from a tag in golden releases.
    
    Args:
        tag_name: Tag identifier (e.g., SKYLP_G0550)
        block_name: Block name for make command
        golden_releases_location: Base location (e.g., /proj/rel/GOLDEN_RELEASES)
        golden_release_block: Block directory (e.g., SKYLP)
        work_dir: Working directory to create flist (default: current directory)
        flist_type: Flist type suffix, e.g. 'synth' or 'fast_synth' (default: 'synth')
        make_target_str: Full make target string from skylp config
                         (e.g. 'ucb.synth.flist VAR=upa CHIPLET=skylp WRAP=1').
                         If provided, overrides the default '{block_name}.{flist_type}.flist' target.
        expected_filename: Actual filename that make will produce on disk
                           (e.g. 'upaw.synth.flist' — named after the wrapper, not the IP).
                           If omitted, falls back to the first word of make_target_str or
                           '{block_name}.{flist_type}.flist'.
    
    Returns:
        Path to generated flist file
    """
    if work_dir is None:
        work_dir = os.getcwd()
    
    # Construct the tag path
    tag_path = os.path.join(golden_releases_location, golden_release_block, tag_name)
    logging.info(f"Attempting to generate flist from tag: {tag_name}")
    logging.info(f"Tag path: {tag_path}")
    
    # Check if tag directory exists
    if not os.path.isdir(tag_path):
        logging.error(f"Tag directory does not exist: {tag_path}")
        return None
    
    # Create generate_flist directory
    gen_flist_dir = os.path.join(work_dir, "generate_flist")
    if os.path.exists(gen_flist_dir):
        logging.info(f"Removing existing generate_flist directory: {gen_flist_dir}")
        shutil.rmtree(gen_flist_dir)
    
    os.makedirs(gen_flist_dir, exist_ok=True)
    logging.info(f"Created generate_flist directory: {gen_flist_dir}")
    
    # Create symlink to makefile
    makefile_src = os.path.join(tag_path, "design", "fl", "scripts", "makefiles", "flows.mk")
    makefile_link = os.path.join(gen_flist_dir, "makefile")
    
    if not os.path.isfile(makefile_src):
        logging.error(f"Makefile not found at: {makefile_src}")
        return None
    
    try:
        if os.path.lexists(makefile_link):  # lexists returns True for broken symlinks
            os.remove(makefile_link)
        os.symlink(makefile_src, makefile_link)
        logging.info(f"Created symlink: {makefile_link} -> {makefile_src}")
    except Exception as e:
        logging.error(f"Failed to create symlink: {e}")
        return None
    
    # Run make command to generate flist
    repo_root = tag_path
    if make_target_str:
        full_make_args = make_target_str             # e.g. 'ucb.synth.flist VAR=upa CHIPLET=skylp WRAP=1'
    else:
        full_make_args = f"{block_name}.{flist_type}.flist"
    # The file make actually writes to disk may differ from the make target name.
    # expected_filename (e.g. 'upaw.synth.flist') takes priority; fall back to
    # the first word of full_make_args.
    if expected_filename:
        flist_target = expected_filename
    else:
        flist_target = full_make_args.split()[0]

    logging.info(f"Running make command to generate {flist_target}")
    logging.info(f"  REPO_ROOT={repo_root}")
    logging.info(f"  Working directory: {gen_flist_dir}")
    
    try:
        # Construct the make command as a string for shell execution
        make_command = f"make REPO_ROOT={repo_root} {full_make_args}"
        logging.info(f"Executing command: {make_command}")
        
        result = subprocess.run(
            make_command,
            cwd=gen_flist_dir,
            capture_output=True,
            text=True,
            timeout=300,
            shell=True
        )
        
        if result.stdout:
            logging.info(f"Make stdout:\n{result.stdout}")
        if result.stderr:
            logging.info(f"Make stderr:\n{result.stderr}")
        
        if result.returncode != 0:
            logging.error(f"Make command failed with exit code {result.returncode}")
            return None
        
        # Check if flist was generated inside generate_flist directory
        generated_flist = os.path.join(gen_flist_dir, flist_target)
        if not os.path.isfile(generated_flist):
            logging.error(f"Flist file not generated: {generated_flist}")
            return None
        
        # Move the flist to the block directory with tag-based naming
        final_flist_name = f"{tag_name}_{flist_target}"
        final_flist_path = os.path.join(work_dir, final_flist_name)
        shutil.move(generated_flist, final_flist_path)
        logging.info(f"Moved flist from {generated_flist} to {final_flist_path}")
        logging.info(f"Successfully generated flist: {final_flist_path}")
        
        return final_flist_path
        
    except subprocess.TimeoutExpired:
        logging.error(f"Make command timed out after 300 seconds")
        return None
    except Exception as e:
        logging.error(f"Failed to execute make command: {e}")
        return None


def resolve_flist_file(flist_arg, block_name, type_config, work_dir=None, skylp_config_data=None):
    """
    Resolve flist file - either use provided file or generate from tag.
    
    Args:
        flist_arg: Command-line argument (file path or tag name)
        block_name: Block name for make command
        type_config: Configuration dictionary for run type
        work_dir: Working directory (default: current directory)
        skylp_config_data: Parsed skylp.config.yaml data for flist target lookup
    
    Returns:
        Path to flist file, or None if not found/generated
    """
    if work_dir is None:
        work_dir = os.getcwd()
    
    if not flist_arg:
        return None
    
    # Check if it's a direct file path
    if os.path.isfile(flist_arg):
        logging.info(f"Using provided flist file: {flist_arg}")
        return flist_arg
    
    # Check if it could be a tag name, optionally with a flist type suffix (e.g. SKYLP_G0507.fast_synth)
    logging.info(f"Flist argument '{flist_arg}' is not a file, checking if it's a tag")

    # Parse optional flist type: '<TAG>.<flist_type>' -> tag_name='<TAG>', flist_type='<flist_type>'
    # If no dot is present the default flist type 'synth' is used.
    if '.' in flist_arg:
        tag_name, flist_type = flist_arg.split('.', 1)
        logging.info(f"Parsed tag='{tag_name}', flist_type='{flist_type}'")
    else:
        tag_name = flist_arg
        flist_type = 'synth'
        logging.info(f"No flist type suffix found; using default flist_type='synth'")

    golden_releases_location = type_config.get('golden_releases_location')
    golden_release_block = type_config.get('golden_release_block', 'SKYLP')
    
    if not golden_releases_location:
        logging.error(f"Golden releases location not configured in type config")
        return None

    # Look up block in skylp config to get the proper make target with extra args
    make_target_str = None
    expected_filename = None
    if skylp_config_data:
        result = find_flist_for_block(block_name, skylp_config_data)
        if result:
            flist_str, wrapper_name = result
            # flist_str e.g. 'ucb.synth.flist VAR=upa CHIPLET=skylp WRAP=1'
            # Substitute the type segment (e.g. synth->sim) to match flist_type
            parts = flist_str.split()
            flist_file = parts[0]              # e.g. 'ucb.synth.flist'
            extra_args = ' '.join(parts[1:])   # e.g. 'VAR=upa CHIPLET=skylp WRAP=1'
            flist_base = flist_file.split('.')[0]  # e.g. 'ucb'
            new_flist_file = f"{flist_base}.{flist_type}.flist"
            make_target_str = f"{new_flist_file} {extra_args}".strip()
            # Make produces the output file named after the wrapper, not the IP prefix
            expected_filename = f"{wrapper_name}.{flist_type}.flist"
            logging.info(f"skylp config flist for '{block_name}': {flist_str}")
            logging.info(f"Using make args: {make_target_str}")
            logging.info(f"Expected output file: {expected_filename}")
        else:
            logging.warning(f"Block '{block_name}' not found in skylp config; using default make target")

    # Try to generate flist from tag
    generated_flist = generate_flist_from_tag(
        tag_name,
        block_name,
        golden_releases_location,
        golden_release_block,
        work_dir,
        flist_type=flist_type,
        make_target_str=make_target_str,
        expected_filename=expected_filename
    )
    
    return generated_flist


def run_rtl_rtl(resolved, block_path, no_exec=False, run_dir=None):
    logging.info("Starting rtl_rtl run")
    
    # Use provided run_dir or create new one
    if run_dir is None:
        run_dir = rotate_and_create_run_dir(resolved['run_location'], resolved['block_name'])
    else:
        logging.info(f"Using existing run directory: {run_dir}")

    # Build info dict for this run
    info = {
        'block_name': resolved['block_name'],
        'run_type': 'rtl_rtl',
        'timestamp': datetime.now().isoformat(),
        'fv_source_location': 'NA',
        'golden_flist': resolved.get('golden_flist') or 'NA',
        'revised_flist': resolved.get('revised_flist') or 'NA',
        'status': 'ok',
        'error_detail': None,
    }

    # Determine source DO file path
    rtl_rtl_do_name = resolved.get('rtl_rtl_do', 'rtl_rtl.do')
    script_dir = Path(__file__).parent
    source_do = os.path.join(script_dir, rtl_rtl_do_name)
    
    if not os.path.isfile(source_do):
        logging.error(f"rtl_rtl DO file not found: {source_do}")
        sys.exit(1)

    # Validate that the resolved flist files actually exist
    golden_flist = resolved.get('golden_flist')
    revised_flist = resolved.get('revised_flist')

    if not golden_flist or not os.path.isfile(golden_flist):
        logging.error(f"Golden flist not found: {golden_flist}")
        info['status'] = 'error'
        info['error_detail'] = f"Golden flist not found: {golden_flist}"
        dump_info_yaml(run_dir, info)
        sys.exit(1)

    if not revised_flist or not os.path.isfile(revised_flist):
        logging.error(f"Revised flist not found: {revised_flist}")
        info['status'] = 'error'
        info['error_detail'] = f"Revised flist not found: {revised_flist}"
        dump_info_yaml(run_dir, info)
        sys.exit(1)
    
    # Copy DO file to run directory
    target_do = os.path.join(run_dir, rtl_rtl_do_name)
    logging.info(f"Copying DO file: {source_do} -> {target_do}")
    shutil.copy2(source_do, target_do)
    
    # Edit the DO file to add flist variables
    try:
        with open(target_do, 'r') as f:
            lines = f.readlines()
        
        # Insert variable definitions at line 2 (after the first line)
        golden_flist = resolved['golden_flist']
        revised_flist = resolved['revised_flist']
        block_name = resolved['block_name']
        flist_vars = f"set goldenFlist {golden_flist} \n set revisedFlist {revised_flist}\nset top_name {block_name}\n"
        
        if len(lines) >= 1:
            lines.insert(1, flist_vars)
        else:
            lines.append(flist_vars)

        # Inject auto black-box notranslate lines before the first read_design
        if resolved.get('autoBlackBox') and resolved.get('skylp_config_data'):
            bb_modules = get_autoblackbox_modules(resolved['block_name'], resolved['skylp_config_data'])
            if bb_modules:
                bb_block = '\n#### Auto black-box sibling modules (autoBlackBox)\n'
                bb_block += ''.join(f'add_notranslate_modules -golden {m}\n' for m in bb_modules)
                bb_block += '\n'
                for i, l in enumerate(lines):
                    if re.search(r'\bread_design\b', l):
                        lines.insert(i, bb_block)
                        logging.info(f"Injected auto black-box before read_design: {bb_modules}")
                        break
        
        with open(target_do, 'w') as f:
            f.writelines(lines)
        
        logging.info(f"Added flist variables to {target_do}")
        logging.info(f"  goldenFlist: {golden_flist}")
        logging.info(f"  revisedFlist: {revised_flist}")
    except Exception as e:
        logging.error(f"Failed to edit {target_do}: {e}")
        info['status'] = 'error'
        info['error_detail'] = f"Failed to edit DO file {target_do}: {e}"
        dump_info_yaml(run_dir, info)
        sys.exit(1)
    
    # Generate run script
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    logfile_name = f"{resolved['block_name']}_lec_{timestamp}.log"
    jobname = f"{resolved['block_name']}_auto_lec_{timestamp}"
    
    sh_cmds = f'''cd "{run_dir}"
echo "Running rtl_rtl for {resolved["block_name"]}"
echo "DO file: {rtl_rtl_do_name}"
echo "Log file: {logfile_name}"

blaunch --cpus 8 --mem 64 -L confrml:1 --jobname {jobname} --mail -L confrml launch -c cdns/confrml@25.20-w228 -- lec -nogui -lp -xl -dofile {rtl_rtl_do_name} -logfile {logfile_name} >> job_id.list
'''
    script = generate_run_script(run_dir, resolved['block_name'], 'rtl_rtl', sh_cmds)
    dump_info_yaml(run_dir, info)
    if not no_exec:
        execute_script(script)
    else:
        logging.info("--no_exec set: not executing script")


def generate_checkpoint_tcl(lec_dir):
    """Generate lec/checkpoint.tcl, used as the -post_compare hook for write_do_lec:
    dumps a Conformal checkpoint session if any compare points are NOT equivalent,
    aborted, or unknown after the compare."""
    tcl_path = os.path.join(lec_dir, 'checkpoint.tcl')
    tcl_content = '''if {[get_compare_points -NONequivalent -count] > 0 || [get_compare_points -abort -count] > 0 || [get_compare_points -unknown -count] > 0} {
    checkpoint debugCheckPoint -replace
}
'''
    with open(tcl_path, 'w') as f:
        f.write(tcl_content)
    logging.info(f"Generated Conformal checkpoint.tcl: {tcl_path}")
    return tcl_path


def generate_dump_lec_data_tcl(lec_dir, golden_db, revised_db):
    """Generate the Innovus tcl file that dumps golden/revised netlists+UPF
    from the two PNR databases and writes the LEC do file. Only used for pnr_pnr."""
    generate_checkpoint_tcl(lec_dir)
    tcl_path = os.path.join(lec_dir, 'dump_lec_data.tcl')
    tcl_content = f'''file mkdir lec/golden
file mkdir lec/revised

read_db {golden_db} -no_tim
write_netlist lec/golden/golden.v
write_power_intent -1801 lec/golden/golden.upf

set_db read_db_stop_at_design_in_memory false
read_db {revised_db} -no_tim
write_netlist lec/revised/revised.v
write_power_intent -1801 lec/revised/revised.upf

write_do_lec lec_files -1801_golden lec/golden/golden.upf -1801_revised lec/revised/revised.upf -verbose -golden_design lec/golden/golden.v -flat -revised_design lec/revised/revised.v -verbose -write_session lec_completed.session -post_compare lec/checkpoint.tcl

exit
'''
    with open(tcl_path, 'w') as f:
        f.write(tcl_content)
    logging.info(f"Generated Innovus dump_lec_data.tcl: {tcl_path}")
    return tcl_path


def generate_lec_data_sh(lec_dir, run_dir, block_name):
    """Generate lec/generate_lec_data.sh which launches Innovus (via srun) on
    dump_lec_data.tcl to dump golden/revised netlists+UPF and the LEC do file.
    Runs from run_dir since dump_lec_data.tcl uses lec/... relative paths."""
    sh_path = os.path.join(lec_dir, 'generate_lec_data.sh')
    content = f'''#!/bin/bash
# Generated for {block_name} pnr_pnr (Innovus dump_lec_data) at {datetime.now().isoformat()}

cd "{run_dir}"

srun --time=18:00:00 \\
     --job-name=lec_gen_data \\
     --ntasks=1 \\
     --partition=od-64-gb-4-cores \\
 -L innovus \\
     --pty \\
     --x11 \\
     bash -c "launch -c cdns/innovus -- innovus -stylus -files lec/dump_lec_data.tcl"
'''
    with open(sh_path, 'w') as f:
        f.write(content)
    os.chmod(sh_path, 0o755)
    logging.info(f"Generated Innovus launch script: {sh_path}")
    return sh_path


def generate_lec_run_sh(run_dir, block_name):
    """Generate run_lec_pnr_pnr.sh which launches Conformal (lec) using the
    do file at ./fv/invs/<block_name>/lec_files (relative to run_dir)."""
    sh_path = os.path.join(run_dir, 'run_lec_pnr_pnr.sh')
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    logfile_name = f"{block_name}_lec_{timestamp}.log"
    jobname = f"{block_name}_auto_lec_{timestamp}"
    dofile = f"./fv/invs/{block_name}/lec_files"
    content = f'''#!/bin/bash
# Generated for {block_name} pnr_pnr (Conformal lec) at {datetime.now().isoformat()}

cd "{run_dir}"
echo "Running pnr_pnr lec for {block_name}"
echo "Dofile: {dofile}"
echo "Log file: {logfile_name}"

blaunch --cpus 8 --mem 64 -L confrml:1 --jobname {jobname} --mail -L confrml launch -c cdns/confrml@25.20-w228 -- lec -nogui -lp -xl -dofile {dofile} -logfile {logfile_name} >> job_id.list

sleep 10
squeue -u $(whoami)
'''
    with open(sh_path, 'w') as f:
        f.write(content)
    os.chmod(sh_path, 0o755)
    logging.info(f"Generated Conformal launch script: {sh_path}")
    return sh_path


def run_pnr_pnr(resolved, block_path, no_exec=False, run_dir=None):
    logging.info("Starting pnr_pnr run")

    if run_dir is None:
        run_dir = rotate_and_create_run_dir(resolved['run_location'], resolved['block_name'])
    else:
        logging.info(f"Using existing run directory: {run_dir}")

    info = {
        'block_name': resolved['block_name'],
        'run_type': 'pnr_pnr',
        'timestamp': datetime.now().isoformat(),
        'fv_source_location': 'NA',
        'golden_db': resolved.get('golden_db') or 'NA',
        'revised_db': resolved.get('revised_db') or 'NA',
        'status': 'ok',
        'error_detail': None,
    }

    golden_db = resolved.get('golden_db')
    revised_db = resolved.get('revised_db')

    if not golden_db or not os.path.exists(golden_db):
        logging.error(f"Golden db not found: {golden_db}")
        info['status'] = 'error'
        info['error_detail'] = f"Golden db not found: {golden_db}"
        dump_info_yaml(run_dir, info)
        sys.exit(1)

    if not revised_db or not os.path.exists(revised_db):
        logging.error(f"Revised db not found: {revised_db}")
        info['status'] = 'error'
        info['error_detail'] = f"Revised db not found: {revised_db}"
        dump_info_yaml(run_dir, info)
        sys.exit(1)

    lec_dir = os.path.join(run_dir, 'lec')
    os.makedirs(lec_dir, exist_ok=True)
    dump_lec_data_tcl = generate_dump_lec_data_tcl(lec_dir, golden_db, revised_db)
    gen_lec_data_script = generate_lec_data_sh(lec_dir, run_dir, resolved['block_name'])
    lec_run_script = generate_lec_run_sh(run_dir, resolved['block_name'])

    dump_info_yaml(run_dir, info)

    if not no_exec:
        execute_script_live(gen_lec_data_script)
        execute_script(lec_run_script)
    else:
        logging.info("--no_exec set: not executing generate_lec_data.sh or run_lec_pnr_pnr.sh")


def run_rtl_syn(resolved, block_path, no_exec=False, pre_created_run_dir=False):
    logging.info("Starting rtl_syn run")
    
    # Only rotate/create run directory if it wasn't pre-created during flist generation
    if pre_created_run_dir:
        run_dir = os.path.join(resolved['run_location'], resolved['block_name'])
        logging.info(f"Using pre-created run directory: {run_dir}")
    else:
        run_dir = rotate_and_create_run_dir(resolved['run_location'], resolved['block_name'])

    # Build info dict for this run
    source_fv = os.path.join(block_path, "fv")
    info = {
        'block_name': resolved['block_name'],
        'run_type': 'rtl_syn',
        'timestamp': datetime.now().isoformat(),
        'fv_source_location': source_fv,
        'golden_flist': resolved.get('golden_flist') or 'NA',
        'revised_flist': 'NA',
        'status': 'ok',
        'error_detail': None,
    }

    # 1) copy fv folder from block_path -> run_dir/fv
    if not os.path.isdir(source_fv):
        logging.error(f"Source fv folder not found: {source_fv}")
        info['status'] = 'error'
        info['error_detail'] = f"fv folder does not exist: {source_fv}"
        dump_info_yaml(run_dir, info)
        sys.exit(1)
    target_fv = os.path.join(run_dir, "fv")
    if os.path.exists(target_fv):
        logging.info(f"Removing existing fv at: {target_fv}")
        shutil.rmtree(target_fv)
    logging.info(f"Copying fv folder: {source_fv} -> {target_fv}")
    shutil.copytree(source_fv, target_fv)

    # 2) Edit rtl_to_fv_map.do inline inside run_rtl_syn
    do_file = os.path.join(target_fv, resolved['block_name'], "rtl_to_fv_map.do")
    if not os.path.exists(do_file):
        logging.warning(f"rtl_to_fv_map.do not found at expected location: {do_file} (continuing)")
    else:
        try:
            with open(do_file, 'r') as f:
                content = f.read()
            
            stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            header = f"tclmode \n\n// Modified by FevBlockFire on {stamp}\nset RUN_DIR \"{run_dir}\"\nset top_name {resolved['block_name']}\nfile mkdir reports\n// Original source: {block_path}\n"
            
            # Add golden_flist variable if provided
            if resolved.get('golden_flist'):
                header += f"set goldenFlist {resolved['golden_flist']}\n"
                logging.info(f"Added golden flist variable: {resolved['golden_flist']}")
            
            header += "\n"
            
            # Process report commands to redirect to reports directory
            lines = content.split('\n')
            modified_lines = []
            report_count = 0
            read_upf = resolved.get('read_upf', True)
            golden_upf_location = resolved.get('golden_upf_location', '')
            block_name = resolved['block_name']

            # Compute auto black-box modules once before entering the line loop
            bb_modules = []
            first_read_design_injected = False
            if resolved.get('autoBlackBox') and resolved.get('skylp_config_data'):
                bb_modules = get_autoblackbox_modules(block_name, resolved['skylp_config_data'])
                if bb_modules:
                    logging.info(f"autoBlackBox enabled: will inject notranslate for: {bb_modules}")

            for line in lines:
                # Inject auto black-box notranslate lines before the first read_design
                if bb_modules and not first_read_design_injected and re.search(r'\bread_design\b', line):
                    first_read_design_injected = True
                    modified_lines.append('')
                    modified_lines.append('#### Auto black-box sibling modules (autoBlackBox)')
                    for m in bb_modules:
                        modified_lines.append(f'add_notranslate_modules -golden {m}')
                    modified_lines.append('')
                    logging.info(f"Injected auto black-box notranslate lines for: {bb_modules}")

                # Handle read_design command for golden with flist
                if 'read_design' in line and '-golden' in line and resolved.get('golden_flist'):
                    # Check if line already has -f option
                    if '-f' in line:
                        # Replace existing flist with new one
                        match = re.match(r'^(.*read_design.*)(-f\s+\S+)(.*)$', line)
                        if match:
                            before_f, old_f, after_f = match.groups()
                            new_line = f"{before_f}-f $goldenFlist{after_f}"
                            modified_lines.append(new_line)
                            logging.info(f"Modified read_design golden flist: {line.strip()} -> {new_line.strip()}")
                            continue
                    else:
                        # Add -f option before any trailing options or at end
                        modified_line = line.rstrip()
                        if modified_line.endswith('\\'):
                            # Line continuation - insert before \\
                            modified_line = modified_line[:-1].rstrip() + ' -f $goldenFlist \\'
                        else:
                            # No continuation - add at end
                            modified_line = modified_line + ' -f $goldenFlist'
                        modified_lines.append(modified_line)
                        logging.info(f"Added -f $goldenFlist to read_design golden: {modified_line.strip()}")
                        continue
                
                # Handle read_power_intent command for golden
                if 'read_power_intent' in line and '-golden' in line:
                    if not read_upf:
                        # Comment out the read_power_intent line if not already commented
                        if not line.lstrip().startswith('#'):
                            commented_line = '# ' + line
                            modified_lines.append(commented_line)
                            logging.info(f"Commented out read_power_intent: {line.strip()}")
                        else:
                            modified_lines.append(line)
                        continue
                    if resolved.get('golden_flist') and golden_upf_location:
                        # Update or add the golden UPF path only when golden_flist is provided
                        upf_path = f"{golden_upf_location}{block_name}/{block_name}.upf"
                        # Check if line already has a file path at the end
                        match = re.match(r'^(.*read_power_intent.*?)\s+(\S+\.upf)(.*)$', line)
                        if match:
                            before_upf, old_upf, after_upf = match.groups()
                            new_line = f"{before_upf} {upf_path}{after_upf}"
                        else:
                            # No existing path, add it
                            new_line = line.rstrip()
                            if new_line.endswith('\\'):
                                new_line = new_line[:-1].rstrip() + f' {upf_path} \\'
                            else:
                                new_line = new_line + f' {upf_path}'
                        modified_lines.append(new_line)
                        logging.info(f"Updated read_power_intent golden UPF: {new_line.strip()}")
                        continue
                    # If no golden_flist was provided, retain the original line
                    modified_lines.append(line)
                    continue
                
                # Check if read_upf is false and line contains read_power_intent (for non-golden)
                if not read_upf and 'read_power_intent' in line and '-golden' not in line:
                    # Comment out the read_power_intent line if not already commented
                    if not line.lstrip().startswith('#'):
                        commented_line = '# ' + line
                        modified_lines.append(commented_line)
                        logging.info(f"Commented out read_power_intent: {line.strip()}")
                    else:
                        modified_lines.append(line)
                    continue
                
                # Check if line contains a report_ command
                if re.search(r'\breport_', line):
                    # Case 1: report_ command with existing redirection
                    if '>' in line:
                        # Extract the report command and existing redirection
                        match = re.match(r'^(\s*)(report_\S+)(.*)>\s*(\S+)(.*)$', line)
                        if match:
                            indent, cmd, args_before, old_file, args_after = match.groups()
                            # Extract just the filename (remove any path)
                            filename = os.path.basename(old_file)
                            new_line = f"{indent}{cmd}{args_before}> reports/{filename}{args_after}"
                            modified_lines.append(new_line)
                            report_count += 1
                            logging.info(f"Modified report redirection: {line.strip()} -> {new_line.strip()}")
                        else:
                            modified_lines.append(line)
                    else:
                        # Case 2: report_ command without redirection
                        match = re.match(r'^(\s*)(report_\S+)(.*)$', line)
                        if match:
                            indent, cmd, args = match.groups()
                            # Generate report filename from command name
                            report_name = cmd.strip()
                            new_line = f"{indent}{cmd}{args} > reports/{report_name}.rpt"
                            modified_lines.append(new_line)
                            report_count += 1
                            logging.info(f"Added report redirection: {line.strip()} -> {new_line.strip()}")
                        else:
                            modified_lines.append(line)
                else:
                    modified_lines.append(line)
                
                # Check if this line contains run_hier_compare command
                if re.search(r'\brun_hier_compare\b', line):
                    # Insert checkpoint commands after run_hier_compare
                    modified_lines.append("\nif {[get_compare_points -NONequivalent -count] > 0 || [get_compare_points -abort -count] > 0 || [get_compare_points -unknown -count] > 0} {")
                    modified_lines.append("    checkpoint debugCheckPoint -replace")
                    modified_lines.append("}")
                    logging.info(f"Inserted checkpoint commands after run_hier_compare")

                # Inject block-specific constraints after read_design -revised
                if 'read_design' in line and '-revised' in line:
                    con_block = (
                        "\n#### additional block level constraints\n"
                        "set _con_file [file dirname [file normalize [info script]]]/constraints/rtl_rtl/${top_name}/${top_name}.con\n"
                        "if {[file exists $_con_file]} {\n"
                        "    puts \"INFO: Block-specific constraints file found for ${top_name} loading constraints: $_con_file\"\n"
                        "    source -echo -verbose $_con_file\n"
                        "} else {\n"
                        "    puts \"INFO: No block-specific constraints file found for ${top_name} (looked for: $_con_file) skipping.\"\n"
                        "}"
                    )
                    modified_lines.append(con_block)
                    logging.info("Injected block-specific constraints block after read_design -revised")
            
            new_content = header + '\n'.join(modified_lines)
            
            with open(do_file, 'w') as f:
                f.write(new_content)
            logging.info(f"Updated rtl_to_fv_map.do at: {do_file}")
            logging.info(f"Modified {report_count} report command(s) to redirect to reports/ directory")
        except Exception as e:
            logging.error(f"Failed to edit {do_file}: {e}")

    # 3) Generate run script
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    do_file = f"./fv/{resolved['block_name']}/rtl_to_fv_map.do"
    logfile_name = f"{resolved['block_name']}_lec_{timestamp}.log"
    jobname = f"{resolved['block_name']}_auto_lec_{timestamp}"
    
    sh_cmds = f'''cd "{run_dir}"
echo "Running rtl_syn for {resolved["block_name"]}"
echo "FV folder: {target_fv}"
echo "DO file: {do_file}"
echo "Log file: {logfile_name}"

blaunch --cpus 8 --mem 64 -L confrml:1 --jobname {jobname} --mail -L confrml launch -c cdns/confrml@25.20-w228 -- lec -nogui -lp -xl -dofile {do_file} -logfile {logfile_name} >> job_id.list
'''
    script = generate_run_script(run_dir, resolved['block_name'], 'rtl_syn', sh_cmds)
    dump_info_yaml(run_dir, info)
    if not no_exec:
        execute_script(script)
    else:
        logging.info("--no_exec set: not executing script")


def run_syn_pnr(resolved, block_path, no_exec=False):
    logging.info("Starting syn_pnr run (placeholder)")
    run_dir = rotate_and_create_run_dir(resolved['run_location'], resolved['block_name'])

    info = {
        'block_name': resolved['block_name'],
        'run_type': 'syn_pnr',
        'timestamp': datetime.now().isoformat(),
        'fv_source_location': 'NA',
        'golden_flist': resolved.get('golden_flist') or 'NA',
        'revised_flist': 'NA',
        'status': 'ok',
        'error_detail': None,
    }

    sh_cmds = f'echo "SYN_PNR for {resolved["block_name"]}"\n# TODO: place syn_pnr commands here\n'
    script = generate_run_script(run_dir, resolved['block_name'], 'syn_pnr', sh_cmds)
    dump_info_yaml(run_dir, info)
    if not no_exec:
        execute_script(script)
    else:
        logging.info("--no_exec set: not executing script")


def main():
    invocation_cwd = os.getcwd()
    parser = argparse.ArgumentParser(description="FevBlockFire - launch block FEV runs")
    parser.add_argument('--block_name', required=True)
    parser.add_argument('--type', choices=['rtl_rtl', 'rtl_syn', 'syn_pnr', 'pnr_pnr'])
    parser.add_argument('--location', help="synthesis input path location path from where to pick the inputs for fv (overrides config)")
    parser.add_argument('--run_location', help="path where lec run gets executed (overrides config)")
    parser.add_argument('--config', help="Path to YAML config (defaults to fevConfig.yaml in fevFireRuns)")
    parser.add_argument('--no_exec', action='store_true', help="Create .sh but do NOT execute it")
    parser.add_argument('--no_upf', action='store_true', help='Comment out read_power_intent commands in the do file (applies to all run types)')
    parser.add_argument('--golden_flist', type=str, help='Golden flist file path or tag (e.g., SKYLP_G0550). Applies to rtl_rtl, rtl_syn, syn_pnr')
    parser.add_argument('--revised_flist', type=str, help='Revised flist file path or tag (rtl_rtl only)')
    parser.add_argument('--golden_db', type=str, help='Golden PNR design database absolute path (required for pnr_pnr)')
    parser.add_argument('--revised_db', type=str, help='Revised PNR design database absolute path (required for pnr_pnr)')
    args = parser.parse_args()

    # config default path
    if args.config:
        config_path = args.config
    else:
        script_dir = Path(__file__).parent
        config_path = os.path.join(script_dir, 'fevConfig.yaml')

    config = load_config(config_path)

    # Determine the run type
    run_type = args.type if args.type else config.get('type')
    
    if run_type not in ['rtl_rtl', 'rtl_syn', 'syn_pnr', 'pnr_pnr']:
        print(f"ERROR: Invalid or missing run type: {run_type}")
        sys.exit(1)
    
    # Get type-specific config
    type_config = config.get(run_type, {})
    
    resolved = {
        'block_name': args.block_name,
        'type': run_type,
        'location': args.location if args.location else type_config.get('location'),
        'run_location': args.run_location if args.run_location else type_config.get('run_location'),
        'read_upf': False if args.no_upf else type_config.get('read_upf', True),
        'rtl_rtl_do': type_config.get('rtl_rtl_do'),
        'golden_upf_location': type_config.get('golden_upf_location', ''),
        'autoBlackBox': type_config.get('autoBlackBox', False),
        'skylp_config_data': config.get('skylp_config_data', {}),
    }
    
    # Change to run_location before setting up logging
    os.chdir(resolved['run_location'])

    # Helper — called right after block dir is created/rotated in each branch below
    def _write_refire(block_dir):
        path = os.path.join(block_dir, 'refire.sh')
        with open(path, 'w') as _rf:
            _rf.write('#!/usr/bin/env bash\n')
            _rf.write('# Auto-generated by FevBlockFire.py \u2014 re-run to repeat the run\n')
            _rf.write(' '.join([sys.executable, os.path.abspath(__file__)] + sys.argv[1:]) + '\n')
        os.chmod(path, 0o755)
        print(f"Command saved to: {path}")

    # setup logging after we have run_location and changed directory
    log_file = setup_logging(args.block_name, run_type, resolved['run_location'])
    logging.info(f"Resolved config: {resolved}")

    if resolved['type'] not in ['rtl_rtl', 'rtl_syn', 'syn_pnr', 'pnr_pnr']:
        logging.error(f"Invalid or missing run type: {resolved['type']}")
        sys.exit(1)

    # For rtl_rtl, handle flist files instead of block path
    if resolved['type'] == 'rtl_rtl':
        # Create the block directory first to ensure generate_flist goes inside it
        block_dir = os.path.join(resolved['run_location'], args.block_name)
        if os.path.exists(block_dir):
            old_runs = os.path.join(resolved['run_location'], "old_runs")
            os.makedirs(old_runs, exist_ok=True)
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            dest = os.path.join(old_runs, f"{args.block_name}_{ts}")
            logging.info(f"Moving existing run dir {block_dir} -> {dest}")
            shutil.move(block_dir, dest)
        os.makedirs(block_dir, exist_ok=True)
        logging.info(f"Created block directory: {block_dir}")
        _write_refire(block_dir)
        
        # Prompt for flist files if not provided via CLI
        golden_flist = args.golden_flist
        revised_flist = args.revised_flist
        
        if not golden_flist:
            golden_flist = input("Enter path to golden flist file or tag name (e.g., SKYLP_G0550): ").strip()
        if not revised_flist:
            revised_flist = input("Enter path to revised flist file or tag name (e.g., SKYLP_G0551): ").strip()
        
        # Resolve flist files - check if they're files or tags
        # Pass block_dir so generate_flist directory is created inside it
        resolved_golden = resolve_flist_file(golden_flist, args.block_name, config.get('rtl_rtl', {}), block_dir, skylp_config_data=config.get('skylp_config_data', {}))
        resolved_revised = resolve_flist_file(revised_flist, args.block_name, config.get('rtl_rtl', {}), block_dir, skylp_config_data=config.get('skylp_config_data', {}))
        
        if not resolved_golden:
            logging.error(f"Golden flist could not be resolved: {golden_flist}")
            sys.exit(1)
        if not resolved_revised:
            logging.error(f"Revised flist could not be resolved: {revised_flist}")
            sys.exit(1)
        
        resolved['golden_flist'] = resolved_golden
        resolved['revised_flist'] = resolved_revised
        logging.info(f"Golden flist: {resolved_golden}")
        logging.info(f"Revised flist: {resolved_revised}")
        
        run_rtl_rtl(resolved, block_path=None, no_exec=args.no_exec, run_dir=block_dir)

    elif resolved['type'] == 'pnr_pnr':
        # pnr_pnr: driven by golden/revised PNR databases (no flists needed)
        if not args.golden_db or not args.revised_db:
            logging.error("--golden_db and --revised_db are required for pnr_pnr type")
            print("ERROR: --golden_db and --revised_db are required for pnr_pnr type")
            sys.exit(1)

        resolved_dbs = {}
        for label, key, db_path in (('--golden_db', 'golden_db', args.golden_db), ('--revised_db', 'revised_db', args.revised_db)):
            abs_db_path = db_path if os.path.isabs(db_path) else os.path.normpath(os.path.join(invocation_cwd, db_path))
            if not os.path.exists(abs_db_path):
                logging.error(f"{label} not found: {abs_db_path}")
                print(f"ERROR: {label} not found: {abs_db_path}")
                sys.exit(1)
            resolved_dbs[key] = abs_db_path
            logging.info(f"Resolved {label}: {db_path} -> {abs_db_path}")

        block_dir = os.path.join(resolved['run_location'], args.block_name)
        if os.path.exists(block_dir):
            old_runs = os.path.join(resolved['run_location'], "old_runs")
            os.makedirs(old_runs, exist_ok=True)
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            dest = os.path.join(old_runs, f"{args.block_name}_{ts}")
            logging.info(f"Moving existing run dir {block_dir} -> {dest}")
            shutil.move(block_dir, dest)
        os.makedirs(block_dir, exist_ok=True)
        logging.info(f"Created block directory: {block_dir}")
        _write_refire(block_dir)

        resolved['golden_db'] = resolved_dbs['golden_db']
        resolved['revised_db'] = resolved_dbs['revised_db']
        logging.info(f"Golden db: {resolved['golden_db']}")
        logging.info(f"Revised db: {resolved['revised_db']}")

        run_pnr_pnr(resolved, block_path=None, no_exec=args.no_exec, run_dir=block_dir)

    else:
        # For rtl_syn and syn_pnr, handle --golden_flist (file or tag) if provided
        golden_tag = args.golden_flist
        
        if golden_tag:
            # Create run directory structure for flist generation (inside block's run dir)
            run_dir_base = os.path.join(resolved['run_location'], args.block_name)
            
            # Rotate existing run directory if it exists
            if os.path.exists(run_dir_base):
                old_runs = os.path.join(resolved['run_location'], "old_runs")
                os.makedirs(old_runs, exist_ok=True)
                ts = datetime.now().strftime("%Y%m%d_%H%M%S")
                dest = os.path.join(old_runs, f"{args.block_name}_{ts}")
                logging.info(f"Moving existing run dir {run_dir_base} -> {dest}")
                shutil.move(run_dir_base, dest)
            
            # Create the run directory and flist_gen subdirectory
            flist_gen_dir = os.path.join(run_dir_base, f"flist_gen_{args.block_name}_{datetime.now().strftime('%Y%m%d_%H%M%S')}")
            os.makedirs(flist_gen_dir, exist_ok=True)
            logging.info(f"Created flist generation directory: {flist_gen_dir}")
            _write_refire(run_dir_base)
            
            # Resolve golden flist from tag or file
            resolved_golden = resolve_flist_file(golden_tag, args.block_name, type_config, flist_gen_dir, skylp_config_data=config.get('skylp_config_data', {}))
            if not resolved_golden:
                logging.error(f"Golden flist could not be resolved: {golden_tag}")
                sys.exit(1)
            
            resolved['golden_flist'] = resolved_golden
            logging.info(f"Golden flist: {resolved_golden}")
            
            # When golden tag is used, also use golden_upf_location from config
            logging.info(f"Using golden_upf_location from config: {resolved['golden_upf_location']}")

        else:
            # No golden flist — block dir has not been created yet; make it now so
            # refire.sh is written before any run logic that might fail.
            _no_golden_block_dir = os.path.join(resolved['run_location'], args.block_name)
            os.makedirs(_no_golden_block_dir, exist_ok=True)
            _write_refire(_no_golden_block_dir)
        
        # find block path (location override is used as exact path if provided)
        block_path = find_block(resolved['location'], resolved['block_name'], location_override=args.location)
        if not block_path:
            logging.error("Failed to determine block path.")
            sys.exit(1)
        logging.info(f"Using block path: {block_path}")
        
        if resolved['type'] == 'rtl_syn':
            # Pass pre_created_run_dir=True if golden_flist was provided (run dir was already created)
            run_rtl_syn(resolved, block_path, no_exec=args.no_exec, pre_created_run_dir=golden_tag is not None)
        elif resolved['type'] == 'syn_pnr':
            run_syn_pnr(resolved, block_path, no_exec=args.no_exec)

    logging.info(f"Run finished. Log: {log_file}")
    print(f"Log saved at: {log_file}")


if __name__ == '__main__':
    main()
