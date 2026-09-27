#!/bin/sh
# Rerun a few simulations, see which saved datasets went stale, and refresh them.
# Run analysis.glue first so the datasets exist. This changes files in data/ and results/.
set -e
cd "$(dirname "$0")"

python3 make_data.py --rerun-ed --rerun-dmft
echo
echo "$ glue status"
glue status --trust
echo
echo "$ glue refresh --all"
glue refresh --all --trust
echo
echo "$ glue status"
glue status --trust
