#!/usr/bin/env bash
# Re-sign the test runner. Run at the START of every session, not when it breaks.
#
# Free personal-team profiles expire 7 days after signing. preflight.py refuses
# to start an overnight grind on a profile older than 4.5 days, because the
# runner dying at 3am costs the whole night.
#
# ONE bundle ID for the entire project. Free accounts have an App ID quota per
# rolling 7-day window; minting a fresh ID every time you experiment will
# exhaust it and lock you out mid-project.
set -euo pipefail

cd "$(dirname "$0")"

: "${UDID:?set UDID — find it with: idevice_id -l}"
SCHEME="${SCHEME:-CrossyRunner}"

# -allowProvisioningUpdates is not optional here. Automatic signing is disabled
# by default for CLI builds, so without it xcodebuild refuses to mint the
# profile and fails with "No profiles for 'com.ethan.crossyrunner' were found"
# on a machine that has never built this scheme — i.e. always, the first time.
xcodebuild build-for-testing \
  -scheme "$SCHEME" \
  -destination "platform=iOS,id=${UDID}" \
  -derivedDataPath ./dd \
  -allowProvisioningUpdates

date -u +%s > ../.last_sign
echo "signed at $(date -u) — expires $(date -u -v+7d 2>/dev/null || date -u -d '+7 days')"
