#!/usr/bin/env bash
# The CI gate. Non-zero exit on regression - that is the whole point.
#
#   bash scripts/eval_gate.sh                  # full: retrieval + agent + judge
#   bash scripts/eval_gate.sh --skip-agent     # retrieval only, no LLM calls
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."

DOMAIN="${DOMAIN:-sample_policy}"
export DOMAIN

echo "== eval gate: $DOMAIN =="
uv run python -m evaluation.gate "$@"
