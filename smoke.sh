#!/usr/bin/env bash
# One-shot harness smoke test. Runs orchestrator step 1 against qwen2.5:3b
# in ~/projects/harness_smoke/. No args.
exec python3 "$HOME/scripts/sensei_3file/orchestrator.py" \
    --workdir "$HOME/projects/harness_smoke" \
    --max-steps 1
