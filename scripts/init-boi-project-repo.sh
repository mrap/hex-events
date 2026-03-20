#!/usr/bin/env bash
set -uo pipefail

# init-boi-project-repo.sh
# Initialize a BOI-built project as a git repo if not already initialized.
# Usage: init-boi-project-repo.sh <path>

if [[ $# -lt 1 ]]; then
    echo "Usage: $0 <repo_path>" >&2
    exit 1
fi

REPO_PATH="$1"
LOG_FILE="${HOME}/.boi/ops-actions.log"

# Expand tilde if present
REPO_PATH="${REPO_PATH/#\~/$HOME}"

if [[ ! -d "$REPO_PATH" ]]; then
    echo "Error: directory not found: $REPO_PATH" >&2
    exit 1
fi

# If .git already exists, nothing to do
if [[ -d "$REPO_PATH/.git" ]]; then
    echo "Already a git repo: $REPO_PATH"
    exit 0
fi

cd "$REPO_PATH"

# Initialize repo
git init

# Create .gitignore
cat > .gitignore << 'GITIGNORE'
__pycache__/
*.pyc
data/*.db
data/reports/
data/heartbeat.txt
*.log
GITIGNORE

# Stage all files (respects .gitignore)
git add -A

# Count files being committed
FILE_COUNT=$(git diff --cached --name-only | wc -l | tr -d ' ')

# Commit
git commit -m "Initial commit — BOI-built project"

# Log the action
TIMESTAMP=$(date '+%Y-%m-%d %H:%M')
mkdir -p "$(dirname "$LOG_FILE")"
echo "${TIMESTAMP} — repo-init: ${REPO_PATH} (${FILE_COUNT} files)" >> "$LOG_FILE"

echo "Initialized git repo at: $REPO_PATH ($FILE_COUNT files committed)"
exit 0
