#!/usr/bin/env bash
#
#  Live view of what the gateway is doing, in one stream.
#
#  Read-only: it follows container logs and nothing else, so it can be started
#  and stopped at will against a running system.
#
#  Three sources are merged because no single one tells the whole story. The
#  API says what was asked and what the card did; the worker says what happened
#  on-chain; the acquirer simulator is the bank's own account of the same
#  events, which is the only one that can contradict ours.
#
#      scripts/watch.sh            # payments only — the default, and readable
#      scripts/watch.sh --all      # every line, health checks and beats included
#      scripts/watch.sh --tx 42    # one transaction, from authorisation to chain
#
set -uo pipefail
cd "$(dirname "$0")/.."

BOLD=$'\033[1m'; DIM=$'\033[2m'; OFF=$'\033[0m'
RED=$'\033[31m'; GREEN=$'\033[32m'; YELLOW=$'\033[33m'
BLUE=$'\033[34m'; MAGENTA=$'\033[35m'; CYAN=$'\033[36m'

MODE="payments"; ONLY_TX=""
case "${1:-}" in
    --all) MODE="all" ;;
    --tx)  ONLY_TX="${2:?usage: scripts/watch.sh --tx <id>}"; MODE="all" ;;
    "") ;;
    *) echo "usage: $0 [--all | --tx <id>]"; exit 1 ;;
esac

for c in gateway-api-prod gateway-worker-prod gateway-bank-prodtest; do
    podman inspect "$c" >/dev/null 2>&1 || {
        echo "${RED}$c is not running. Start the stack first: scripts/prodtest.sh${OFF}"; exit 1; }
done

echo
echo "  ${BOLD}Gateway — live${OFF}   ${DIM}Ctrl-C to stop; nothing here writes anything${OFF}"
if [[ -n "${ONLY_TX}" ]]; then
    echo "  ${DIM}transaction ${ONLY_TX} only${OFF}"
elif [[ "${MODE}" == "payments" ]]; then
    echo "  ${DIM}payments only — run with --all to see health checks and scheduled tasks${OFF}"
fi
echo

# Each source is tagged as it is read, then all three go through one formatter,
# so the ordering the operator sees is the order events actually arrived.
{
    podman logs -f --since 1s gateway-api-prod    2>&1 | sed -u 's/^/api|/'  &
    podman logs -f --since 1s gateway-worker-prod 2>&1 | sed -u 's/^/job|/'  &
    podman logs -f --since 1s gateway-bank-prodtest 2>&1 | sed -u 's/^/bank|/' &
    wait
} | WATCH_MODE="${MODE}" WATCH_TX="${ONLY_TX}" python3 -u scripts/_watch_format.py
