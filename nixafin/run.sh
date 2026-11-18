#!/usr/bin/env bash
# Run the Nixafin prospecting runner with this checkout's virtual environment.
#   nixafin/run.sh --import nixafin/companies.csv
#   nixafin/run.sh --discover
#   nixafin/run.sh --export-only
set -euo pipefail
cd "$(dirname "$0")/.."
source .venv/bin/activate
# Cloud container only: read websites through the session proxy. Leave this
# unset on a normal computer (remove the line or set SELLBIRD_WEBSITE_PROXY=).
if [ -n "${HTTPS_PROXY:-}" ]; then export SELLBIRD_WEBSITE_PROXY="${SELLBIRD_WEBSITE_PROXY:-session}"; fi
exec python nixafin/prospect.py "$@"
