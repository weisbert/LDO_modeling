"""Rebuild a fit-ready reference npz from a pasted [7] GT DIGEST block.

The air-gap return channel: the red-zone report's section [7] carries log-resampled
GT curves as text. This parser turns that paste back into results/ref/<name>.npz so
the real part can be fitted/iterated LOCALLY (no npz crosses the gap).

Digest columns: f[Hz], |Z|[ohm], Zph[deg], PSRRatt[dB], Hph[deg], Sv[V/rtHz]
('-' = quantity not swept at that frequency). dc_loadreg is synthesized from the
per-corner LF Re(Zout) (good enough for fitting; the digest has no DC sweeps).

Usage:
    python harness/digest_import.py results/ref/myldo_digest.txt [--name myldo_digest]
                                    [--vref 1.05]
"""
import argparse
import pathlib
import re
import sys

import numpy as np

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "harness"))
REFDIR = ROOT / "results" / "ref"


def parse_digest(text):
    """-> (corners, dcblocks): corner -> ndarray[N,6] (nan for '-') in file order, plus
    optional '# dcblock <name>' 2-column DC curves (dc_loadreg/dc_linereg/dc_dropout --
    newer red-zone reports carry them so the replica's DC is real, not synthesized)."""
    corners, dcblocks = {}, {}
    cur = curdc = None
    for raw in text.splitlines():
        line = raw.strip()
        m = re.match(r"#\s*corner\s+(\S+)", line)
        if m:
            cur, curdc = m.group(1), None
            corners[cur] = []
            continue
        m = re.match(r"#\s*dcblock\s+(\S+)", line)
        if m:
            curdc, cur = m.group(1), None
            dcblocks[curdc] = []
            continue
        if not line or line.startswith("#"):
            continue
        toks = [t.strip() for t in line.split(",")]
        if curdc is not None and len(toks) >= 2:
            try:
                dcblocks[curdc].append([float(toks[0]), float(toks[1])])
            except ValueError:
                pass
            continue
        if cur is None or len(toks) < 6:
            continue
        try:
            row = [float("nan") if t == "-" else float(t) for t in toks[:6]]
        except ValueError:
            continue
        corners[cur].append(row)
    return ({il: np.array(rows, float) for il, rows in corners.items() if rows},
            {k: np.array(v, float) for k, v in dcblocks.items() if len(v) >= 2})


def check_sufficiency(corners, synth_dc=None):
    """Digest SUFFICIENCY screen: the air-gap digest is a lossy, log-resampled copy of
    the real GT -- a fit on an inadequate digest converges confidently to the wrong
    model and there is no held-out data on this side of the gap to catch it. Returns
    a list of (level, msg) where level is 'WARN' (fit quality at risk -- re-export a
    denser/wider digest) or 'INFO' (known structural limitation)."""
    out = []
    for il, a in corners.items():
        f = a[:, 0]
        span = np.log10(f[-1] / f[0]) if f[-1] > f[0] else 0.0
        ppd = (len(f) - 1) / span if span > 0 else float("inf")
        if ppd < 4.0:
            out.append(("WARN", f"corner {il}: only {ppd:.1f} pts/decade (<4) over "
                                f"{span:.1f} decades -- sharp features (resonance Q, "
                                f"notches) will be aliased; re-export a denser digest"))
        # resonance sampling: is the |Z| peak resolved above its half-power width?
        zmag = a[:, 1]
        ipk = int(np.argmax(zmag))
        if 0 < ipk < len(f) - 1:
            half = zmag[ipk] / np.sqrt(2.0)
            n_half = 1
            j = ipk - 1
            while j >= 0 and zmag[j] >= half:
                n_half += 1; j -= 1
            j = ipk + 1
            while j < len(f) and zmag[j] >= half:
                n_half += 1; j += 1
            if zmag[ipk] > 2.0 * np.median(zmag) and n_half < 3:
                out.append(("WARN", f"corner {il}: |Z| resonance at {f[ipk]:.3g}Hz has only "
                                    f"{n_half} point(s) above half-power -- peak height/Q "
                                    f"will be underestimated; densify the digest near the peak"))
        elif ipk in (0, len(f) - 1):
            out.append(("WARN", f"corner {il}: |Z| peaks at the {'low' if ipk == 0 else 'high'} "
                                f"band edge ({f[ipk]:.3g}Hz) -- the sweep may be truncated"))
        # noise LF coverage: flicker/RTN must actually be in the data to be fit
        nfin = np.isfinite(a[:, 5])
        if not nfin.any():
            out.append(("WARN", f"corner {il}: no noise (Sv) data in the digest -- the noise "
                                f"block will be fit to nothing"))
        elif f[nfin][0] > 1e3:
            out.append(("WARN", f"corner {il}: noise data starts at {f[nfin][0]:.3g}Hz (>1kHz) "
                                f"-- the flicker/LF tail is invisible to the fit"))
        # PSRR coverage
        pfin = np.isfinite(a[:, 3]) & np.isfinite(a[:, 4])
        if not pfin.any():
            out.append(("WARN", f"corner {il}: no PSRR data in the digest"))
        elif pfin.sum() < 0.5 * len(f):
            out.append(("INFO", f"corner {il}: PSRR sampled at only {int(pfin.sum())}/{len(f)} "
                                f"digest points"))
    if synth_dc is None or synth_dc == ["dc_loadreg", "dc_linereg", "dc_dropout"]:
        out.append(("INFO", "DC curves (loadreg/linereg/dropout) are SYNTHESIZED from LF Re(Zout) "
                            "-- the digest carries no DC sweeps; dc_dropout / slew_en=1 large-signal "
                            "behavior is a placeholder, do not trust it for a real part"))
    elif synth_dc:
        out.append(("INFO", f"DC curves partially real: {synth_dc} SYNTHESIZED (absent from the "
                            f"digest), the rest carried as '# dcblock' data"))
    else:
        out.append(("INFO", "all three DC curves (loadreg/linereg/dropout) carried as REAL "
                            "'# dcblock' data -- slew_en=1 dropout table and vdd line-reg "
                            "tracking are real on this replica"))
    return out


