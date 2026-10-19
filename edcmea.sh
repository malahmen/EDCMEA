#!/usr/bin/env bash
# EDCMEA's launcher: what cron and scomp-link run. The engine is Python, standard
# library only; this only finds a python3 and hands it every argument.
#   edcmea.sh check            (cron: */10 * * * * /path/to/edcmea.sh check)
set -euo pipefail
exec "${EDCMEA_PYTHON:-python3}" "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/edcmea.py" "$@"
