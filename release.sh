#!/bin/bash
# =============================================================================
# CI/CD Pipeline Script for blockRunFire Python Release Management
# Usage: source release.sh  OR  . release.sh
# =============================================================================

set -e

# --- Configuration ---
RELEASE_BASE="/proj/work/ramapriya/scripts_rel/blockRunFire"
SOURCE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LATEST_LINK="${RELEASE_BASE}/latest"
REMOTE_NAME="${REMOTE_NAME:-origin}"
PYTHON3="/proj/local/bin/python3"

# --- Color Codes ---
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
NC='\033[0m' # No Color

# =============================================================================
# STEP 1: Determine Previous Tag and Ask for New Tag
# =============================================================================
echo -e "${CYAN}============================================${NC}"
echo -e "${CYAN}   blockRunFire Release CI/CD Pipeline     ${NC}"
echo -e "${CYAN}============================================${NC}"

# Get the latest git tag
PREV_TAG=$(git -C "${SOURCE_DIR}" describe --tags --abbrev=0 2>/dev/null || echo "none")

echo -e "\n${YELLOW}Previous tag was: ${PREV_TAG}${NC}"
echo -ne "${GREEN}New tag is? :: ${NC}"
read NEW_TAG

# Validate tag input
if [[ -z "$NEW_TAG" ]]; then
    echo -e "${RED}ERROR: Tag cannot be empty. Aborting.${NC}"
    return 1 2>/dev/null || exit 1
fi

# Check if tag already exists
if git -C "${SOURCE_DIR}" rev-parse "$NEW_TAG" >/dev/null 2>&1; then
    echo -e "${RED}ERROR: Tag '${NEW_TAG}' already exists. Aborting.${NC}"
    return 1 2>/dev/null || exit 1
fi

# Ask for change notes to include in commit/tag messages
echo -e "\n${GREEN}What changes are included in this release?${NC}"
echo -e "${YELLOW}(Describe what was fixed, added, or changed — press Enter twice when done)${NC}"
CHANGE_NOTES=""
while IFS= read -r line; do
    [[ -z "$line" ]] && break
    CHANGE_NOTES+="${line}\n"
done
CHANGE_NOTES="$(echo -e "${CHANGE_NOTES}" | sed 's/[[:space:]]*$//')"

if [[ -z "$CHANGE_NOTES" ]]; then
    echo -e "${RED}ERROR: Change notes cannot be empty. Aborting.${NC}"
    return 1 2>/dev/null || exit 1
fi

echo -e "\n${CYAN}[1/6] Tagging release as: ${NEW_TAG}${NC}"

# =============================================================================
# STEP 2: Git Commit (if needed), Tag, and Push to Remote
# =============================================================================
echo -e "${CYAN}[2/6] Creating git tag and pushing to remote...${NC}"

# Detect current branch
CURRENT_BRANCH="$(git -C "${SOURCE_DIR}" rev-parse --abbrev-ref HEAD)"

# Make sure we have a remote
if ! git -C "${SOURCE_DIR}" remote get-url "${REMOTE_NAME}" >/dev/null 2>&1; then
    echo -e "${RED}ERROR: Remote '${REMOTE_NAME}' not found. Aborting.${NC}"
    return 1 2>/dev/null || exit 1
fi

# Stage + commit only if there are changes
MADE_COMMIT=0
if ! git -C "${SOURCE_DIR}" diff --quiet || ! git -C "${SOURCE_DIR}" diff --cached --quiet; then
    git -C "${SOURCE_DIR}" add -A
    git -C "${SOURCE_DIR}" commit -m "Release: ${NEW_TAG}

${CHANGE_NOTES}"
    MADE_COMMIT=1
    echo -e "${GREEN}  ✔ Changes committed.${NC}"
else
    echo -e "${YELLOW}  ℹ No uncommitted changes found; no commit created.${NC}"
fi

# Create annotated tag locally
git -C "${SOURCE_DIR}" tag -a "${NEW_TAG}" -m "Release version ${NEW_TAG}

${CHANGE_NOTES}"
echo -e "${GREEN}  ✔ Git tag '${NEW_TAG}' created locally.${NC}"

# Push commit (only if we created one)
if [[ "${MADE_COMMIT}" -eq 1 ]]; then
    echo -e "${CYAN}  Pushing commit to ${REMOTE_NAME}/${CURRENT_BRANCH}...${NC}"
    if ! git -C "${SOURCE_DIR}" push "${REMOTE_NAME}" "${CURRENT_BRANCH}"; then
        echo -e "${RED}ERROR: Failed to push commit to remote. Aborting.${NC}"
        git -C "${SOURCE_DIR}" tag -d "${NEW_TAG}"
        return 1 2>/dev/null || exit 1
    fi
    echo -e "${GREEN}  ✔ Commit pushed to ${REMOTE_NAME}/${CURRENT_BRANCH}.${NC}"
