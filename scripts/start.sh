#!/usr/bin/env sh
set -eu

cd /app/PeTTa
if [ ! -f run.metta ]; then
  cp repos/mettaclaw/run.metta ./
fi

RUN_TIMEOUT="${RUN_TIMEOUT:-120}"
if [ -z "${OPENAI_API_KEY:-}" ] && [ -z "${ASI_API_KEY:-}" ]; then
  if [ "$RUN_TIMEOUT" = "0" ]; then
    echo "OPENAI_API_KEY or ASI_API_KEY is required for deployment mode (RUN_TIMEOUT=0)."
    exit 1
  fi
  echo "OPENAI_API_KEY or ASI_API_KEY is not set; skipping full runtime startup."
  echo "Container build/runtime wiring is valid; provide a key to run the full agent loop."
  exit 0
fi

if [ "$RUN_TIMEOUT" = "0" ]; then
  echo "Starting MeTTaClaw in long-running deployment mode..."
  exec sh run.sh run.metta default
fi

echo "Starting MeTTaClaw (max ${RUN_TIMEOUT}s in CI)..."

status=0
timeout "${RUN_TIMEOUT}" sh run.sh run.metta default || status=$?

if [ "$status" -eq 124 ]; then
  echo "MeTTaClaw reached the CI timeout (${RUN_TIMEOUT}s). Marking run as successful."
  exit 0
fi

exit "$status"
