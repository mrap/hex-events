#!/bin/bash
set -uo pipefail

# check-devserver.sh — Poll for dev7_2xlarge availability near NYC
# Used by hex-events devserver-monitor policy.
#
# Strategy:
#   1. Try devenv-admin lease list (if installed) to check pool capacity
#   2. Try buck2 run devmanager pools (if in fbsource checkout)
#   3. If neither works, emit a "manual check needed" event
#
# Target regions (nearest NYC): PRN (Princeton), ASH (Ashburn), LDC (Loudoun)

STATE_FILE="/tmp/devserver-availability.json"
HEX_EMIT="$HOME/.hex-events/hex_emit.py"
TARGET_TYPE="dev7_2xlarge"
TARGET_REGIONS=("PRN" "ASH" "LDC")
TIMESTAMP=$(date -u +"%Y-%m-%dT%H:%M:%SZ")

emit_available() {
    local count="$1"
    local regions="$2"
    python3 "$HEX_EMIT" devserver.available \
        "{\"type\": \"$TARGET_TYPE\", \"location\": \"NYC\", \"count\": $count, \"regions\": \"$regions\", \"timestamp\": \"$TIMESTAMP\"}" \
        devserver-monitor
}

# Write state file with result
write_state() {
    local available="$1"
    local count="${2:-0}"
    local method="${3:-unknown}"
    local regions="${4:-}"
    cat > "$STATE_FILE.tmp" <<JSONEOF
{
  "timestamp": "$TIMESTAMP",
  "target_type": "$TARGET_TYPE",
  "available": $available,
  "count": $count,
  "method": "$method",
  "regions": "$regions"
}
JSONEOF
    mv "$STATE_FILE.tmp" "$STATE_FILE"
}

# --- Method 1: devenv-admin (preferred, if installed) ---
if command -v devenv-admin &>/dev/null || command -v devenv_admin &>/dev/null; then
    CMD=$(command -v devenv-admin 2>/dev/null || command -v devenv_admin 2>/dev/null)

    # Use 'lease list' to check if there are available leases in the pool
    # Note: devenv-admin doesn't have a direct "check availability" command,
    # but lease list with pool filters can indicate capacity
    OUTPUT=$("$CMD" lease list --status available --wide 2>/dev/null) || true

    if echo "$OUTPUT" | grep -qi "$TARGET_TYPE"; then
        # Count matching lines (rough availability estimate)
        COUNT=$(echo "$OUTPUT" | grep -ci "$TARGET_TYPE" || echo "0")
        MATCHING_REGIONS=""
        for r in "${TARGET_REGIONS[@]}"; do
            if echo "$OUTPUT" | grep -qi "$r"; then
                MATCHING_REGIONS="${MATCHING_REGIONS}${r} "
            fi
        done
        if [ -n "$MATCHING_REGIONS" ]; then
            write_state "true" "$COUNT" "devenv-admin" "$MATCHING_REGIONS"
            emit_available "$COUNT" "$MATCHING_REGIONS"
            exit 0
        fi
    fi

    write_state "false" 0 "devenv-admin"
    exit 0
fi

# --- Method 2: DaiQuery via meta CLI (if available) ---
if command -v meta &>/dev/null; then
    # Try fetching the pre-built DaiQuery for devserver availability
    # Query ID: 1301149828201280
    OUTPUT=$(meta daiquery run 1301149828201280 --format json 2>/dev/null) || true

    if [ -n "$OUTPUT" ] && echo "$OUTPUT" | python3 -c "
import sys, json
try:
    data = json.load(sys.stdin)
    # Look for dev7_2xlarge rows with availability > 0 in target regions
    for row in data if isinstance(data, list) else data.get('rows', []):
        dtype = str(row.get('devserver_type', ''))
        region = str(row.get('region', ''))
        avail = int(row.get('availability', row.get('num_available', 0)))
        if 'dev7_2xlarge' in dtype and region in ('PRN','ASH','LDC') and avail > 0:
            print(f'{region}:{avail}')
            sys.exit(0)
    sys.exit(1)
except:
    sys.exit(1)
" 2>/dev/null; then
        RESULT=$(echo "$OUTPUT" | python3 -c "
import sys, json
data = json.load(sys.stdin)
total = 0
regions = []
for row in data if isinstance(data, list) else data.get('rows', []):
    dtype = str(row.get('devserver_type', ''))
    region = str(row.get('region', ''))
    avail = int(row.get('availability', row.get('num_available', 0)))
    if 'dev7_2xlarge' in dtype and region in ('PRN','ASH','LDC') and avail > 0:
        total += avail
        regions.append(region)
print(f'{total}|{\" \".join(regions)}')
" 2>/dev/null)
        COUNT=$(echo "$RESULT" | cut -d'|' -f1)
        REGIONS=$(echo "$RESULT" | cut -d'|' -f2)
        write_state "true" "$COUNT" "daiquery" "$REGIONS"
        emit_available "$COUNT" "$REGIONS"
        exit 0
    fi

    write_state "false" 0 "daiquery"
    exit 0
fi

# --- Method 3: No programmatic tool available ---
# Write state indicating manual check needed
write_state "false" 0 "none_available"

# Log for debugging
echo "$(date): No devserver availability tool found (devenv-admin, meta daiquery)." \
    >> /tmp/devserver-monitor.log
echo "  Install devenv-admin: devfeature install devenv_admin" \
    >> /tmp/devserver-monitor.log
echo "  Or check manually: https://www.internalfb.com/devservers/reservation" \
    >> /tmp/devserver-monitor.log

exit 0