fi

# Push tag to remote
echo -e "${CYAN}  Pushing tag '${NEW_TAG}' to ${REMOTE_NAME}...${NC}"
if ! git -C "${SOURCE_DIR}" push "${REMOTE_NAME}" "${NEW_TAG}"; then
    echo -e "${RED}ERROR: Failed to push tag to remote. Aborting.${NC}"
    git -C "${SOURCE_DIR}" tag -d "${NEW_TAG}"
    return 1 2>/dev/null || exit 1
fi
echo -e "${GREEN}  ✔ Tag '${NEW_TAG}' pushed to ${REMOTE_NAME}.${NC}"

# =============================================================================
# STEP 3: Create Release Directory
# =============================================================================
RELEASE_DIR="${RELEASE_BASE}/${NEW_TAG}"

echo -e "${CYAN}[3/6] Creating release directory: ${RELEASE_DIR}${NC}"

if [[ -d "$RELEASE_DIR" ]]; then
    echo -e "${RED}ERROR: Release directory '${RELEASE_DIR}' already exists. Aborting.${NC}"
    return 1 2>/dev/null || exit 1
fi

mkdir -p "$RELEASE_DIR"
echo -e "${GREEN}  ✔ Release directory created.${NC}"

# =============================================================================
# STEP 4: Copy *.py, *.yaml, and *.do files into the Release Directory
# =============================================================================
echo -e "${CYAN}[4/6] Copying *.py, *.yaml, and *.do files to release directory...${NC}"

