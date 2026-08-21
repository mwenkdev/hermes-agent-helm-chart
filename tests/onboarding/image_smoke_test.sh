#!/usr/bin/env bash
# Image compatibility smoke test for the onboarding validator.
#
# Runs files/onboarding/check.py inside the exact official Hermes image the
# chart deploys, against an empty temporary HERMES_HOME. This catches moved or
# renamed upstream internal modules before a chart release -- without building
# a derivative image.
#
#   * exit 10 (incomplete)        -> adapters imported, nothing configured: PASS
#   * exit 20 (validator error)   -> an adapter no longer matches this image: FAIL
#   * exit 0                      -> unexpected; an empty home cannot be complete
#
# Usage: tests/onboarding/image_smoke_test.sh [image-ref]

set -euo pipefail

CHART_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"

image="${1:-}"
if [ -z "$image" ]; then
    repository="$(sed -n 's/^  repository: *//p' "${CHART_ROOT}/values.yaml" | head -1)"
    tag="$(sed -n 's/^  tag: *"\{0,1\}\([^"]*\)"\{0,1\} *$/\1/p' "${CHART_ROOT}/values.yaml" | head -1)"
    image="${repository}:${tag}"
fi

workdir="$(mktemp -d)"

# The validator runs as the image's own user and seeds HERMES_HOME with
# restrictive-mode state directories (logs/, sessions/, ...), which the host
# user cannot then delete. Hand ownership back from inside a container before
# touching those paths.
reclaim_workdir() {
    docker run --rm -v "$workdir:/work" \
        --entrypoint chown "$image" -R "$(id -u):$(id -g)" /work \
        >/dev/null 2>&1 || true
}

cleanup() {
    reclaim_workdir
    rm -rf "$workdir"
}
trap cleanup EXIT

mkdir -p "$workdir/home" "$workdir/scripts"
cp "${CHART_ROOT}/files/onboarding/check.py" "$workdir/scripts/"
chmod -R a+rwX "$workdir"

run_case() {
    local label="$1"
    local requirements="$2"
    local expected="$3"

    reclaim_workdir
    printf '%s' "$requirements" > "$workdir/scripts/requirements.json"
    rm -f "$workdir/home/.helm-onboarding-complete.json"

    set +e
    output="$(docker run --rm \
        -e HERMES_HOME=/hermes-home \
        -v "$workdir/scripts:/scripts:ro" \
        -v "$workdir/home:/hermes-home" \
        --entrypoint /opt/hermes/.venv/bin/python \
        "$image" \
        /scripts/check.py --requirements /scripts/requirements.json 2>&1)"
    status=$?
    set -e

    printf '%s\n' "$output"
    reclaim_workdir
    if [ "$status" -ne "$expected" ]; then
        echo "FAIL [$label]: expected exit ${expected}, got ${status}" >&2
        exit 1
    fi
    if [ -f "$workdir/home/.helm-onboarding-complete.json" ]; then
        echo "FAIL [$label]: an incomplete run must not write a completion latch" >&2
        exit 1
    fi
    echo "PASS [$label] (exit ${status})"
}

echo "Running onboarding validator smoke test against ${image}"

run_case "provider only" \
    '{"schemaVersion":1,"validatorVersion":1,"provider":true,"platforms":[]}' 10

run_case "all supported platforms" \
    '{"schemaVersion":1,"validatorVersion":1,"provider":true,"platforms":["discord","photon","telegram"]}' 10

echo "Onboarding validator is compatible with ${image}"
