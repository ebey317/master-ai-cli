#!/usr/bin/env bash
# Run tests for a specific test directory (mirrors source package layout).
# Usage: ./scripts/run_tests.sh tests/<subdir>/

set -euo pipefail

if [[ $# -ne 1 ]]; then
    echo "Usage: $0 <test-directory>"
    echo "Example: $0 tests/core/"
    exit 1
fi

TEST_DIR="$1"

if [[ ! -d "$TEST_DIR" ]]; then
    echo "Error: Test directory '$TEST_DIR' does not exist"
    exit 1
fi

# Ensure we're at repo root
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

# Run pytest on the specified test directory with coverage
exec python -m pytest "$TEST_DIR" -v --tb=short "$@"
