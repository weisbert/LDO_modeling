"""Convert Cadence-exported characterization results into results/ref/<name>.npz,
EXACTLY per CADENCE_EXTRACTION.md, so `harness/fit_model.py --variant <name>` consumes
them unchanged. Two input modes:

  * PSF tree  (ADE/OCEAN `-format psfascii`, or our spectre runs) -> from_psf
  * CSV       (ADE manual "Export to CSV" — the first-class MANUAL-TB fallback) -> from_csv

The automated spectre path (`extract_ref.py`) writes the npz directly; THIS module is the
firewall for the ADE/manual path (Phase 4 in-situ, or a hand-built TB). `assemble()` is the
single contract-schema writer both paths agree on.

CSV layout (one file per quantity per corner; f in Hz; the harness interpolates the grid):
    z_<il>.csv      : f, Re(Z), Im(Z)      (Z = V(vout)/Iac, 1 A AC into vout, ideal vin)
    p_<il>.csv      : f, Re(H), Im(H)      (H = V(vout)/V(vin), 1 V AC on vin)   COMPLEX, not dB
    noise_<il>.csv  : f, Sv[V/sqrtHz]      (output noise PSD; sqrt if the tool gives V^2/Hz)
    z_121u_hf.csv / p_121u_hf.csv : nominal corner extended to 500 MHz
  optional: trans_*_<il>.csv (t,V), dc_loadreg.csv / dc_linereg.csv / dc_dropout.csv (x,V)
"""
import argparse
import pathlib
import numpy as np
import psf

ROOT = pathlib.Path(__file__).resolve().parents[1]
REFDIR = ROOT / "results" / "ref"


# ---------- contract schema writer (the single source both paths use) ----------
def assemble(loads, per, meta=None, hf=None, dc=None, trans=None, out=None):
    """Build (and optionally save) the contract npz dict.
    per   = {il: {"z":(f,Zc), "p":(f,Hc), "noise":(f,Sv)}}   complex Zc/Hc, real Sv
    hf    = {"z":(f,Zc), "p":(f,Hc)}   nominal corner to 500 MHz (z_121u_hf/p_121u_hf)
    dc    = {"dc_loadreg":(x,V), "dc_linereg":(x,V), "dc_dropout":(x,V)}
    trans = {"trans_lin_<il>":(t,V), "trans_big_121u":(t,V), "trans_slew_121u":(t,V)}
    meta  = {"cout":.., "esr":..}
    """
    ref = {"loads": np.array(list(loads))}
    m = meta or {}
    ref["meta_cout"] = m.get("cout", np.nan)
    ref["meta_esr"] = m.get("esr", np.nan)
    for il, q in per.items():
        fz, Z = q["z"]; ref[f"z_{il}"] = np.c_[fz, np.real(Z), np.imag(Z)]
        fp, H = q["p"]; ref[f"p_{il}"] = np.c_[fp, np.real(H), np.imag(H)]
        fn, S = q["noise"]; ref[f"noise_{il}"] = np.c_[fn, np.asarray(S, float)]
    if hf:
        fz, Z = hf["z"]; ref["z_121u_hf"] = np.c_[fz, np.real(Z), np.imag(Z)]
        fp, H = hf["p"]; ref["p_121u_hf"] = np.c_[fp, np.real(H), np.imag(H)]
    for k, (x, y) in (dc or {}).items():
        ref[k] = np.c_[x, y]
    for k, (t, y) in (trans or {}).items():
        ref[k] = np.c_[t, y]
    # intrinsic spurs: empty unless the caller supplies them (LDOs have none)
    ref.setdefault("spur_F", np.array([]))
    ref.setdefault("spur_twin0", np.array(24e-6))
    ref.setdefault("spur_binhz", np.array(62500.0))
    for il in loads:
        ref.setdefault(f"spurs_{il}", np.empty((0, 3)))
    if out:
        REFDIR.mkdir(parents=True, exist_ok=True)
        np.savez(out, **ref)
        print(f"saved {out}  ({len(ref)} arrays)")
    return ref


# ---------- PSF input (ADE/OCEAN psfascii, or our spectre raw dirs) ----------
def cplx_from_psf(path, node):
    d = psf.read_psf(path)
    return np.asarray(d[d["_sweep"]]).real, np.asarray(d[node])


def real_from_psf(path, node):
    d = psf.read_psf(path)
    return np.asarray(d[d["_sweep"]]).real, np.asarray(d[node]).real


def from_psf(root, loads, names, out=None, meta=None, out_node="vout", sup_node="vin"):
    """Assemble from a PSF tree. `names(il, kind)` -> psf file path for kind in
    {"z","p","noise"}; PSRR divides out_node by sup_node, noise reads the 'out' trace."""
    root = pathlib.Path(root)
    per = {}
    for il in loads:
        fz, Z = cplx_from_psf(root / names(il, "z"), out_node)
        fp, Vo = cplx_from_psf(root / names(il, "p"), out_node)
        _, Vs = cplx_from_psf(root / names(il, "p"), sup_node)
        fn, Sv = real_from_psf(root / names(il, "noise"), "out")
        per[il] = {"z": (fz, Z), "p": (fp, Vo / Vs), "noise": (fn, Sv)}
    return assemble(loads, per, meta=meta, out=out)


# ---------- CSV input (ADE manual export — first-class manual-TB fallback) ----------
def _csv(path):
    a = np.loadtxt(path, delimiter=",", comments="#")
    return np.atleast_2d(a)


def from_csv(root, loads, out=None, meta=None):
    """Assemble from the documented CSV layout (see module docstring)."""
    root = pathlib.Path(root)
    per = {}
    for il in loads:
        z = _csv(root / f"z_{il}.csv"); p = _csv(root / f"p_{il}.csv"); n = _csv(root / f"noise_{il}.csv")
        per[il] = {"z": (z[:, 0], z[:, 1] + 1j * z[:, 2]),
                   "p": (p[:, 0], p[:, 1] + 1j * p[:, 2]),
                   "noise": (n[:, 0], n[:, 1])}
    hf = None
    if (root / "z_121u_hf.csv").exists():
        zh = _csv(root / "z_121u_hf.csv"); ph = _csv(root / "p_121u_hf.csv")
        hf = {"z": (zh[:, 0], zh[:, 1] + 1j * zh[:, 2]), "p": (ph[:, 0], ph[:, 1] + 1j * ph[:, 2])}
    dc = {}
    for k in ("dc_loadreg", "dc_linereg", "dc_dropout"):
        if (root / f"{k}.csv").exists():
            a = _csv(root / f"{k}.csv"); dc[k] = (a[:, 0], a[:, 1])
    return assemble(loads, per, meta=meta, hf=hf, dc=dc or None, out=out)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("mode", choices=["csv", "psf"])
    ap.add_argument("root", help="directory of CSV files or PSF tree")
    ap.add_argument("--name", required=True, help="-> results/ref/<name>.npz")
    ap.add_argument("--loads", default="20u,121u,250u")
    ap.add_argument("--cout", type=float, default=float("nan"))
    ap.add_argument("--esr", type=float, default=float("nan"))
    a = ap.parse_args()
    loads = a.loads.split(",")
    meta = {"cout": a.cout, "esr": a.esr}
    out = REFDIR / f"{a.name}.npz"
    if a.mode == "csv":
        from_csv(a.root, loads, out=out, meta=meta)
    else:
        from_psf(a.root, loads, lambda il, k: f"{k}_{il}.ac" if k != "noise" else f"noise_{il}.noise",
                 out=out, meta=meta)