PY_FILES=("${SOURCE_DIR}"/*.py)
YAML_FILES=("${SOURCE_DIR}"/*.yaml)
DO_FILES=("${SOURCE_DIR}"/*.do)

if [[ ! -e "${PY_FILES[0]}" ]]; then
    echo -e "${RED}ERROR: No *.py files found in ${SOURCE_DIR}. Aborting.${NC}"
    rm -rf "$RELEASE_DIR"
    git -C "${SOURCE_DIR}" tag -d "${NEW_TAG}"
    git -C "${SOURCE_DIR}" push "${REMOTE_NAME}" --delete "${NEW_TAG}" 2>/dev/null || true
    return 1 2>/dev/null || exit 1
fi

cp "${SOURCE_DIR}"/*.py "${RELEASE_DIR}/"
echo -e "${GREEN}  ✔ Copied Python source files:${NC}"
for f in "${RELEASE_DIR}"/*.py; do
    echo -e "     - $(basename "$f")"
done

if [[ -e "${YAML_FILES[0]}" ]]; then
    cp "${SOURCE_DIR}"/*.yaml "${RELEASE_DIR}/"
    echo -e "${GREEN}  ✔ Copied YAML files:${NC}"
    for f in "${RELEASE_DIR}"/*.yaml; do
        echo -e "     - $(basename "$f")"
    done
else
    echo -e "${YELLOW}  ℹ No *.yaml files found; skipping.${NC}"
fi

if [[ -e "${DO_FILES[0]}" ]]; then
    cp "${SOURCE_DIR}"/*.do "${RELEASE_DIR}/"
    echo -e "${GREEN}  ✔ Copied .do files:${NC}"
    for f in "${RELEASE_DIR}"/*.do; do
        echo -e "     - $(basename "$f")"
    done
else
    echo -e "${YELLOW}  ℹ No *.do files found; skipping.${NC}"
fi

SH_FILES=()
while IFS= read -r -d '' f; do
    [[ "$(basename "$f")" == "release.sh" ]] && continue
    SH_FILES+=("$f")
done < <(find "${SOURCE_DIR}" -maxdepth 1 -name "*.sh" ! -name "release.sh" -print0)

if [[ ${#SH_FILES[@]} -gt 0 ]]; then
    cp "${SH_FILES[@]}" "${RELEASE_DIR}/"
    echo -e "${GREEN}  ✔ Copied shell script files:${NC}"
    for f in "${RELEASE_DIR}"/*.sh; do
        echo -e "     - $(basename "$f")"
    done
else
    echo -e "${YELLOW}  ℹ No *.sh files found (excluding release.sh); skipping.${NC}"
fi

# =============================================================================
# STEP 5: Byte-Compile Python Files and Remove Source in Release Directory ONLY
# =============================================================================
echo -e "${CYAN}[5/6] Byte-compiling Python files in release directory...${NC}"
echo -e "${YELLOW}  ⚠  Compilation will run ONLY inside: ${RELEASE_DIR}${NC}"
echo -e "${YELLOW}  ⚠  Source directory is untouched:    ${SOURCE_DIR}${NC}"

# Safety check: ensure we are NOT in the source directory
if [[ "$(realpath "${RELEASE_DIR}")" == "$(realpath "${SOURCE_DIR}")" ]]; then
    echo -e "${RED}CRITICAL: Release dir and source dir are the same! Aborting to protect source files.${NC}"
    rm -rf "$RELEASE_DIR"
    git -C "${SOURCE_DIR}" tag -d "${NEW_TAG}"
    git -C "${SOURCE_DIR}" push "${REMOTE_NAME}" --delete "${NEW_TAG}" 2>/dev/null || true
    return 1 2>/dev/null || exit 1
fi

# Byte-compile all .py files in the release directory.
# -b  : write .pyc next to the .py (not inside __pycache__) — legacy layout, directly runnable
# -q  : quiet (set to -v for verbose)
(
    cd "${RELEASE_DIR}" || { echo -e "${RED}Cannot cd into ${RELEASE_DIR}${NC}"; exit 1; }

    echo -e "${YELLOW}  Running: ${PYTHON3} -m compileall -b .${NC}"
    "${PYTHON3}" -m compileall -b -q .

    COMPILE_STATUS=$?
    if [[ $COMPILE_STATUS -ne 0 ]]; then
        echo -e "${RED}ERROR: Python byte-compilation failed with exit code ${COMPILE_STATUS}.${NC}"
        exit $COMPILE_STATUS
    fi

    # Remove __pycache__ created by compileall itself
    rm -rf __pycache__
)

# Capture subshell exit status
if [[ $? -ne 0 ]]; then
    echo -e "${RED}Compilation step failed. Aborting release. Cleaning up...${NC}"
    rm -rf "$RELEASE_DIR"
    git -C "${SOURCE_DIR}" tag -d "${NEW_TAG}"
    git -C "${SOURCE_DIR}" push "${REMOTE_NAME}" --delete "${NEW_TAG}" 2>/dev/null || true
    return 1 2>/dev/null || exit 1
fi

# Remove Python source files — keep only .pyc (in __pycache__) and .yaml
echo -e "${YELLOW}  Removing Python source (.py) files from release directory...${NC}"
rm -f "${RELEASE_DIR}"/*.py

# Grant 755 permissions to all files in the release directory
echo -e "${YELLOW}  Setting 755 permissions on all files in release directory...${NC}"
find "${RELEASE_DIR}" -type f -exec chmod 755 {} +
echo -e "${GREEN}  ✔ Permissions set to 755 on all files.${NC}"

echo -e "${GREEN}  ✔ Byte-compilation complete. Remaining files in release directory:${NC}"
find "${RELEASE_DIR}" -type f | sort | while read -r f; do
    echo -e "     - ${f#${RELEASE_DIR}/}"
done

# =============================================================================
# STEP 6: Update 'latest' Symlink AFTER Successful Compilation
# =============================================================================
echo -e "${CYAN}[6/6] Updating 'latest' symlink...${NC}"

# Remove old 'latest' symlink if it exists
if [[ -L "$LATEST_LINK" ]]; then
    OLD_LATEST=$(readlink "$LATEST_LINK")
    rm "$LATEST_LINK"
    echo -e "${YELLOW}  ℹ Removed old 'latest' link (was -> ${OLD_LATEST})${NC}"
fi

# Create new 'latest' symlink pointing to new release
ln -s "${RELEASE_DIR}" "${LATEST_LINK}"
echo -e "${GREEN}  ✔ 'latest' now points to: ${RELEASE_DIR}${NC}"

# =============================================================================
# Summary
# =============================================================================
echo -e "\n${CYAN}============================================${NC}"
echo -e "${GREEN}  ✅ Release Pipeline Complete!${NC}"
echo -e "${CYAN}============================================${NC}"
echo -e "${GREEN}  Tag    : ${NEW_TAG}${NC}"
echo -e "${GREEN}  Release: ${RELEASE_DIR}${NC}"
echo -e "${GREEN}  Latest : ${LATEST_LINK} -> ${RELEASE_DIR}${NC}"
echo -e "${CYAN}============================================${NC}"
