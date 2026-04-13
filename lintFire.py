#!/usr/bin/env python3
"""
lintFire.py

Cron-driven lint runner that fires a rotating daily subset of blocks.

Reads lintConfig.yaml (adjacent to this script) for:
  skylp_config_yaml  - path to skylp.config.yaml (provides flist info per block)
  blocks             - comma-separated block names
  blockFire          - number of groups to divide blocks into; one group runs per day
  runDirectory       - root directory for all run artifacts

Rotation logic (day-of-week based):
  chunk_index = weekday() % blockFire   (Monday=0)
  chunk_size  = ceil(total_blocks / blockFire)
  today_blocks = blocks[chunk_index*chunk_size : (chunk_index+1)*chunk_size]

Directory layout under runDirectory:
  gitRepo/           - cloned repos (cloned once, pulled on subsequent runs)
  <block>/           - per-block working directory
    makefile -> ../gitRepo/design/fl/scripts/makefiles/flows.mk

Per-block steps:
  1. Create <block>/ directory and symlink makefile
  2. Locate flist attribute in skylp_config_yaml (at block or parent wrapper level)
  3. make <flist_target> REPO_ROOT=../gitRepo [extra_flist_vars...]
  4. make <lint_target> REPO_ROOT=../gitRepo

Usage (crontab example):
  0 6 * * 1-5 /usr/bin/python3 /path/to/lintFire.py >> /path/to/logs/lintFire.log 2>&1

Options:
  --no_git          Skip git clone/pull and use the repos already present in
                    <runDirectory>/gitRepo/.
  --block BLOCK     Run only the named block instead of today's rotation slice.
"""

import argparse
import logging
import math
import os
import subprocess
import sys
from datetime import date, datetime

import yaml


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

