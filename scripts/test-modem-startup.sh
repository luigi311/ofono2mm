#!/usr/bin/env bash
# Repeatedly restart the bridge and compare Phosh's own WWAN logs with live D-Bus state.
set -Eeuo pipefail
umask 077

usage() {
    cat <<'EOF'
Usage: bash scripts/test-modem-startup.sh [options]

Run as your normal phone user, over USB/Wi-Fi. sudo is used only to restart
ModemManager.service. Phosh and oFono stay running. Ctrl+C stops the test.

  --interval SEC     Minimum seconds between restart starts (default: 60).
                     Samples are taken near the END of this recovery window.
  --cycles N         Number of restarts (default: 20; 0 means until Ctrl+C).
  --confirm N        Consecutive matching observations for a verdict (default: 2).
  --poll SEC         Seconds between confirmation samples (default: 2).
  --ofono-path PATH  oFono modem to compare (default: /ril_0).
  --mm-path PATH     MM modem to compare (default: /org/freedesktop/ModemManager1/Modem/0).
  --output DIR       New directory for CSV results, JSON snapshots and Phosh logs.
  -h, --help         Show this help.

Example:
  bash scripts/test-modem-startup.sh --interval 60 --cycles 50 --output ./before

Uses Phosh's runtime DebugControl.LogDomains interface, temporarily enables only
the WWAN debug domains in addition to existing domains, and restores them on exit.
Run this against the ORIGINAL bridge, then the FIXED bridge, with the same options
and different output directories. The script does not install either build.

MATCH / PHOSH_DESYNC compare Phosh's presence, SIM/enabled state and network type
with registered oFono/MM state. Exact Phosh signal percentage and pixel bar count
are not exposed; oFono/MM percentages are recorded for inspection. Registration
delays, bridge mismatches, incomplete telemetry and errors are counted separately.
The mismatch percentage excludes those inconclusive cycles. Missing Phosh logs
are UNKNOWN, never a successful match. More than one active SIM needs explicit
paths for the modem Phosh tracks. A blocked restart can extend the interval.
EOF
}

interval=60 cycles=20 confirm=2 poll=2
ofono_path=/ril_0
mm_path=/org/freedesktop/ModemManager1/Modem/0
output=''
while (($#)); do
    case "$1" in
        -h|--help) usage; exit 0 ;;
        --interval|--cycles|--confirm|--poll|--ofono-path|--mm-path|--output)
            (($# >= 2)) || { printf 'Missing value for %s\n' "$1" >&2; exit 2; }
            case "$1" in
                --interval) interval=$2 ;; --cycles) cycles=$2 ;;
                --confirm) confirm=$2 ;; --poll) poll=$2 ;;
                --ofono-path) ofono_path=$2 ;; --mm-path) mm_path=$2 ;; --output) output=$2 ;;
            esac
            shift 2 ;;
        *) printf 'Unknown option: %s\n' "$1" >&2; exit 2 ;;
    esac
done
for value in "$interval" "$cycles" "$confirm" "$poll"; do
    [[ $value =~ ^(0|[1-9][0-9]{0,5})$ ]] || { printf 'Timing/count values must be nonnegative integers.\n' >&2; exit 2; }
done
((interval > 0 && confirm > 0 && poll > 0 && interval > (confirm - 1) * poll)) || {
    printf 'Use positive timings and interval > (confirm - 1) * poll.\n' >&2; exit 2;
}
((EUID != 0)) || { printf 'Run as your normal phone user; the script invokes sudo for the restart.\n' >&2; exit 2; }
for command in python3 journalctl systemctl sudo timeout gsettings; do
    command -v "$command" >/dev/null || { printf 'Missing command: %s\n' "$command" >&2; exit 2; }
done
[[ $(gsettings get sm.puri.phosh wwan-backend) == "'modemmanager'" ]] || {
    printf 'This test requires Phosh wwan-backend=modemmanager.\n' >&2; exit 2;
}
script_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
probe=$script_dir/wwan-sync-probe.py
[[ -f $probe ]] || { printf 'Missing helper: %s\n' "$probe" >&2; exit 2; }
sudo -v
output=${output:-"$PWD/modem-sync-$(date +%Y%m%d-%H%M%S)"}
mkdir -- "$output"
output=$(cd -- "$output" && pwd)
state=$output/session.json
csv=$output/results.csv
attempts=0

cleanup() {
    local status=$?
    trap - EXIT
    set +e
    if [[ -f $state ]]; then
        if ! timeout 15s python3 "$probe" restore --state "$state"; then
            printf 'Could not restore logging; original settings are saved in %s\n' "$state" >&2
            if ((status == 0)); then status=1; fi
        fi
    fi
    python3 "$probe" summary --csv "$csv" --attempts "$attempts"
    printf 'Results: %s\n' "$output"
    exit "$status"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
python3 "$probe" prepare --state "$state"
printf 'Checking Phosh WWAN tracking/type; exact rendered bar count is not observable.\n'
printf 'Restart interval: %ss; cycles: %s; output: %s\n' "$interval" "$cycles" "$output"

while ((cycles == 0 || attempts < cycles)); do
    cycle=$((attempts + 1))
    cycle_dir=$output/cycle-$cycle
    mkdir -- "$cycle_dir"
    python3 "$probe" cursor --state "$state" --output "$cycle_dir/cursor"
    attempts=$cycle
    started=$SECONDS
    printf 'Cycle %s: restarting ModemManager.service ...\n' "$cycle"
    # Never overlap restarts, or leave an authentication prompt hidden in a loop.
    if ! timeout 120s sudo -n systemctl restart ModemManager.service; then
        python3 "$probe" error --csv "$csv" --cycle "$cycle" --reason 'ModemManager restart or sudo authentication failed'
        exit 1
    fi
    delay=$((interval - (confirm - 1) * poll - (SECONDS - started)))
    if ((delay > 0)); then sleep "$delay"; fi
    samples=()
    for ((sample = 1; sample <= confirm; sample++)); do
        file=$cycle_dir/sample-$sample.json
        if ! python3 "$probe" sample --state "$state" --cursor "$cycle_dir/cursor" \
                --ofono-path "$ofono_path" --mm-path "$mm_path" --output "$file"; then
            python3 "$probe" error --csv "$csv" --cycle "$cycle" --reason 'State collection failed; see terminal output'
            exit 1
        fi
        samples+=("$file")
        if ((sample < confirm)); then sleep "$poll"; fi
    done
    python3 "$probe" record --csv "$csv" --cycle "$cycle" "${samples[@]}"
    python3 "$probe" summary --csv "$csv" --attempts "$attempts"
done
