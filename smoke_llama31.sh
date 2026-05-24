#!/usr/bin/env bash
exec python3 "$HOME/scripts/sensei_3file/orchestrator.py" \
    --workdir "$HOME/projects/harness_smoke_llama31" \
    --max-steps 1 \
    --model "llama3.1:8b"
