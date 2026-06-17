#!/usr/bin/env bash
# RED-ZONE incremental update: refresh app/ source ONLY, reuse the existing .venv + wheels.
# Run from inside the extracted INCREMENTAL bundle:  ./update.sh [PREFIX]
# GUARD: aborts if this bundle's requirements hash differs from the deployed venv's
#        (a deps change requires a FULL deploy -> bootstrap.sh).
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
PREFIX="${1:-/opt/ldo_modeler}"

echo "== LDO modeler incremental update =="
[ -d "$PREFIX/.venv" ] || { echo "ERROR: no venv at $PREFIX -- run bootstrap.sh (full) first"; exit 1; }

new_hash="$(grep -o '"requirements_hash"[^,]*' "$HERE/MANIFEST.json" | sed 's/.*: *"//; s/"//')"
dep_hash="$(grep -o '"requirements_hash"[^,]*' "$PREFIX/MANIFEST.deployed.json" 2>/dev/null \
            | sed 's/.*: *"//; s/"//' || true)"
[ -z "$dep_hash" ] && dep_hash="$(grep -o '"requirements_hash"[^,]*' "$PREFIX/INSTALL.json" \
            | sed 's/.*: *"//; s/"//')"

bld="$(grep -o '"git_sha"[^,]*' "$HERE/MANIFEST.json" | sed 's/.*: *"//; s/"//')"
bdt="$(grep -o '"built_utc"[^,]*' "$HERE/MANIFEST.json" | sed 's/.*: *"//; s/"//')"
echo "   bundle build      : ${bld:0:9} ($bdt)   <-- VERIFY this is the build you expect"
echo "   bundle req-hash   : ${new_hash:0:12}"
echo "   deployed req-hash : ${dep_hash:0:12}"
if [ "$new_hash" != "$dep_hash" ]; then
    echo "ABORT: requirements changed since the deployed venv was built."
    echo "       Run a FULL deploy (bootstrap.sh from a 'package.py full' bundle) instead."
    exit 2
fi

echo "[1/2] refreshing app/ (venv + wheels + results + model untouched) ..."
rm -rf "$PREFIX/app"
cp -r "$HERE/app" "$PREFIX/"
# re-link the persistent user-output stores (the fresh app/ from the bundle has no results/model)
mkdir -p "$PREFIX/results" "$PREFIX/model"
ln -sfn "$PREFIX/results" "$PREFIX/app/results"
ln -sfn "$PREFIX/model"   "$PREFIX/app/model"
# keep the root launchers fresh + present (they must live at the install root, next to .venv)
for L in run_gui update apply; do
    [ -f "$PREFIX/app/deploy/$L" ] && { cp "$PREFIX/app/deploy/$L" "$PREFIX/$L"; chmod +x "$PREFIX/$L"; }
done

echo "[2/2] re-running smoke test ..."
# bundled-Qt isolation (same as bootstrap): beat the box's system/Cadence libQt5Core.so.5
QTLIB="$(echo "$PREFIX"/.venv/lib/python3.*/site-packages/PyQt5/Qt5/lib)"
export LD_LIBRARY_PATH="$QTLIB${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
QT_QPA_PLATFORM=offscreen "$PREFIX/.venv/bin/python" \
    "$PREFIX/app/gui/ldo_modeler.py" --selftest --require-qt

cp "$HERE/MANIFEST.json" "$PREFIX/MANIFEST.deployed.json"
echo "OK. app/ updated to build ${bld:0:9}; venv unchanged."