def build_ref(corners, name, vref=1.05, dcblocks=None):
    dcblocks = dcblocks or {}
    loads = list(corners.keys())
    ref = {"loads": np.array(loads),
           "meta_cout": np.array(np.nan), "meta_esr": np.array(np.nan),
           "spur_F": np.array([]), "spur_twin0": np.array(0.0),
           "spur_binhz": np.array(15625.0)}
    rdc = {}
    for il, a in corners.items():
        f = a[:, 0]
        Z = a[:, 1] * np.exp(1j * np.radians(a[:, 2]))
        ref[f"z_{il}"] = np.c_[f, Z.real, Z.imag]
        ok = np.isfinite(a[:, 3]) & np.isfinite(a[:, 4])
        H = 10.0 ** (-a[ok, 3] / 20.0) * np.exp(1j * np.radians(a[ok, 4]))
        ref[f"p_{il}"] = np.c_[f[ok], H.real, H.imag]
        nk = np.isfinite(a[:, 5])
        ref[f"noise_{il}"] = np.c_[f[nk], a[nk, 5]]
        rdc[il] = float(Z.real[0])              # LF Re(Zout) ~ DC output resistance
    # DC curves: REAL when the digest carries '# dcblock' sections (newer red-zone
    # reports); otherwise SYNTHESIZED placeholders -- linear load-reg from the mean
    # LF Re(Z); flat line-reg; dropout = load-reg extended.
    rmean = float(np.mean(list(rdc.values())))
    imax = max(float(il.replace("u", "e-6").replace("m", "e-3")) for il in loads)
    synth = []
    if "dc_loadreg" in dcblocks:
        ref["dc_loadreg"] = dcblocks["dc_loadreg"]
    else:
        idc = np.linspace(imax / 400, 4 * imax, 80)
        ref["dc_loadreg"] = np.c_[idc, vref - rmean * idc]
        synth.append("dc_loadreg")
    if "dc_linereg" in dcblocks:
        ref["dc_linereg"] = dcblocks["dc_linereg"]
    else:
        vdc = np.linspace(vref + 0.05, vref + 0.4, 40)
        ref["dc_linereg"] = np.c_[vdc, vref + 0 * vdc]
        synth.append("dc_linereg")
    if "dc_dropout" in dcblocks:
        ref["dc_dropout"] = dcblocks["dc_dropout"]
    else:
        iddc = np.linspace(imax / 400, 10 * imax, 80)
        ref["dc_dropout"] = np.c_[iddc, np.maximum(vref - rmean * iddc, 0.2 * vref)]
        synth.append("dc_dropout")
    out = REFDIR / f"{name}.npz"
    np.savez(out, **ref)
    return out, loads, synth


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("digest", help="text file containing the pasted [7] GT DIGEST block")
    ap.add_argument("--name", default=None, help="output ref name (default: file stem)")
    ap.add_argument("--vref", type=float, default=1.05)
    a = ap.parse_args()
    p = pathlib.Path(a.digest)
    corners, dcblocks = parse_digest(p.read_text(encoding="utf-8"))
    if not corners:
        sys.exit("no '# corner' blocks parsed -- is this a [7] GT DIGEST paste?")
    name = a.name or p.stem
    out, loads, synth_dc = build_ref(corners, name, vref=a.vref, dcblocks=dcblocks)
    print(f"wrote {out}   corners={loads} "
          f"({', '.join(str(len(corners[il])) + 'pts' for il in loads)})"
          + (f"   dcblocks={sorted(dcblocks)}" if dcblocks else ""))
    checks = check_sufficiency(corners, synth_dc=synth_dc)
    nwarn = sum(1 for lv, _ in checks if lv == "WARN")
    print(f"\nDIGEST SUFFICIENCY ({nwarn} warning(s)):")
    for lv, msg in checks:
        print(f"  {lv}: {msg}")


if __name__ == "__main__":
    main()
