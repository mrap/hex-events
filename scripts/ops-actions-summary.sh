#!/usr/bin/env bash
# ops-actions-summary.sh — Summarize hex-ops autonomous actions from ops-actions.log
set -uo pipefail

LOG_FILE="${HOME}/.boi/ops-actions.log"

# Compute cutoff date 7 days ago
CUTOFF=$(date -v-7d '+%Y-%m-%d' 2>/dev/null || date -d '7 days ago' '+%Y-%m-%d' 2>/dev/null || echo "0000-00-00")

echo "hex-ops Actions (last 7 days)"

if [[ ! -f "${LOG_FILE}" ]] || [[ ! -s "${LOG_FILE}" ]]; then
    echo "  auto-commits: 0"
    echo "  auto-pushes: 0"
    echo "  repo-inits: 0"
    echo ""
    echo "Recent:"
    echo "  (no actions recorded)"
    exit 0
fi

# Count actions in last 7 days
auto_commits=0
auto_pushes=0
repo_inits=0

while IFS= read -r line; do
    # Extract date from line (format: YYYY-MM-DD HH:MM — ...)
    line_date="${line:0:10}"
    [[ "${line_date}" < "${CUTOFF}" ]] && continue

    if [[ "${line}" == *"— auto-commit:"* ]]; then
        auto_commits=$((auto_commits + 1))
        # Count pushes by checking for push=pushed
        if [[ "${line}" == *"push=pushed"* ]]; then
            auto_pushes=$((auto_pushes + 1))
        fi
    elif [[ "${line}" == *"— repo-init:"* ]]; then
        repo_inits=$((repo_inits + 1))
    fi
done < "${LOG_FILE}"

echo "  auto-commits: ${auto_commits}"
echo "  auto-pushes: ${auto_pushes}"
echo "  repo-inits: ${repo_inits}"
echo ""
echo "Recent:"

# Show last 10 entries (most recent first)
recent=$(tail -10 "${LOG_FILE}" | tac 2>/dev/null || tail -10 "${LOG_FILE}" | tail -r 2>/dev/null || tail -10 "${LOG_FILE}")

if [[ -z "${recent}" ]]; then
    echo "  (no actions recorded)"
else
    while IFS= read -r line; do
        echo "  ${line}"
    done <<< "${recent}"
fi
