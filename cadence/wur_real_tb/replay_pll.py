#!/usr/bin/env python3
"""Local current-replay validation harness for the PLL LDO rail model.

THE validated local loop (proven faithful to box Spectre to 1.43mV with the 2000-pt
current): feed a measured load current as a PWL isource into the behavioral .va, run
LOCAL Spectre, compare vout(t) to the real-silicon VDD. Use this to iterate the model
on the desk with NO box runs.

Usage:
  python3 replay_pll.py <model.va>                 # replay real current, compare to real_V
  python3 replay_pll.py <model.va> --dc 0.5e-3     # DC load-reg point (must stay ~0.8V)
  python3 replay_pll.py <model.va> --step          # clean 0.5->1mA step (must NOT ring past rails)

Data (in this dir):
  real_pmu_iload_PLL_2000.txt  = real silicon PLL load current, 2000pt, 0-200ns (replay INPUT)
  real_V_VDD0P8_PLL.txt        = real silicon VDD (the TARGET to reproduce), 0-300ns
  model_V_VDD0P8_PLL.txt       = box model output (for loop self-validation, same current)
"""
import argparse, os, sys
import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.abspath(os.path.join(HERE, "..", ".."))
sys.path.insert(0, os.path.join(REPO, "cadence"))
import spectre_run  # noqa: E402

ICUR = os.path.join(HERE, "real_pmu_iload_PLL_2000.txt")
VSIL = os.path.join(HERE, "real_V_VDD0P8_PLL.txt")


def _read(f):
    r = []
    for ln in open(f):
        s = ln.strip()
        if not s or s[0] in '#"':
            continue
        p = s.replace(",", " ").split()
        try:
            r.append((float(p[0]), float(p[1])))
        except ValueError:
            pass
    a = np.array(r)
    return a[:, 0], a[:, 1]


def _spectre(va, isrc_card, tstop, step="1e-11"):
    va = os.path.abspath(va)   # Spectre runs in its own wd -> ahdl_include needs an ABS path
    scs = ("simulator lang=spectre\n"
           f'ahdl_include "{va}"\n'
           "Xd (AVDD1P0 vout 0) ldo_pll\n"
           "Vs (AVDD1P0 0) vsource dc=1.0\n"
           "Cd (vout 0) capacitor c=20e-12\n"
           f"{isrc_card}\n"
           f"tr tran stop={tstop} step={step} maxstep={step}\n")
    o = spectre_run.run(scs, "replay")["tr"]
    tk = next(k for k in o if k.lower() in ("time", "tr"))
    vk = next(k for k in o if k.lower() == "vout")
    return np.asarray(o[tk], float), np.asarray(o[vk], float).real


def replay(va):
    t, I = _read(ICUR)
    wave = " ".join(f"{a:.6e} {b:.6e}" for a, b in zip(t, I))
    tr, vr = _spectre(va, f"Il (vout 0) isource type=pwl wave=[{wave}]", "2.0001e-7")
    ts, vs = _read(VSIL)
    m = ts <= 2.0e-7
    tb, vb = ts[m], vs[m]
    vi = np.interp(tb, tr, vr) * 1e3
    d = vi - vb * 1e3
    print(f"  dip   model={vi.min():7.2f}mV @{tb[np.argmin(vi)]*1e9:5.1f}ns   silicon={vb.min()*1e3:7.2f} @{tb[np.argmin(vb)]*1e9:5.1f}ns")
    print(f"  ss    model={vi[int(len(vi)*0.8):].mean():7.2f}mV          silicon={vb[int(len(vb)*0.8):].mean()*1e3:7.2f}")
    print(f"  RMS  ={np.sqrt(np.mean(d**2)):6.2f}mV   max|d|={np.max(np.abs(d)):6.2f}mV")
    for lo, hi in [(0, 30), (30, 125), (125, 200)]:
        w = (tb >= lo*1e-9) & (tb <= hi*1e-9)
        dd = vi[w] - vb[w]*1e3
        print(f"    window {lo:3d}-{hi:3d}ns: RMS={np.sqrt(np.mean(dd**2)):6.2f}mV  mean={dd.mean():+6.2f}mV")


def dc(va, iload):
    tr, vr = _spectre(va, f"Il (vout 0) isource dc={iload:.6e}", "3e-6", "2e-9")
    print(f"  DC @ {iload*1e3:.3f}mA = {vr[-1]*1e3:.3f}mV   (MUST stay ~800mV)")


def step(va):
    tr, vr = _spectre(va, "Il (vout 0) isource type=pwl wave=[0 0.5m 19.9n 0.5m 20n 1m 3u 1m]", "3e-6", "1e-10")
    print(f"  clean 0.5->1mA step:  min={vr.min()*1e3:.1f}mV  max={vr.max()*1e3:.1f}mV  final={vr[-1]*1e3:.2f}mV")
    print("  (healthy LDO: monotonic dip+recover, NO swing past 0..1000mV, NO overshoot)")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("va")
    ap.add_argument("--dc", type=float, default=None)
    ap.add_argument("--step", action="store_true")
    a = ap.parse_args()
    print(f"model: {a.va}")
    if a.dc is not None:
        dc(a.va, a.dc)
    elif a.step:
        step(a.va)
    else:
        replay(a.va)
