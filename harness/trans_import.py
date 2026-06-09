"""Cadence multitone-transient importer  (productionization piece B).

Reads the exported transient waveform(s) from ONE multitone trans-ID run (the stimulus
emitted by trans_id.emit_stim_va) + the sidecar plan.json, recovers Zout(f)/PSRR(f) per
band via trans_id.extract_zp_from_wave, and writes z_<corner>.csv / p_<corner>.csv (plus
z_<nom>_hf / p_<nom>_hf for the nominal corner) in the freq,real,imag layout that
cadence/import_cadence.py consumes -> drop straight into the GUI Import tab. Noise PSD
still comes from a separate .noise (a deterministic .tran carries no device noise).

The validated recipe is BAND-SPLIT: one transient (hence one waveform) per band. The
importer loops the plan's bands, extracts each on its own coherent window, and concatenates
sorted by freq -- the exact mirror of trans_id.measure_zp, but reading files instead of
running ngspice. Per-bin RATIOS (Vout/Vin, Vout/Iinj) recover magnitude AND phase; the
supply tones are reconstructed analytically when v(vin) is not exported (an ideal source).

  python harness/trans_import.py --smoke        # self-contained round-trip vs the AC ref
"""
import argparse
import json
import pathlib
import numpy as np

import trans_id


# ------------------------------------------------------------------------------- io
def load_plan(plan_path):
    """Load the sidecar plan.json emitted by trans_id.emit_stim_va."""
    return json.loads(pathlib.Path(plan_path).read_text())


def _read_table(path):
    """Tolerant CSV/whitespace reader -> float ndarray[rows,cols]; skips header / comment /
    unit rows (mirrors cadence/import_cadence._read_table so real Cadence exports parse)."""
    rows = []
    for raw in pathlib.Path(path).read_text().splitlines():
        line = raw.strip()
        if not line or line[0] in "#*;":
            continue
        toks = [t for t in line.replace(",", " ").split() if t]
        try:
            rows.append([float(t) for t in toks])
        except ValueError:
            continue                                   # header / unit row
    if not rows:
        raise ValueError(f"{path}: no numeric rows parsed")
    w = max(len(r) for r in rows)
    return np.array([r + [np.nan] * (w - len(r)) for r in rows], float)


def _split_wave(spec):
    """Resolve ONE band's waveform spec -> (t, vout, vin|None).
      spec = a single path: 2-col [time, vout] (vin reconstructed) OR
                            3-col [time, vin, vout] (the fixture's 'export v(vin) & v(vout)').
      spec = (vout_path, vin_path): two single-signal files.
      spec = an ndarray already loaded ([t,vout] or [t,vin,vout]) -- in-memory shortcut.
    The 3-col order matches ngspice `wrdata out v(vin) v(vout)` (single time scale)."""
    if isinstance(spec, np.ndarray):
        arr = spec
    elif isinstance(spec, (tuple, list)) and len(spec) == 2 \
            and isinstance(spec[0], (str, pathlib.Path)):
        a = _read_table(spec[0]); b = _read_table(spec[1])
        return a[:, 0], a[:, 1], b[:, 1]
    else:
        arr = _read_table(spec)
    if arr.shape[1] >= 3:
        return arr[:, 0], arr[:, 2], arr[:, 1]         # [t, vin, vout]
    return arr[:, 0], arr[:, 1], None                  # [t, vout] -> reconstruct vin


# --------------------------------------------------------------------- extraction
def zp_from_files(band_waveforms, plan, corner=None):
    """band_waveforms aligned to plan['bands'] (one per band). Returns (z, p, info) with
    z=np.c_[freq,Re,Im], p=np.c_[freq,Re,Im] (sorted by freq) -- same layout as the AC ref's
    z_{il}/p_{il}. info carries vout_dc (settled window mean), counts and the worst leak."""
    bands = plan["bands"]
    if len(band_waveforms) != len(bands):
        raise ValueError(f"{len(band_waveforms)} waveform(s) but plan has {len(bands)} band(s)")
    ib = float(plan["ib"]); va = float(plan["va"])
    zf, zr, zi, pf, pr, pi, per = [], [], [], [], [], [], []
    vout_dc = None
    for bw, spec in zip(bands, band_waveforms):
        t, vout, vin = _split_wave(spec)
        d = trans_id.extract_zp_from_wave(t, vout, bw, ib=ib, va=va, vin=vin)
        zf.append(d["fz"]); zr.append(d["Z"].real); zi.append(d["Z"].imag)
        pf.append(d["fp"]); pr.append(d["P"].real); pi.append(d["P"].imag)
        if vout_dc is None:                            # lowest band first (plan is sorted)
            vout_dc = d["vout_dc"]
        per.append(dict(f_lo=bw["f_lo"], f_hi=bw["f_hi"], N=int(bw["N"]),
                        leak_db=d["leak_db"]))

    def stack(fs, rs, iss):
        f = np.concatenate(fs); R = np.concatenate(rs); I = np.concatenate(iss)
        o = np.argsort(f)
        return np.c_[f[o], R[o], I[o]]

    z, p = stack(zf, zr, zi), stack(pf, pr, pi)
    info = dict(corner=corner, vout_dc=vout_dc, n_z=z.shape[0], n_p=p.shape[0],
                worst_leak_db=float(max(x["leak_db"] for x in per)), bands=per)
    return z, p, info


