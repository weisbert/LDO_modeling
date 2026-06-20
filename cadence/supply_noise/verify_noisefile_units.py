"""Empirically pin the UNIT of a Spectre vsource `noisefile` on local Spectre 18.1.

Drive a node directly with an ideal vsource carrying a FLAT noise file value V=1e-14,
probe the node's output noise. Spectre's noise `out` trace is amplitude density [V/rtHz]
(established by our other tests). So:
  out == 1e-7  (=sqrt(1e-14))  ->  the file value is POWER  [V^2/Hz]
  out == 1e-14                 ->  the file value is AMPLITUDE [V/rtHz]
"""
import pathlib
import sys

import numpy as np

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))           # cadence/
import spectre_run as sr                                                    # noqa: E402

FLAT = 1e-14

# flat noise file: two PWL points, same value -> constant vs freq
nf = HERE / "flat_nf.dat"
nf.write_text(f"10 {FLAT:e}\n1e9 {FLAT:e}\n")

scs = f"""// noisefile unit probe
simulator lang=spectre
Vn (n 0) vsource dc=0 noisefile="{nf.resolve()}"
Rp (n 0) resistor r=1
save n
nz (n 0) noise start=100 stop=1k dec=1
"""

d = sr.run(scs, "nf_units")
f = np.asarray(d["nz"]["freq"]).real
out = np.asarray(d["nz"]["out"]).real
print("freq:", f)
print("out :", out)
o = float(np.median(out))
print(f"\nflat file value V = {FLAT:e}")
print(f"median out        = {o:e} V/rtHz")
print(f"sqrt(V)           = {np.sqrt(FLAT):e}")
if abs(o - np.sqrt(FLAT)) / np.sqrt(FLAT) < 0.05:
    print("=> file value is POWER  [V^2/Hz]   (out == sqrt(file))")
elif abs(o - FLAT) / FLAT < 0.05:
    print("=> file value is AMPLITUDE [V/rtHz] (out == file)")
else:
    print("=> INCONCLUSIVE -- inspect numbers above")
