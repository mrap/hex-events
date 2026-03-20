#!/usr/bin/env bash
# update-landings-from-boi.sh
# Auto-update landings file when BOI specs are dispatched, completed, or failed.
# Usage: update-landings-from-boi.sh <event_type> <spec_id> <spec_title> <tasks_done> <tasks_total>
set -uo pipefail

EVENT_TYPE="${1:-}"
SPEC_ID="${2:-}"
SPEC_TITLE="${3:-}"
TASKS_DONE="${4:-0}"
TASKS_TOTAL="${5:-0}"

if [[ -z "$EVENT_TYPE" || -z "$SPEC_ID" || -z "$SPEC_TITLE" ]]; then
    echo "Usage: $0 <event_type> <spec_id> <spec_title> <tasks_done> <tasks_total>" >&2
    exit 1
fi

# Get today's date
TODAY=$(bash ~/mrap-hex/.claude/scripts/today.sh)
if [[ -z "$TODAY" ]]; then
    echo "ERROR: today.sh returned empty string — is ~/mrap-hex/.claude/scripts/today.sh present and executable?" >&2
    exit 1
fi
if [[ ! "$TODAY" =~ ^[0-9]{4}-[0-9]{2}-[0-9]{2}$ ]]; then
    echo "ERROR: today.sh returned '${TODAY}' which does not match YYYY-MM-DD format" >&2
    exit 1
fi
LANDINGS_FILE="$HOME/mrap-hex/landings/${TODAY}.md"
OPS_LOG="$HOME/.boi/ops-actions.log"

# If landings file doesn't exist, exit cleanly
if [[ ! -f "$LANDINGS_FILE" ]]; then
    echo "[update-landings-from-boi] No landings file for ${TODAY}, skipping."
    exit 0
fi

# Delegate all markdown manipulation to Python
python3 - "$EVENT_TYPE" "$SPEC_ID" "$SPEC_TITLE" "$TASKS_DONE" "$TASKS_TOTAL" "$LANDINGS_FILE" "$OPS_LOG" <<'PYEOF'
import sys
import re
import os
from datetime import datetime

event_type = sys.argv[1]
spec_id    = sys.argv[2]
spec_title = sys.argv[3]
tasks_done = sys.argv[4]
tasks_total = sys.argv[5]
landings_file = sys.argv[6]
ops_log = sys.argv[7]

STOP_WORDS = {
    'the','a','an','in','on','at','to','for','of','with','and','or',
    'is','are','was','were','be','been','from','by','as','that','this',
    'it','its','into','via','per','vs','etc'
}

def significant_words(text):
    """Extract significant words from text (lowercase, alpha-only, no stop words)."""
    words = re.findall(r'[a-zA-Z0-9]+', text.lower())
    # Split on hyphens and dashes too
    expanded = []
    for w in words:
        expanded.extend(re.split(r'[-_]', w))
    return {w for w in expanded if w and w not in STOP_WORDS and len(w) > 1}

def find_matching_landing(content, spec_title):
    """
    Return (section_header_line_idx, landing_number, landing_name) if 2+ significant words match.
    Returns None if no match.
    """
    spec_words = significant_words(spec_title)
    lines = content.split('\n')
    for i, line in enumerate(lines):
        m = re.match(r'^### (L\d+)\. (.+)$', line)
        if m:
            landing_num = m.group(1)
            landing_name = m.group(2)
            landing_words = significant_words(landing_name)
            overlap = spec_words & landing_words
            if len(overlap) >= 2:
                return (i, landing_num, landing_name)
    return None

def find_last_landing_number(content):
    """Return the highest L-number integer found, or 0 if none."""
    nums = [int(m.group(1)) for m in re.finditer(r'^### L(\d+)\.', content, re.MULTILINE)]
    return max(nums) if nums else 0

def find_table_end_in_section(lines, section_start_idx):
    """
    Find the last line of the markdown table that follows section_start_idx.
    Returns the index of the last table row, or -1 if no table found.
    """
    in_table = False
    last_table_line = -1
    for i in range(section_start_idx + 1, len(lines)):
        line = lines[i]
        # Stop at next section header
        if re.match(r'^#{1,3} ', line):
            break
        if line.startswith('|'):
            in_table = True
            last_table_line = i
        elif in_table and line.strip() == '':
            # Blank line after table — table ended
            break
    return last_table_line

def find_changelog_idx(lines):
    """Return the index of the '## Changelog' line, or -1."""
    for i, line in enumerate(lines):
        if re.match(r'^## Changelog', line):
            return i
    return -1

def make_row(spec_id, spec_title, status):
    return f"| BOI {spec_id}: {spec_title} | BOI | Build | {status} |"

def find_existing_subitem_idx(lines, spec_id, section_start, section_end):
    """Find line index of existing sub-item containing spec_id within the section."""
    for i in range(section_start, section_end + 1):
        if f"BOI {spec_id}:" in lines[i] or f"BOI {spec_id} " in lines[i]:
            return i
    return -1