def setup_logging(run_directory: str) -> str:
    """Configure logging to both console and a timestamped log file."""
    logs_dir = os.path.join(run_directory, "logs")
    os.makedirs(logs_dir, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    log_file = os.path.join(logs_dir, f"lintFire_{ts}.log")

    logger = logging.getLogger()
    logger.setLevel(logging.INFO)
    for h in logger.handlers[:]:
        logger.removeHandler(h)

    formatter = logging.Formatter("%(asctime)s - %(levelname)s - %(message)s")
    fh = logging.FileHandler(log_file)
    fh.setFormatter(formatter)
    ch = logging.StreamHandler(sys.stdout)
    ch.setFormatter(formatter)
    logger.addHandler(fh)
    logger.addHandler(ch)

    return log_file


# ---------------------------------------------------------------------------
# Config loading
# ---------------------------------------------------------------------------

def load_lint_config(config_path: str) -> dict:
    if not os.path.exists(config_path):
        print(f"ERROR: lintConfig.yaml not found: {config_path}", file=sys.stderr)
        sys.exit(1)
    with open(config_path, "r") as fh:
        cfg = yaml.safe_load(fh) or {}
    return cfg


def load_skylp_config(skylp_config_path: str) -> dict:
    if not os.path.isfile(skylp_config_path):
        logging.error(f"skylp_config_yaml not found: {skylp_config_path}")
        sys.exit(1)
    with open(skylp_config_path, "r") as fh:
        data = yaml.safe_load(fh) or {}
    logging.info(f"Loaded skylp config: {skylp_config_path}")
    return data


# ---------------------------------------------------------------------------
# Block rotation
# ---------------------------------------------------------------------------

def get_today_blocks(blocks: list, block_fire: int) -> list:
    """
    Return the slice of blocks that should run today.

    The week day (Mon=0 … Sun=6) is mapped to a chunk index via modulo.
    Chunk size is ceil(total / blockFire) so the first chunks are slightly
    larger when the division is not exact.

    Example: 20 blocks, blockFire=3 → chunks of 7, 7, 6.
      Mon/Thu/Sun → chunk 0 (blocks 0-6)
      Tue/Fri     → chunk 1 (blocks 7-13)
      Wed/Sat     → chunk 2 (blocks 14-19)
    """
    total = len(blocks)
    chunk_size = math.ceil(total / block_fire)
    chunk_idx = date.today().weekday() % block_fire
    start = chunk_idx * chunk_size
    end = min(start + chunk_size, total)
    return blocks[start:end]


# ---------------------------------------------------------------------------
# Skylp config flist lookup
# ---------------------------------------------------------------------------

def find_flist_for_block(block_name: str, skylp_config_data: dict):
    """
    Search the skylp.config.yaml hierarchy for *block_name* and return
    (flist_str, wrapper_name) or (None, None).

    Expected structure:
      skylp:
        <cf_xxx>:
          instances:
            <wrapper_name>:
              flist: "<target> [VAR=val ...]"
              instances:
                <block_name>:
                  ...

    The flist attribute lives at the wrapper level.  If block_name equals
    the wrapper directly, that flist is used.  If block_name appears as a
    sub-instance of the wrapper, the wrapper's flist is used (inherited).

    wrapper_name is returned because make uses it as the output file stem
    (e.g. ``make ldu.synth.flist`` not ``ldm.synth.flist``).
    """
    top = skylp_config_data.get("skylp", {})
    for cf_name, cf_data in top.items():
        if not isinstance(cf_data, dict):
            continue
        instances = cf_data.get("instances", {}) or {}
        for wrapper_name, wrapper_data in instances.items():
            flist_str = None
            if isinstance(wrapper_data, dict):
                flist_str = wrapper_data.get("flist")
            if not flist_str:
                continue

            # Direct match: the block IS the wrapper
            if wrapper_name == block_name:
                return (flist_str, wrapper_name)

            # Indirect match: block is a sub-instance of this wrapper
            if isinstance(wrapper_data, dict):
                sub_instances = wrapper_data.get("instances", {}) or {}
                if block_name in sub_instances:
                    return (flist_str, wrapper_name)

    return (None, None)


def parse_flist_string(flist_str: str):
    """
    Split 'ldu.synth.flist CHIPLET=skylp WRAP=1' into
    ('ldu.synth.flist', ['CHIPLET=skylp', 'WRAP=1']).
    """
    parts = flist_str.split()
    if not parts:
        return (None, [])
    return (parts[0], parts[1:])


def derive_lint_target(flist_target: str) -> str:
    """Replace trailing .flist with .lint  (e.g. ldu.synth.flist → ldu.synth.lint)."""
    return flist_target.replace(".flist", ".lint")


# ---------------------------------------------------------------------------
# Git repo management
# ---------------------------------------------------------------------------

REPOS = [
    "git@github.com:tsavoritesi/design.git",
    "git@github.com:tsavoritesi/chiplet.git",
    "git@github.com:tsavoritesi/cdv.git",
    "git@github.com:tsavoritesi/cpd.git",
    "git@github.com:tsavoritesi/ral.git",
    "git@github.com:tsavoritesi/syscmodels.git",
]


def run_command(cmd: list, cwd: str = None, extra_env: dict = None) -> subprocess.CompletedProcess:
    """Run *cmd* and stream output to the logger.  Raises on non-zero exit."""
    logging.info("CMD: %s  (cwd=%s)", " ".join(cmd), cwd or os.getcwd())
    env = os.environ.copy()
    if extra_env:
        env.update(extra_env)
    result = subprocess.run(
        cmd,
        cwd=cwd,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    for line in result.stdout.splitlines():
        logging.info("  %s", line)
    if result.returncode != 0:
        raise subprocess.CalledProcessError(result.returncode, cmd)
    return result


def setup_git_repos(git_repo_dir: str, git_env: dict) -> None:
    """
    Ensure all required repos exist under *git_repo_dir*.
    Clones missing repos; pulls the default branch for repos that already exist.
    """
    os.makedirs(git_repo_dir, exist_ok=True)
    for repo_url in REPOS:
        repo_name = repo_url.split("/")[-1].replace(".git", "")
        repo_path = os.path.join(git_repo_dir, repo_name)
        if os.path.isdir(os.path.join(repo_path, ".git")):
            logging.info("Pulling latest: %s", repo_name)
            run_command(["git", "pull"], cwd=repo_path, extra_env=git_env)
        else:
            logging.info("Cloning: %s", repo_url)
            run_command(["git", "clone", repo_url, repo_path], extra_env=git_env)


# ---------------------------------------------------------------------------
# Per-block lint execution
# ---------------------------------------------------------------------------

def setup_block_directory(run_directory: str, block_name: str) -> str:
    """
    Create <run_directory>/<block_name>/ and symlink *makefile* to the flows.mk
    provided by the design repo.
    Returns the absolute path to the block directory.
    """
    block_dir = os.path.join(run_directory, block_name)
    os.makedirs(block_dir, exist_ok=True)

    makefile_link = os.path.join(block_dir, "makefile")
    flows_mk_target = "../gitRepo/design/fl/scripts/makefiles/flows.mk"

    if os.path.islink(makefile_link):
        os.remove(makefile_link)
    os.symlink(flows_mk_target, makefile_link)
    logging.info("Symlink: %s -> %s", makefile_link, flows_mk_target)

    return block_dir


def run_lint_for_block(
    run_directory: str, block_name: str, skylp_config_data: dict, no_fire: bool = False
) -> bool:
    """
    Set up the block working directory, generate the flist, and run lint.
    If no_fire is True, write a .pending placeholder instead of running make lint.
    Returns True on success, False on any failure.
    """
    logging.info("=" * 60)
    logging.info("Processing block: %s", block_name)
    logging.info("=" * 60)

    # -- Flist lookup -------------------------------------------------------
    flist_str, wrapper_name = find_flist_for_block(block_name, skylp_config_data)
    if flist_str is None:
        logging.error("No flist found for block '%s' in skylp config", block_name)
        return False
    logging.info("flist found via wrapper '%s': %s", wrapper_name, flist_str)

    flist_target, extra_args = parse_flist_string(flist_str)
    if flist_target is None:
        logging.error("Could not parse flist string: %s", flist_str)
        return False

    lint_target = derive_lint_target(flist_target)

    # -- Directory / symlink setup ------------------------------------------
    block_dir = setup_block_directory(run_directory, block_name)

    # -- Generate flist ------------------------------------------------------
    # make <flist_target> REPO_ROOT=../gitRepo [extra_flist_vars...]
    flist_cmd = ["make", flist_target, "REPO_ROOT=../gitRepo"] + extra_args
    try:
        run_command(flist_cmd, cwd=block_dir)
    except subprocess.CalledProcessError:
        logging.error("Flist generation failed for block '%s'", block_name)
        return False

    # -- Run lint ------------------------------------------------------------
    if no_fire:
        placeholder = os.path.join(block_dir, f"{lint_target}.pending")
        with open(placeholder, "w") as f:
            f.write(f"lint target : {lint_target}\n")
            f.write(f"flist target: {flist_target}\n")
            f.write(f"extra args  : {' '.join(extra_args)}\n")
            f.write(f"command     : make {lint_target} REPO_ROOT=../gitRepo\n")
            f.write(f"created     : {datetime.now().isoformat()}\n")
        logging.info("--nofire: placeholder created: %s", placeholder)
        return True

    # make <lint_target> REPO_ROOT=../gitRepo
    lint_cmd = ["make", lint_target, "REPO_ROOT=../gitRepo"]
    try:
        run_command(lint_cmd, cwd=block_dir)
    except subprocess.CalledProcessError:
        logging.error("Lint FAILED for block '%s'", block_name)
        return False

    logging.info("Lint PASSED for block '%s'", block_name)
    return True


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="Lint Fire — rotating block lint runner")
    parser.add_argument(
        "--no_git",
        action="store_true",
        help="Skip git clone/pull; use repos already present in <runDirectory>/gitRepo/",
    )
    parser.add_argument(
        "--block",
        metavar="BLOCK",
        default=None,
        help="Run only this block instead of today's rotation slice",
    )
    parser.add_argument(
        "--nofire",
        action="store_true",
        help="Skip the lint make command; write a .pending placeholder file instead",
    )
    args = parser.parse_args()

    script_dir = os.path.dirname(os.path.abspath(__file__))
    config_path = os.path.join(script_dir, "lintConfig.yaml")

    # Load lint config first (need runDirectory before we can set up logging)
    cfg = load_lint_config(config_path)

    skylp_config_yaml = cfg.get("skylp_config_yaml", "")
    blocks_raw        = cfg.get("blocks", "")
    block_fire        = int(cfg.get("blockFire", 1))
    run_directory     = cfg.get("runDirectory", "")
    if not run_directory:
        print("ERROR: runDirectory not set in lintConfig.yaml", file=sys.stderr)
        sys.exit(1)

    log_file = setup_logging(run_directory)
    logging.info("lintFire.py started")
    logging.info("Config: %s", config_path)
    logging.info("Log file: %s", log_file)

    # Validate required config fields
    for field, value in [("skylp_config_yaml", skylp_config_yaml), ("blocks", blocks_raw)]:
        if not value:
            logging.error("'%s' not set in lintConfig.yaml", field)
            sys.exit(1)

    # Parse block list
    blocks = [b.strip() for b in blocks_raw.split(",") if b.strip()]
    if not blocks:
        logging.error("No blocks found in lintConfig.yaml 'blocks' field")
        sys.exit(1)
    logging.info("Total blocks configured: %d", len(blocks))

    # --block override
    if args.block:
        if args.block not in blocks:
            logging.warning(
                "Block '%s' is not in lintConfig.yaml blocks list; running anyway",
                args.block,
            )
        today_blocks = [args.block]
        logging.info("--block override: running only '%s'", args.block)
    else:
        if block_fire < 1:
            logging.error("blockFire must be >= 1, got %d", block_fire)
            sys.exit(1)
        today_blocks = get_today_blocks(blocks, block_fire)
        logging.info(
            "Today (weekday=%d, chunk=%d/%d): %d block(s) → %s",
            date.today().weekday(),
            date.today().weekday() % block_fire,
            block_fire,
            len(today_blocks),
            today_blocks,
        )

    # Load skylp config
    skylp_config_data = load_skylp_config(skylp_config_yaml)

    # Set up git repositories
    git_repo_dir = os.path.join(run_directory, "gitRepo")
    if args.no_git:
        logging.info("--no_git: skipping git clone/pull, using existing repos in %s", git_repo_dir)
        if not os.path.isdir(git_repo_dir):
            logging.error("--no_git specified but gitRepo directory not found: %s", git_repo_dir)
            sys.exit(1)
    else:
        git_env = {}
        logging.info("Setting up git repos under: %s", git_repo_dir)
        try:
            setup_git_repos(git_repo_dir, git_env)
        except subprocess.CalledProcessError as exc:
            logging.error("Git setup failed: %s", exc)
            sys.exit(1)

    if args.nofire:
        logging.info("--nofire: lint make commands will be skipped; .pending files will be written")

    # Run lint for each of today's blocks
    results: dict[str, bool] = {}
    for block in today_blocks:
        results[block] = run_lint_for_block(run_directory, block, skylp_config_data, no_fire=args.nofire)

    # Summary
    passed = [b for b, ok in results.items() if ok]
    failed = [b for b, ok in results.items() if not ok]

    logging.info("")
    logging.info("=" * 60)
    logging.info("LINT RUN SUMMARY")
    logging.info("=" * 60)
    logging.info("PASSED (%d): %s", len(passed), passed)
    logging.info("FAILED (%d): %s", len(failed), failed)
    logging.info("=" * 60)

    if failed:
        sys.exit(1)


if __name__ == "__main__":
    main()
