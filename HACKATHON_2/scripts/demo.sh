#!/usr/bin/env bash
# The demo, as a command rather than a memory.
#
# Runs the five things worth showing, in order, with a pause between each.
#   bash scripts/demo.sh            # interactive
#   bash scripts/demo.sh --no-wait  # straight through
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."

WAIT=1
[ "${1:-}" = "--no-wait" ] && WAIT=0
DOMAIN="${DOMAIN:-sample_policy}"
export DOMAIN

step() {
    printf '\n\n\033[1m== %s ==\033[0m\n\n' "$1"
    [ "$WAIT" = "1" ] && read -r -p "  [enter]" _ || true
}

step "1. The line of processing - nine stages, readable from the tree"
ls -1 src/agentcore/pipeline/ | grep -E '^s[0-9]'

step "2. Retrieval is measured, not asserted (and the metadata filter earns its place)"
uv run python -m evaluation.retrieval_eval

step "3. The distance ceiling is calibrated to THIS corpus, not inherited"
uv run python -m evaluation.calibrate

step "4. Safety holds without a network: risk floor, fail-closed approval, injection"
uv run pytest tests/test_safety_invariants.py -q

step "5. The eval gate fails the build on regression"
uv run python -m evaluation.gate --skip-agent || echo "(gate exited non-zero, as designed)"

step "6. The seam: every domain satisfies the contract, no vocabulary leaked into core"
uv run pytest tests/test_domain_contract.py tests/test_no_domain_leakage.py -q

printf '\n\nDone. The swap: cp -r src/domains/_template src/domains/<name>\n'
