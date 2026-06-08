#!/usr/bin/env bash
# Rehearse the airgap install on a REAL glibc-2.17 image, with the NETWORK DISABLED -- proves
# the offline `pip install --no-index` works on the red target before you ever touch the airgap.
# Requires Docker (run at home / on the yellow zone, NOT the red box).
#
#   deploy/dryrun_manylinux2014.sh dist/ldo_modeler_full.tar.gz
#
# manylinux2014 is a BARE image (no Virtuoso) so it lacks Qt's xcb/libGL system .so. We therefore
# run the smoke with SMOKE_REQUIRE_QT=0: the offline install + numpy/scipy/matplotlib import +
# harness fit/predict/emit are fully proven; Qt is attempted but not required here (it IS required
# by bootstrap.sh's default on the real red box, which has the Qt runtime libs).
set -euo pipefail

BUNDLE="${1:?usage: dryrun_manylinux2014.sh <full-bundle.tar.gz>}"
IMAGE="${IMAGE:-quay.io/pypa/manylinux2014_x86_64}"
BUNDLE_ABS="$(cd "$(dirname "$BUNDLE")" && pwd)/$(basename "$BUNDLE")"
command -v docker >/dev/null 2>&1 || { echo "ERROR: docker not found"; exit 1; }

echo "== airgap dry-run on $IMAGE (network DISABLED) =="
docker run --rm --network none -v "$BUNDLE_ABS":/bundle.tar.gz:ro "$IMAGE" bash -euxc '
  export PATH=/opt/python/cp311-cp311/bin:$PATH
  command -v python3.11 >/dev/null 2>&1 || ln -sf /opt/python/cp311-cp311/bin/python3.11 /usr/local/bin/python3.11
  python3.11 --version
  ldd --version | head -1                     # confirm the image glibc (expect 2.17)
  mkdir -p /tmp/b && tar xzf /bundle.tar.gz -C /tmp/b && cd /tmp/b
  SMOKE_REQUIRE_QT=0 ./bootstrap.sh /opt/ldo_modeler
  # extra proof: the heavy native wheels import on this glibc-2.17 box
  /opt/ldo_modeler/.venv/bin/python -c "import numpy,scipy,matplotlib; print(\"imports OK\", numpy.__version__, scipy.__version__, matplotlib.__version__)"
'
echo ""
echo "DRY-RUN PASSED: offline --no-index install + native-wheel import succeeded on glibc 2.17."