# ------------------------------------------------------------------------- writers
def _save_zp(path, arr):
    pathlib.Path(path).parent.mkdir(parents=True, exist_ok=True)
    # freq,real,imag header -> import_cadence auto-detects fmt="reim" (unambiguous; never partial)
    np.savetxt(path, arr, delimiter=",", header="freq,real,imag", comments="", fmt="%.10e")


def write_corner_csv(z, p, corner, outdir, nominal=None):
    """Write z_<corner>.csv / p_<corner>.csv (+ z_<nom>_hf / p_<nom>_hf for the nominal corner,
    so fit_model can auto-extract Cout/ESR from the HF tail). Returns the (kind,corner)->path
    files dict that cadence/import_cadence.assemble() / the GUI Import path consume."""
    outdir = pathlib.Path(outdir); outdir.mkdir(parents=True, exist_ok=True)
    files = {}
    zp, pp = outdir / f"z_{corner}.csv", outdir / f"p_{corner}.csv"
    _save_zp(zp, z); _save_zp(pp, p)
    files[("z", corner)] = str(zp); files[("p", corner)] = str(pp)
    if nominal is not None and str(corner) == str(nominal):
        zh, ph = outdir / f"z_{corner}_hf.csv", outdir / f"p_{corner}_hf.csv"
        _save_zp(zh, z); _save_zp(ph, p)
        files[("z_hf", corner)] = str(zh); files[("p_hf", corner)] = str(ph)
    return files


def import_run(plan, corner_waveforms, outdir, nominal=None):
    """High-level: corner_waveforms = {corner: [band0_wave, band1_wave, ...]} ->
    (files, info). `files` is the (kind,corner)->path dict for import_cadence.assemble();
    `info` is per-corner extraction diagnostics. Noise/DC are NOT produced here (separate)."""
    files, info = {}, {}
    for corner, bws in corner_waveforms.items():
        z, p, inf = zp_from_files(bws, plan, corner=corner)
        files.update(write_corner_csv(z, p, corner, outdir, nominal=nominal))
        info[corner] = inf
    return files, info


# ----------------------------------------------------------------- folder matcher (GUI)
def match_wave_dir(folder, plan, loads):
    """Scan a folder for per-(corner, band) exported waveforms -> {corner: [band0, band1, ...]}
    (ordered by plan band index), for the GUI 'import one multitone trans' gesture. Accepts
    <corner>_b<i>.<ext> / wave_<corner>_b<i>.<ext> / vout_<corner>_b<i>.<ext>, ext in
    csv/txt/dat/tr0/prn. Only corners with a COMPLETE band set are returned."""
    folder = pathlib.Path(folder)
    nb = len(plan["bands"])
    exts = (".csv", ".txt", ".dat", ".tr0", ".prn")

    def find(corner, bi):
        for stem in (f"{corner}_b{bi}", f"wave_{corner}_b{bi}", f"vout_{corner}_b{bi}"):
            for e in exts:
                p = folder / f"{stem}{e}"
                if p.exists():
                    return str(p)
        return None

    out = {}
    for il in loads:
        ws = [find(str(il), bi) for bi in range(nb)]
        if all(ws):
            out[str(il)] = ws
    return out


# --------------------------------------------------------------------------- smoke
def _cinterp(f_to, f_from, Z):
    o = np.argsort(f_from); f_from, Z = f_from[o], Z[o]
    mag = np.exp(np.interp(np.log(f_to), np.log(f_from), np.log(np.abs(Z) + 1e-30)))
    ph = np.interp(np.log(f_to), np.log(f_from), np.unwrap(np.angle(Z)))
    return mag * np.exp(1j * ph)


