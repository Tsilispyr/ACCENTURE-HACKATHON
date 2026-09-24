#!/usr/bin/env bash
# Build the index for a domain, then report whether retrieval actually works.
#
#   bash scripts/index.sh sample_policy
#   bash scripts/index.sh sample_policy --reset
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."

DOMAIN="${1:-${DOMAIN:-sample_policy}}"; shift || true
export DOMAIN

echo "== indexing '$DOMAIN' =="
uv run python -m agentcore.rag.index "$@"

echo ""
echo "== retrieval quality =="
uv run python -m evaluation.retrieval_eval

echo ""
echo "== distance calibration =="
# Every corpus needs its own ceiling. Skipping this is why a system refuses
# everything or grounds on noise - and it looks like a prompt bug.
uv run python -m evaluation.calibrate
