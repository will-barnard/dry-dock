#!/usr/bin/env bash
# Run the eval harness. One command, no setup.
#
#   ./run.sh                          # test with the default model
#   ./run.sh qwen2.5-coder:14b        # test a different model
#
# Reuses the worker Docker image already built on this machine, so there is no
# venv to create and nothing to install. Your repo is mounted over /app, so it
# always runs the code you have right now.

set -euo pipefail
cd "$(dirname "$0")/.."          # worker/

MODEL="${1:-qwen2.5-coder:32b}"
IMAGE="drydock-worker:latest"

if ! docker image inspect "$IMAGE" >/dev/null 2>&1; then
  echo "error: docker image '$IMAGE' not found."
  echo "Build it first:  cd $(pwd) && ./workers.sh rebuild"
  exit 1
fi

if ! curl -fsS http://localhost:11434/api/tags >/dev/null 2>&1; then
  echo "warning: Ollama did not answer on localhost:11434."
  echo "Start it, or the run will fail on the first task."
  echo
fi

echo "Running 6 tasks x 3 attempts against $MODEL."
echo "Each attempt is a full model call, so this takes a while."
echo

docker run --rm -i \
  -v "$PWD:/app" \
  -w /app/evals \
  -e OLLAMA_BASE_URL=http://host.docker.internal:11434 \
  --add-host host.docker.internal:host-gateway \
  "$IMAGE" \
  python3 run_evals.py --model "$MODEL" --repeat 3 --out /app/evals/results.jsonl

echo
echo "Done. The summary above is what matters."
echo "Per-run detail is in worker/evals/results.jsonl"
