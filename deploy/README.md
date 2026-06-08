# Offline (airgap) deployment — LDO behavioral modeler GUI

Two-zone pipeline for installing the PyQt5 GUI modeler on an **airgapped red zone**
(CentOS7-class Linux, **glibc 2.17**, has **python3.11**, no network), using a **yellow zone**
(Windows with network) to fetch and audit the Linux wheels. The GUI is analytic-only (no
simulator), so the runtime deps are just numpy / scipy / matplotlib / PyQt5 (no scikit-rf).

```
 yellow (Windows, net)                         red (CentOS7, glibc 2.17, no net)
 ─────────────────────                         ─────────────────────────────────
 package.py full        ──tar.gz (145 MB)──▶   bootstrap.sh  →  <your-folder>/{.venv,wheels,app,results}
   • cross-download cp311 manylinux2014 wheels    • python3.11 -m venv
   • AUDIT tags  (reject > glibc 2.17)             • pip install --no-index --find-links=wheels
   • freeze requirements.lock                      • smoke: gui --selftest --require-qt
   • MANIFEST.json (+ sha256)
 package.py incremental ──tar.gz (77 KB)───▶   update.sh    →  refresh app/ only (reuse .venv)
   • app/ source only, carries req-hash           • GUARD: abort if req-hash ≠ deployed
```

## Yellow zone (Windows, has network)

```powershell
# FULL bundle: download + audit + lock + tar
.\.venv\Scripts\python.exe deploy\package.py full --out dist\
#   -> dist\ldo_modeler_full.tar.gz (+ .sha256, + MANIFEST.full.json)
#   The build FAILS if any wheel needs glibc > 2.17 (prints exactly what to downpin).

# INCREMENTAL bundle (code-only, after a full build exists):
.\.venv\Scripts\python.exe deploy\package.py incremental --out dist\
#   -> dist\ldo_modeler_incremental.tar.gz  (no wheels; reuses the red .venv)
```

Audit any wheel directory standalone: `python deploy\audit_wheels.py <dir> --max-glibc 2.17`.

**Pins** live in `deploy/requirements-gui.txt` (direct deps); `package.py` resolves the transitive
set, audits every wheel, and freezes the exact versions into `requirements.lock` inside the bundle.
Verified glibc-2.17 set (cp311/x86_64): numpy 1.26.4, scipy 1.15.3, matplotlib 3.9.4, pillow 12.2.0,
contourpy 1.3.2, fonttools 4.63, kiwisolver 1.5, PyQt5 5.15.10, **PyQt5-Qt5 5.15.2**, PyQt5-sip 12.15.

## Red zone (CentOS7, glibc 2.17, airgapped)

Keep everything under one folder you create. The install must NOT go to `/opt` on a shared box
(no write permission), and **PREFIX must be an absolute path** or bootstrap's `app/results` symlink
breaks. Don't introduce a `ROOT=`-style variable (EDA shells often already export `$ROOT`); use the
shell's built-in `$PWD` — you never assign it, so nothing in your environment is touched. Run from
the folder holding the tarball:

```bash
cd /path/to/your-folder                      # the folder you created; tarball is here ($PWD = it)
sed 's/\r$//' ldo_modeler_full.tar.gz.sha256 | sha256sum -c   # integrity (tolerates old CRLF sidecar)

mkdir -p bundle && tar xzf ldo_modeler_full.tar.gz -C bundle
sed -i 's/\r$//' bundle/requirements.lock    # no-op on new (LF) bundles; rescues old Windows-built ones
bash bundle/bootstrap.sh "$PWD"              # PREFIX = this folder → .venv/app/results land directly here (bash = no +x)
.venv/bin/python app/gui/ldo_modeler.py      # launch GUI (from this folder)

# later code-only update (same install dir):
mkdir -p bundle_incr && tar xzf ldo_modeler_incremental.tar.gz -C bundle_incr
bash bundle_incr/update.sh "$PWD"            # refresh app/, keep .venv/wheels/results
```

- `results/` persists across updates (never overwritten); `.venv/` + `wheels/` are built once
  by `bootstrap.sh` and reused by every `update.sh`.
- `update.sh` aborts if the bundle's `requirements_hash` ≠ the deployed venv's — a deps change
  demands a fresh FULL deploy.
- Runtime libs: PyQt5-Qt5 needs xcb/fontconfig/libGL. The red box runs Virtuoso (a Qt app) so
  these are present; if the xcb plugin fails, set `QT_QPA_PLATFORM_PLUGIN_PATH` or ship the 1–2
  missing `.so`. The smoke test uses `QT_QPA_PLATFORM=offscreen` to avoid needing a display.

## Rehearse before the airgap (recommended)

```bash
# at home, with Docker, prove the offline install on a real glibc-2.17 image:
deploy/dryrun_manylinux2014.sh dist/ldo_modeler_full.tar.gz
```

This extracts the bundle into `quay.io/pypa/manylinux2014_x86_64` (glibc 2.17) and runs
`bootstrap.sh` with the network disabled — the same `--no-index` install the red box does.
