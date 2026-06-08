#!/usr/bin/env bash
# RED-ZONE full install (airgapped CentOS7-class, glibc 2.17, has python3.11, NO network).
# Run from inside the extracted FULL bundle:  ./bootstrap.sh [PREFIX]
# Layout built:  $PREFIX/{.venv,wheels,app,results,model}  (.venv/wheels/results/model persist;
# app/ is wiped+replaced on incremental update, so user outputs live OUTSIDE it and are linked in).
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
PREFIX="${1:-/opt/ldo_modeler}"
PYBIN="${PYTHON:-python3.11}"

echo "== LDO modeler bootstrap =="
echo "   bundle : $HERE"
echo "   prefix : $PREFIX"
command -v "$PYBIN" >/dev/null 2>&1 || { echo "ERROR: $PYBIN not found on PATH"; exit 1; }
"$PYBIN" -c 'import sys; assert sys.version_info[:2]==(3,11), sys.version' \
    || { echo "ERROR: need Python 3.11 (the wheels are cp311)"; exit 1; }

echo "[1/5] verifying bundle integrity (MANIFEST sha256) ..."
"$PYBIN" - "$HERE" <<'PYEOF'
import sys, json, hashlib, pathlib
here = pathlib.Path(sys.argv[1])
m = json.loads((here / "MANIFEST.json").read_text())
bad = 0
for rel, want in m.get("checksums", {}).items():
    p = here / rel.replace("\\", "/")   # tolerate MANIFESTs built on Windows (backslash keys)
    if not p.exists():
        print("   MISSING", rel); bad += 1; continue
    if hashlib.sha256(p.read_bytes()).hexdigest() != want:
        print("   CORRUPT", rel); bad += 1
if bad:
    print(f"   INTEGRITY FAIL: {bad} file(s) -- bundle is corrupt/incomplete"); sys.exit(1)
print(f"   integrity OK ({len(m.get('checksums', {}))} files)")
PYEOF

mkdir -p "$PREFIX" "$PREFIX/results" "$PREFIX/model"
echo "[2/5] copying app/ + wheels/ + lock + installers ..."
cp -r "$HERE/app"            "$PREFIX/"
cp -r "$HERE/wheels"         "$PREFIX/"
cp    "$HERE/requirements.lock" "$PREFIX/"
cp    "$HERE/update.sh"      "$PREFIX/" 2>/dev/null || true
cp    "$HERE/MANIFEST.json"  "$PREFIX/MANIFEST.deployed.json"
# user outputs (imported refs, emitted models) persist OUTSIDE app/ -> link them in so the GUI's
# ROOT/results and ROOT/model writes land in the persistent stores and survive update.sh.
ln -sfn "$PREFIX/results" "$PREFIX/app/results"
ln -sfn "$PREFIX/model"   "$PREFIX/app/model"

echo "[3/5] building venv (no interpreter bundled) ..."
"$PYBIN" -m venv "$PREFIX/.venv"

echo "[4/5] OFFLINE pip install (--no-index) ..."
"$PREFIX/.venv/bin/pip" install --no-index --find-links="$PREFIX/wheels" \
    -r "$PREFIX/requirements.lock"

echo "[5/5] smoke test (offscreen Qt import + GUI selftest) ..."
# Use the BUNDLED Qt, not the box's system/Cadence Qt: EDA boxes put a conflicting libQt5Core.so.5
# on LD_LIBRARY_PATH -> "symbol _ZdaPvm, version Qt_5 not defined" at import. Prepend our wheel's
# Qt5/lib so it wins (python3.* glob is robust to the cp311 venv layout).
QTLIB="$(echo "$PREFIX"/.venv/lib/python3.*/site-packages/PyQt5/Qt5/lib)"
export LD_LIBRARY_PATH="$QTLIB${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
# SMOKE_REQUIRE_QT=1 (default): strict -- Qt MUST import (real red box has Virtuoso's xcb/libGL).
# Set 0 for a bare container rehearsal that lacks Qt's system .so (the offline pip install +
# numpy/scipy/matplotlib import is still fully proven; Qt is attempted but not required).
REQ_FLAG="--require-qt"; [ "${SMOKE_REQUIRE_QT:-1}" = "0" ] && REQ_FLAG=""
QT_QPA_PLATFORM=offscreen "$PREFIX/.venv/bin/python" \
    "$PREFIX/app/gui/ldo_modeler.py" --selftest $REQ_FLAG

# record req-hash for incremental update guard
REQ_HASH="$(grep -o '"requirements_hash"[^,]*' "$PREFIX/MANIFEST.deployed.json" \
            | sed 's/.*: *"//; s/"//')"
echo "{\"installed_utc\":\"$(date -u +%Y-%m-%dT%H:%M:%SZ)\",\"requirements_hash\":\"$REQ_HASH\",\"prefix\":\"$PREFIX\"}" \
    > "$PREFIX/INSTALL.json"

# install the launchers from the single source of truth (deploy/run_gui + deploy/update, shipped
# into app/deploy/) so the repo file and the deployed file never drift.
cp "$PREFIX/app/deploy/run_gui" "$PREFIX/run_gui"
cp "$PREFIX/app/deploy/update"  "$PREFIX/update"
chmod +x "$PREFIX/run_gui" "$PREFIX/update"

echo ""
echo "OK. Launch the GUI with:"
echo "   $PREFIX/run_gui          (uses the bundled Qt; needs a display / X11 / VNC)"
echo "Code update: upload ldo_modeler_incremental.tar.gz into $PREFIX, then run  $PREFIX/update"
echo "Outputs persist under $PREFIX/results"