def update_row_status(line, new_status):
    """Replace the last column (Status) of a markdown table row."""
    # Row format: | col1 | col2 | col3 | Status |
    parts = line.rstrip().rstrip('|').split('|')
    if len(parts) >= 2:
        parts[-1] = f" {new_status} "
        return '|'.join(parts) + '|'
    return line

try:
    with open(landings_file, 'r') as f:
        content = f.read()
except OSError as e:
    print(f"ERROR: Cannot read landings file '{landings_file}': {e}", file=sys.stderr)
    sys.exit(1)

lines = content.split('\n')
action_taken = None
now_str = datetime.now().strftime('%H:%M')

match = find_matching_landing(content, spec_title)

if match:
    section_idx, landing_num, landing_name = match
    # Find table end within this section
    table_end = find_table_end_in_section(lines, section_idx)

    # Find existing sub-item for this spec_id within the section
    section_end_idx = table_end if table_end > 0 else section_idx + 30
    existing_idx = find_existing_subitem_idx(lines, spec_id, section_idx, section_end_idx)

    if event_type == 'dispatched':
        if existing_idx >= 0:
            # Already exists — skip (idempotent)
            action_taken = f"SKIP: sub-item for {spec_id} already in {landing_num}"
        else:
            new_row = make_row(spec_id, spec_title, 'In Progress')
            if table_end >= 0:
                lines.insert(table_end + 1, new_row)
            else:
                # No table found — append after section header
                lines.insert(section_idx + 1, '')
                lines.insert(section_idx + 2, '| Sub-item | Owner | Action | Status |')
                lines.insert(section_idx + 3, '|----------|-------|--------|--------|')
                lines.insert(section_idx + 4, new_row)
            action_taken = f"ADDED sub-item to {landing_num} ({landing_name})"

    elif event_type == 'completed':
        if existing_idx >= 0:
            lines[existing_idx] = update_row_status(lines[existing_idx], 'Done ✓')
            action_taken = f"UPDATED {landing_num} sub-item to Done ✓"
        else:
            new_row = make_row(spec_id, spec_title, 'Done ✓')
            if table_end >= 0:
                lines.insert(table_end + 1, new_row)
            else:
                lines.insert(section_idx + 1, new_row)
            action_taken = f"ADDED Done sub-item to {landing_num}"

    elif event_type == 'failed':
        if existing_idx >= 0:
            lines[existing_idx] = update_row_status(lines[existing_idx], 'Failed')
            action_taken = f"UPDATED {landing_num} sub-item to Failed"
        else:
            action_taken = f"SKIP: no sub-item found for {spec_id} in {landing_num}, not adding on failure"

else:
    # No matching landing
    if event_type == 'dispatched':
        last_num = find_last_landing_number(content)
        new_num = last_num + 1
        new_row = make_row(spec_id, spec_title, 'In Progress')
        new_section = [
            '',
            f'### L{new_num}. {spec_title}',
            '**Priority:** L3 — BOI auto-generated',
            '**Status:** In Progress',
            '',
            '| Sub-item | Owner | Action | Status |',
            '|----------|-------|--------|--------|',
            new_row,
        ]
        # Insert before ## Changelog (or at end)
        cl_idx = find_changelog_idx(lines)
        if cl_idx >= 0:
            for offset, sl in enumerate(new_section):
                lines.insert(cl_idx + offset, sl)
        else:
            lines.extend(new_section)
        action_taken = f"CREATED L{new_num} for {spec_id} (no landing match)"
    else:
        action_taken = f"SKIP: no matching landing for {spec_id}, event={event_type}"

# Append to changelog
if action_taken and not action_taken.startswith('SKIP'):
    cl_idx = find_changelog_idx(lines)
    changelog_entry = f"- {now_str} — BOI {spec_id} {event_type}: {spec_title}"
    if cl_idx >= 0:
        # Find the last '- ' entry in the changelog section and insert after it
        last_entry_idx = cl_idx
        for i in range(cl_idx + 1, len(lines)):
            if lines[i].startswith('- '):
                last_entry_idx = i
            elif lines[i].startswith('#') and i > cl_idx + 1:
                break
        lines.insert(last_entry_idx + 1, changelog_entry)
    else:
        lines.extend(['', '## Changelog', changelog_entry])

# Write atomically
new_content = '\n'.join(lines)
tmp_path = landings_file + '.tmp'
try:
    with open(tmp_path, 'w') as f:
        f.write(new_content)
    os.rename(tmp_path, landings_file)
except OSError as e:
    print(f"ERROR: Cannot write landings file '{landings_file}' (tmp='{tmp_path}'): {e}", file=sys.stderr)
    try:
        os.remove(tmp_path)
    except OSError:
        pass
    sys.exit(1)

# Log to ops-actions.log
timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
log_entry = f"[{timestamp}] update-landings-from-boi: {action_taken} | spec={spec_id} event={event_type}"
os.makedirs(os.path.dirname(ops_log), exist_ok=True)
try:
    with open(ops_log, 'a') as f:
        f.write(log_entry + '\n')
except OSError as e:
    print(f"WARNING: Cannot write ops log '{ops_log}': {e}", file=sys.stderr)

print(log_entry)
PYEOF
