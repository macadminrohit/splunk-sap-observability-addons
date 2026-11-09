#!/usr/bin/env bash
# Build, normalise permissions, package and AppInspect-vet the add-on.
#
# Prereqs (one-time):
#   python3.12 -m venv .venv && . .venv/bin/activate
#   pip install splunk-add-on-ucc-framework splunk-appinspect
#   macOS: brew install libmagic   (AppInspect needs it; export DYLD_LIBRARY_PATH=/opt/homebrew/lib)
#
# Output goes to a temp dir, not the (OneDrive) source tree. OneDrive mounts report every file
# as rwx------, which AppInspect rejects (check_for_bin_files), so modes are normalised here.
set -euo pipefail
cd "$(dirname "$0")"
VERSION="${VERSION:-0.2.0}"
OUT="${OUT:-${TMPDIR:-/tmp}/ta-btp-uas-out}"

find package -name __pycache__ -prune -exec rm -rf {} + || true   # test runs leave .pyc files; AppInspect rejects them
rm -rf "$OUT"
ucc-gen build --source package --config globalConfig.json -o "$OUT" --ta-version "$VERSION"
find "$OUT/ta_sap_btp_uas" -type d -exec chmod 755 {} +
find "$OUT/ta_sap_btp_uas" -type f -exec chmod 644 {} +
ucc-gen package --path "$OUT/ta_sap_btp_uas" -o "$OUT"
echo "Package: $OUT/ta_sap_btp_uas-$VERSION.tar.gz"

if command -v splunk-appinspect >/dev/null; then
  splunk-appinspect inspect "$OUT/ta_sap_btp_uas-$VERSION.tar.gz" \
    --included-tags cloud --included-tags private_victoria \
    --output-file "$OUT/appinspect.json" --data-format json | tail -12
fi