def _smoke():
    """Self-contained: synthesize a band's vout/vin waveform from the AC ref (the inverse of
    _spectrum) and assert zp_from_files recovers it to the validated tolerance -- NO simulator,
    NO compiled .va; this proves the importer's extraction math inverts the construction."""
    import ng
    ref = np.load(ng.ROOT / "results" / "ref" / "base.npz", allow_pickle=True)
    gz, gp = ref["z_121u"], ref["p_121u"]
    Zac = lambda f: _cinterp(f, gz[:, 0], gz[:, 1] + 1j * gz[:, 2])
    Hac = lambda f: _cinterp(f, gp[:, 0], gp[:, 1] + 1j * gp[:, 2])

    pl = trans_id.plan_band(f_lo=1e5, f_hi=1e7, n_per_dec=12, ppp=12)
    N = pl["N"]; tg = pl["t0"] + pl["dt"] * np.arange(N)
    va, ib, VDD = trans_id.VA_DEFAULT, trans_id.IB_DEFAULT, trans_id.VDD
    Zt = Zac(pl["fb"]); Ht = Hac(pl["fa"])
    vin = np.full(N, VDD)
    for f in pl["fa"]:
        vin = vin + va * np.sin(2 * np.pi * float(f) * tg)
    vout = np.full(N, 1.0)                              # arbitrary DC (window mean removed)
    for f, Z in zip(pl["fb"], Zt):                      # current-tone response: |Z|*ib at fb
        vout = vout + ib * np.abs(Z) * np.sin(2 * np.pi * float(f) * tg + np.angle(Z))
    for f, H in zip(pl["fa"], Ht):                      # supply-tone response: |H|*va at fa
        vout = vout + va * np.abs(H) * np.sin(2 * np.pi * float(f) * tg + np.angle(H))

    # plan.json-style single-band plan (extract reads N/t0/dt/fa/ba/fb/bb)
    bj = dict(index=0, f_lo=pl["f_lo"], f_hi=pl["f_hi"], N=int(N), dt=pl["dt"], t0=pl["t0"],
              fa=[float(x) for x in pl["fa"]], ba=[int(b) for b in pl["ba"]],
              fb=[float(x) for x in pl["fb"]], bb=[int(b) for b in pl["bb"]])
    plan = dict(VDD=VDD, va=va, ib=ib, iload=0.0, bands=[bj])

    wave = np.c_[tg, vin, vout]                         # 3-col [t, vin, vout]
    wpath = ng.ROOT / "work" / "trans_import_smoke.dat"
    wpath.parent.mkdir(parents=True, exist_ok=True)
    np.savetxt(wpath, wave)

    for label, specs in (("exported-vin", [str(wpath)]),
                         ("reconstructed-vin", [np.c_[tg, vout]])):
        if label == "reconstructed-vin":               # write a 2-col file -> vin reconstructed
            wp2 = ng.ROOT / "work" / "trans_import_smoke_vout.dat"
            np.savetxt(wp2, np.c_[tg, vout]); specs = [str(wp2)]
        z, p, info = zp_from_files(specs, plan, corner="121u")
        ez = 20 * np.log10(np.abs(z[:, 1] + 1j * z[:, 2]) / np.abs(Zac(z[:, 0])))
        ep = 20 * np.log10(np.abs(p[:, 1] + 1j * p[:, 2]) / np.abs(Hac(p[:, 0])))
        ezp = np.degrees(np.angle((z[:, 1] + 1j * z[:, 2]) / Zac(z[:, 0])))
        epp = np.degrees(np.angle((p[:, 1] + 1j * p[:, 2]) / Hac(p[:, 0])))
        print(f"[{label:18}] Zout |err| max {np.max(np.abs(ez)):.4f} dB / {np.max(np.abs(ezp)):.3f} deg "
              f"| PSRR |err| max {np.max(np.abs(ep)):.4f} dB / {np.max(np.abs(epp)):.3f} deg "
              f"| n(z,p)={info['n_z']},{info['n_p']} leak={info['worst_leak_db']:.0f}dB")
        assert np.max(np.abs(ez)) < 0.45, "Zout recovery worse than validated tolerance"
        assert np.max(np.abs(ep)) < 0.45, "PSRR recovery worse than validated tolerance"
    print("trans_import smoke OK")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--smoke", action="store_true", help="round-trip smoke vs the AC ref (default action)")
    ap.parse_args()
    _smoke()                                   # the module's only CLI action
