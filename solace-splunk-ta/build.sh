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
VERSION="${VERSION:-0.2.1}"
OUT="${OUT:-${TMPDIR:-/tmp}/ta-out}"

rm -rf "$OUT"
# running the unit tests byte-compiles package/bin; AppInspect rejects .pyc files
find package -name __pycache__ -prune -exec rm -rf {} + 2>/dev/null || true
ucc-gen build --source package --config globalConfig.json -o "$OUT" --ta-version "$VERSION"
find "$OUT/ta_solace_semp" -type d -exec chmod 755 {} +
find "$OUT/ta_solace_semp" -type f -exec chmod 644 {} +
ucc-gen package --path "$OUT/ta_solace_semp" -o "$OUT"
echo "Package: $OUT/ta_solace_semp-$VERSION.tar.gz"

if command -v splunk-appinspect >/dev/null; then
  splunk-appinspect inspect "$OUT/ta_solace_semp-$VERSION.tar.gz" \
    --included-tags cloud --included-tags private_victoria \
    --output-file "$OUT/appinspect.json" --data-format json | tail -12
fi
