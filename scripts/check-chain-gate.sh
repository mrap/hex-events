#!/bin/bash
# Check if all prerequisite BOI specs have completed.
# Usage: check-chain-gate.sh <state_file> <spec_id1> <spec_id2> ...
#
# Records completed spec IDs in the state file.
# Exits 0 (gate open) when ALL prerequisites are met.
# Exits 1 (gate closed) when some are still pending.

set -uo pipefail

STATE_FILE="${1:?Usage: check-chain-gate.sh <state_file> <spec_id> ...}"
shift
REQUIRED_SPECS=("$@")

mkdir -p "$(dirname "$STATE_FILE")"

# Initialize state file if it doesn't exist
if [[ ! -f "$STATE_FILE" ]]; then
    echo '{"completed": []}' > "$STATE_FILE"
fi

# Record the triggering spec as completed (passed via HEX_EVENT_SPEC_ID env var)
if [[ -n "${HEX_EVENT_SPEC_ID:-}" ]]; then
    python3 -c "
import json, sys
with open('$STATE_FILE') as f:
    state = json.load(f)
spec_id = '${HEX_EVENT_SPEC_ID}'
if spec_id not in state['completed']:
    state['completed'].append(spec_id)
with open('$STATE_FILE', 'w') as f:
    json.dump(state, f)
print(f'Recorded {spec_id} as completed. Total: {len(state[\"completed\"])}')
"
fi

# Check if all required specs are completed
python3 -c "
import json, sys
with open('$STATE_FILE') as f:
    state = json.load(f)
required = set(sys.argv[1:])
completed = set(state['completed'])
missing = required - completed
if missing:
    print(f'Gate CLOSED. Waiting on: {missing}')
    sys.exit(1)
else:
    print(f'Gate OPEN. All prerequisites met: {completed}')
    sys.exit(0)
" "${REQUIRED_SPECS[@]}"
