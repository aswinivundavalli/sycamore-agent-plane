#!/usr/bin/env bash
# Start the conductor. No dependencies beyond python3 (3.11+) and git (2.38+).
cd "$(dirname "$0")" && exec python3 -m conductor "$@"
