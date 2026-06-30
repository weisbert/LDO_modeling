#!/usr/bin/env python3
"""Analysis of the PVT grid (loads results.json, no re-sim):
 (1) OPTIMISM table — a model shipped as TT vs the real per-corner silicon.
 (2) LOCAL vs EXTREME temperature interpolation — is T the 'continuous exception'?
"""
import json
WD = "/tmp/claude-1000/-home-yusheng-cadence-work-Test-workarea-LDO-modeling/95e8958d-7617-4337-a59f-6e96a1e43594/scratchpad/pvt_exp"
R = json.load(open(f"{WD}/results.json"))
P = {r["id"].split("_")[1]:r for r in R["process"]}
T = {r["temp"]:r for r in R["temp"]}
V = {r["vin"]:r for r in R["volt"]}
TTref = P["TT"]                 # the single corner the model would be fit at

# ---------- (1) OPTIMISM: ship-TT vs real-corner ----------
print("="*100)
print("OPTIMISM — a single-corner (TT) model emits TTref at EVERY declared corner.")
print("           Below: what TT claims vs the real per-corner silicon. (unsafe = optimistic)")
print("="*100)
stressed = [("SS @27C/1.05V", P["SS"]), ("HOT 125C", T[125]), ("COLD -40C", T[-40]),
            ("LOW supply 0.95V", V[0.95]), ("HIGH supply 1.30V", V[1.30])]
print(f"{'corner':<20}{'I-ceil claim/real':<22}{'PSRRworst claim/real':<24}{'dip claim/real':<20}{'Zpk claim/real'}")
print("-"*100)
for name, c in stressed:
    ic = f"{TTref['Iceil']*1e3:.0f}/{c['Iceil']*1e3:.0f}mA ({TTref['Iceil']/c['Iceil']:.1f}x)"
    pw = f"{TTref['Pworst']:.1f}/{c['Pworst']:.1f}dB ({TTref['Pworst']-c['Pworst']:+.0f})"
    dp = f"{TTref['dip_mV']:.0f}/{c['dip_mV']:.0f}mV ({(TTref['dip_mV']-c['dip_mV'])/c['dip_mV']*100:+.0f}%)"
    zp = f"{TTref['Zpk']:.0f}/{c['Zpk']:.0f}ohm ({TTref['Zpk']/c['Zpk']:.2f}x)"
    print(f"{name:<20}{ic:<22}{pw:<24}{dp:<20}{zp}")
print("="*100)
print("READ: I-ceil 'x'>1 = model claims MORE current capability than exists (overload risk).")
print("      PSRRworst (+) = model claims MORE supply rejection than exists.")
print("      dip (-) = model UNDER-predicts the droop (says rail holds up better than it does).")

# ---------- (2) temperature: LOCAL (neighbor) vs EXTREME interpolation ----------
def interp(a, b, xa, xb, x):  # linear
    return a + (b-a)*(x-xa)/(xb-xa)

temps = sorted(T)
keys = [("Zlf","ZoutLF"),("Zpk","Zpk"),("Pworst","PSRRworst"),("Iceil","Iceil"),("dip_mV","dip")]
print("\n"+"="*100)
print("TEMPERATURE INTERPOLATABILITY — is T the 'continuous exception' route-A allows?")
print("  LOCAL: predict each interior T from its 2 NEAREST neighbors (dense sampling).")
print("  EXTREME: predict the same T from the -40 & 125 endpoints only (2-pt across the span).")
print("="*100)
print(f"{'observable':<12}{'LOCAL mean|err|':<18}{'EXTREME mean|err|':<18}  interpretation")
print("-"*100)
for k,name in keys:
    loc, ext = [], []
    for i in range(1,len(temps)-1):
        x = temps[i]
        pl = interp(T[temps[i-1]][k], T[temps[i+1]][k], temps[i-1], temps[i+1], x)
        pe = interp(T[temps[0]][k],  T[temps[-1]][k],  temps[0],  temps[-1],  x)
        act = T[x][k]
        if act: loc.append(abs((pl-act)/act*100)); ext.append(abs((pe-act)/act*100))
    lm, em = sum(loc)/len(loc), sum(ext)/len(ext)
    verdict = "dense-interp OK" if lm < 15 else ("non-monotonic" if lm < em else "rough")
    print(f"{name:<12}{lm:<18.0f}{em:<18.0f}  {verdict}")
print("="*100)

# ---------- (3) process: can ANY interp help? (FF->TT->SS adjacency) ----------
print("\nPROCESS interpolatability — predict TT from FF&SS (the only 3 process corners):")
for k,name in keys:
    pred = (P["FF"][k]+P["SS"][k])/2
    act = P["TT"][k]
    flag = "  <-- FF OP-degraded (m4 triode)" if k in ("Zlf","Zpk","Pworst") else ""
    print(f"  {name:<10} pred {pred:<10.3g} actual {act:<10.3g} err {(pred-act)/act*100:+.0f}%{flag}")
print("  => process corners change device REGION (FF m4 triode) -> no interp; route-A sections required.")
