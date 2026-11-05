#!/usr/bin/env bash

# Put the grow light where the saved schedule says it should be *right now*.
# Cron only fires at the on/off edges, so after a power cut or a service
# restart the light stays wrong until the next edge. Run this at boot or any
# time the light may have been zeroed; prints what it did.

set -euo pipefail

GOE_PATH=$(realpath "$(dirname "$(readlink -e "${0}")")/..")
export PYTHONPATH="${GOE_PATH}${PYTHONPATH:+:${PYTHONPATH}}"

desired=$("${GOE_PATH}/venv/bin/python" -c \
    "from app.sensors.schedule import schedule as s; d = s.reconcile_light(); print('none' if d is None else d)")

case "${desired}" in
    none) echo "no light schedule to reconcile against" ;;
    0)    "${GOE_PATH}/bin/light.sh" off; echo "light off per schedule" ;;
    *)    "${GOE_PATH}/bin/light.sh" "${desired}"; echo "light ${desired}% per schedule" ;;
esac
