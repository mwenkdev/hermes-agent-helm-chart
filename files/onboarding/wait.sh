#!/bin/sh
# shellcheck shell=sh
#
# Onboarding gate wait wrapper for the Hermes Agent Helm chart.
#
# Runs check.py once per poll interval, each attempt externally bounded by
# ONBOARDING_VALIDATION_TIMEOUT_SECONDS, and exits 0 as soon as the validator
# reports that every declared requirement is satisfied.  Until then it keeps
# the init container alive so an operator can
#
#     kubectl exec -it <pod> -c onboarding-gate -- sh
#
# and run the normal interactive Hermes setup commands.
#
# Invoked explicitly through /bin/sh; it never relies on the executable bit
# (ConfigMap mounts are 0644 by default) or on bash being present.

set -u

ONBOARDING_DIR="${ONBOARDING_DIR:-/opt/hermes-chart/onboarding}"
REQUIREMENTS="${ONBOARDING_REQUIREMENTS:-${ONBOARDING_DIR}/requirements.json}"
CHECK="${ONBOARDING_CHECK:-${ONBOARDING_DIR}/check.py}"
POLL_INTERVAL="${ONBOARDING_POLL_INTERVAL_SECONDS:-10}"
VALIDATION_TIMEOUT="${ONBOARDING_VALIDATION_TIMEOUT_SECONDS:-15}"
# Reprint the full missing-item report every N unchanged attempts so the log
# stays usable while still being greppable long after the first failure.
FULL_REPORT_EVERY="${ONBOARDING_FULL_REPORT_EVERY:-30}"

EXIT_INCOMPLETE=10
EXIT_VALIDATOR_ERROR=20
EXIT_TIMEOUT=124

log() {
    printf '%s onboarding-gate: %s\n' "$(date -u '+%Y-%m-%dT%H:%M:%SZ')" "$*"
}

# Resolve the interpreter that owns the installed Hermes packages. The
# official image puts its venv first on PATH, but resolve the absolute path
# first so a PATH rewrite (e.g. npmPackages) cannot select a bare system
# python without the Hermes distribution.
resolve_python() {
    if [ -n "${ONBOARDING_PYTHON:-}" ] && [ -x "${ONBOARDING_PYTHON}" ]; then
        printf '%s' "${ONBOARDING_PYTHON}"
        return 0
    fi
    if [ -x /opt/hermes/.venv/bin/python ]; then
        printf '%s' /opt/hermes/.venv/bin/python
        return 0
    fi
    for candidate in python3 python; do
        if command -v "$candidate" >/dev/null 2>&1; then
            command -v "$candidate"
            return 0
        fi
    done
    return 1
}

PYTHON="$(resolve_python)" || {
    log "FATAL: no python interpreter found; cannot run the onboarding validator"
    exit 1
}

run_check() {
    if command -v timeout >/dev/null 2>&1; then
        timeout -k 5 "${VALIDATION_TIMEOUT}" \
            "${PYTHON}" "${CHECK}" --requirements "${REQUIREMENTS}" 2>&1
    else
        # No coreutils timeout: run unbounded rather than not at all. The
        # official image ships coreutils, so this is a defensive fallback.
        "${PYTHON}" "${CHECK}" --requirements "${REQUIREMENTS}" 2>&1
    fi
}

log "waiting for onboarding requirements from ${REQUIREMENTS}"
log "interpreter=${PYTHON} poll=${POLL_INTERVAL}s timeout=${VALIDATION_TIMEOUT}s"
log "exec into this container to complete setup: kubectl exec -it <pod> -c onboarding-gate -- sh"

attempt=0
unchanged=0
previous=""

while true; do
    attempt=$((attempt + 1))
    output="$(run_check)"
    status=$?

    if [ "$status" -eq 0 ]; then
        [ -n "$output" ] && printf '%s\n' "$output"
        log "requirements satisfied after ${attempt} attempt(s); starting Hermes"
        exit 0
    fi

    case "$status" in
        "$EXIT_INCOMPLETE")
            headline="configuration incomplete - operator action required"
            ;;
        "$EXIT_VALIDATOR_ERROR")
            headline="VALIDATOR ERROR - this is a chart/validator problem, not missing setup"
            ;;
        "$EXIT_TIMEOUT")
            headline="validation attempt timed out after ${VALIDATION_TIMEOUT}s (no latch written)"
            output=""
            ;;
        *)
            headline="validator exited with unexpected status ${status}"
            ;;
    esac

    if [ "$output" = "$previous" ] && [ "$attempt" -gt 1 ]; then
        unchanged=$((unchanged + 1))
    else
        unchanged=0
    fi
    previous="$output"

    if [ "$unchanged" -eq 0 ] || [ $((unchanged % FULL_REPORT_EVERY)) -eq 0 ]; then
        log "$headline"
        [ -n "$output" ] && printf '%s\n' "$output"
    else
        log "still waiting (attempt ${attempt}, status ${status}); diagnostics unchanged"
    fi

    sleep "${POLL_INTERVAL}"
done
