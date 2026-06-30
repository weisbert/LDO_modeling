#!/usr/bin/env python3
"""Comprehensive PVT experiments on the synthetic transistor LDO (ldo_gt).

Bias-fixed (WM=40u mirror). Sweeps process(FF/TT/SS) x Vin x temp, measures
Zout / PSRR / dropout-ceiling / load-step dip with an OP saturation guard, runs
an interpolation-fails analysis (route-A validation), a fine-temp smoothness
test, and a supply-voltage axis. Local ngspice, SCRATCHPAD ONLY. Verification.
"""
import os, re, subprocess, json

REPO = "/home/yusheng/cadence_work/Test/workarea/LDO_modeling"
BASE = "/tmp/claude-1000/-home-yusheng-cadence-work-Test-workarea-LDO-modeling/95e8958d-7617-4337-a59f-6e96a1e43594/scratchpad"
WD   = f"{BASE}/pvt_exp"
NG   = "/home/yusheng/.local/bin/ngspice"
os.makedirs(WD, exist_ok=True)

NMOD = open(f"{REPO}/models/nmos_lv.mod").read()
PMOD = open(f"{REPO}/models/pmos_lv.mod").read()

def _sub(text, key, fn):
    pat = re.compile(r"(" + re.escape(key) + r"=\s*)([-+]?[0-9][0-9.eE+\-]*)")
    return pat.sub(lambda m: m.group(1) + fn(float(m.group(2))), text, count=1)

_MODCACHE = {}
def skew_models(s):
    """s in [-1,1]: -1 FF (fast), 0 TT, +1 SS (slow). Gentle, OP-preserving."""
    key = round(s, 3)
    if key in _MODCACHE: return _MODCACHE[key]
    dvth, ku, kt = 0.03*s, 1.0-0.08*s, 1.0+0.02*s
    n = _sub(_sub(_sub(NMOD, "Vth0", lambda v:f"{v+dvth:.5g}"),
                  "U0", lambda v:f"{v*ku:.6g}"), "Tox", lambda v:f"{v*kt:.4g}")
    p = _sub(_sub(_sub(PMOD, "Vth0", lambda v:f"{v-dvth:.5g}"),
                  "U0", lambda v:f"{v*ku:.6g}"), "Tox", lambda v:f"{v*kt:.4g}")
    tag = f"s{key:+.2f}".replace(".","p").replace("+","P").replace("-","M")
    np_, pp_ = f"{WD}/n_{tag}.mod", f"{WD}/p_{tag}.mod"
    open(np_,"w").write(n); open(pp_,"w").write(p)
    _MODCACHE[key] = (np_, pp_)
    return np_, pp_

DUT = """\
Vin   vin 0 DC {vin} AC {acvin}
Vref  vref 0 DC 0.6
{iload_line}
mp vout ng vin vin plv W=400u L=0.2u
m1 n1 fb  vtail 0   nlv W=10u L=0.5u
m2 ng vref vtail 0  nlv W=10u L=0.5u
m3 n1 n1  vin   vin plv W=40u L=0.5u
m4 ng n1  vin   vin plv W=40u L=0.5u
mtail vtail nb 0 0  nlv W=20u L=0.5u
mb   nb nb 0 0      nlv W=20u L=0.5u
Ibias vin nb DC 10u
R1 vout fb 500k
R2 fb 0 1meg
Cout vout ncesr 100p
Resr ncesr 0 0.5
.options temp={temp}
"""

def _run(name, txt):
    path = f"{WD}/{name}.cir"
    open(path,"w").write(txt)
    r = subprocess.run([NG,"-b",path], capture_output=True, text=True, cwd=WD, timeout=180)
    if r.returncode != 0 and "fatal" in (r.stdout+r.stderr).lower():
        print(f"[{name}] ngspice FAIL\n{(r.stdout+r.stderr)[-1200:]}")
    return r

def _cols(fn):
    xs, ys = [], []
    for ln in open(fn):
        ln = ln.strip()
        if not ln or ln[0].isalpha(): continue
        p = ln.split()
        try: xs.append(float(p[0])); ys.append(float(p[1]))
        except (ValueError, IndexError): pass
    return xs, ys

def measure(cid, s, vin, temp):
    np_, pp_ = skew_models(s)
    head = f".include {np_}\n.include {pp_}\n"
    o = {"id":cid, "s":s, "vin":vin, "temp":temp}

    # --- AC: Zout (inject 1A into vout) + DC dropout/ceiling + OP guard ---
    body = DUT.format(acvin=0, vin=vin, temp=temp, iload_line="Iload vout 0 DC 121u AC 1")
    ctrl = f"""
.control
op
print @mp[vds] @mp[vdsat] @m4[vds] @m4[vdsat] @mtail[vds] @mtail[vdsat] @m2[vds] @m2[vdsat] > {WD}/op_{cid}.txt
ac dec 30 10 100meg
let zmag = abs(v(vout))
wrdata {WD}/zout_{cid}.dat zmag
dc Iload 1u 60m 60u
wrdata {WD}/drop_{cid}.dat v(vout)
.endc
.end
"""
    _run(f"acdc_{cid}", head+body+ctrl)
    f, z = _cols(f"{WD}/zout_{cid}.dat")
    o["Zlf"], o["Zpk"] = z[0], max(z)
    o["Zpkf"] = f[z.index(max(z))]
    o["Qproxy"] = max(z)/z[0]                      # peak/LF: stability/peaking proxy

    il, vo = _cols(f"{WD}/drop_{cid}.dat")
    j121 = min(range(len(il)), key=lambda k: abs(il[k]-121e-6))
    o["Vreg121"] = vo[j121]
    vref = vo[j121]
    ceil = il[0]
    for i, v in zip(il, vo):
        if v >= 0.90*vref: ceil = i
        else: break
    o["Iceil"] = ceil

    op = {}
    for ln in open(f"{WD}/op_{cid}.txt"):
        m = re.match(r"\s*@(\w+)\[(vds|vdsat)\]\s*=\s*([-\d.eE+]+)", ln)
        if m: op[(m.group(1), m.group(2))] = float(m.group(3))
    bad = [d for d in ("mp","m4","mtail","m2")
           if (d,"vds") in op and op[(d,"vds")] < op[(d,"vdsat")] - 1e-4]
    o["OPok"] = (not bad)
    o["OPbad"] = ",".join(bad)

    # --- AC: PSRR (drive vin 1V AC) ---
    body = DUT.format(acvin=1, vin=vin, temp=temp, iload_line="Iload vout 0 DC 121u AC 0")
    ctrl = f"""
.control
ac dec 30 10 100meg
let p = -db(abs(v(vout)))
wrdata {WD}/psrr_{cid}.dat p
.endc
.end
"""
    _run(f"psrr_{cid}", head+body+ctrl)
    f, p = _cols(f"{WD}/psrr_{cid}.dat")
    o["Plf"], o["Pworst"] = p[0], min(p)

    # --- TRAN: load-step dip 121u -> 1.121m (1mA step, 1ns edge) ---
    body = DUT.format(acvin=0, vin=vin, temp=temp,
        iload_line="Iload vout 0 PWL(0 121u 0.999u 121u 1u 1.121m 5u 1.121m)")
    ctrl = f"""
.control
tran 2n 4u uic
wrdata {WD}/tran_{cid}.dat v(vout)
.endc
.end
"""
    _run(f"tran_{cid}", head+body+ctrl)
    t, v = _cols(f"{WD}/tran_{cid}.dat")
    pre = [vv for tt,vv in zip(t,v) if tt < 1e-6]
    vpre = pre[-1] if pre else v[0]
    post = [vv for tt,vv in zip(t,v) if tt >= 1e-6]
    o["dip_mV"] = (vpre - min(post))*1e3 if post else float("nan")
    return o

# ----------------------- experiment grids -----------------------
results = {"process":[], "temp":[], "volt":[]}

# Process axis @ Vin=1.05, T=27
for s,lab in [(-1,"FF"),(0,"TT"),(1,"SS")]:
    results["process"].append(measure(f"P_{lab}", s, 1.05, 27))

# Fine temperature axis @ TT, Vin=1.05
for T in [-40,-15,10,27,55,85,125]:
    results["temp"].append(measure(f"T_{T}", 0, 1.05, T))

# Supply-voltage axis @ TT, T=27
for V in [0.95,1.00,1.05,1.15,1.30]:
    results["volt"].append(measure(f"V_{int(V*100)}", 0, V, 27))

json.dump(results, open(f"{WD}/results.json","w"), indent=1)

# ----------------------- tables -----------------------
KEYS = [("Zlf","Zout LF [ohm]","{:.3g}"),
        ("Zpk","Zout peak [ohm]","{:.3g}"),
        ("Qproxy","Z peak/LF [x]","{:.2f}"),
        ("Plf","PSRR LF [dB]","{:.1f}"),
        ("Pworst","PSRR worst [dB]","{:.1f}"),
        ("Vreg121","Vout@121u [V]","{:.3f}"),
        ("Iceil","I ceiling [A]","{:.3g}"),
        ("dip_mV","dip [mV]","{:.0f}")]

def table(title, rows, colname):
    print("\n"+"="*108); print(title); print("="*108)
    hdr = [r["id"].split("_",1)[1] for r in rows]
    print(f"{'observable':<18}" + "".join(f"{h:<13}" for h in hdr))
    print("-"*108)
    for k,name,fmt in KEYS:
        print(f"{name:<18}" + "".join(f"{fmt.format(r[k]):<13}" for r in rows))
    print(f"{'OP valid':<18}" + "".join(f"{('ok' if r['OPok'] else 'TRI:'+r['OPbad']):<13}" for r in rows))
    print("="*108)

table("PROCESS axis (Vin=1.05, T=27)  — bias-fixed ldo_gt", results["process"], "corner")
table("TEMPERATURE axis (TT, Vin=1.05)", results["temp"], "T")
table("SUPPLY-VOLTAGE axis (TT, T=27)", results["volt"], "Vin")

# ----------------------- interpolation-fails analysis -----------------------
def interp_err(lo, mid, hi, xlo, xmid, xhi, key):
    a, b, c = lo[key], mid[key], hi[key]
    pred = a + (c-a)*(xmid-xlo)/(xhi-xlo)
    err = (pred-b)/b*100 if b else float("nan")
    return pred, b, err

print("\n"+"="*108)
print("INTERPOLATION-FAILS ANALYSIS — predict the MID corner from linear interp of the EXTREMES")
print("(route-A says: refit each corner; interpolation REJECTED. Large err => interpolation unsafe.)")
print("="*108)

# process: FF(s=-1), TT(s=0), SS(s=+1)  -> predict TT from FF&SS
P = {r["id"].split("_")[1]:r for r in results["process"]}
# temp: predict 27 from -40 & 125
TT = {r["temp"]:r for r in results["temp"]}
print(f"{'observable':<18}{'PROCESS pred(TT)':<18}{'actual':<10}{'err%':<9}   {'TEMP pred(27C)':<18}{'actual':<10}{'err%':<9}")
print("-"*108)
for k,name,_ in KEYS:
    pp,pa,pe = interp_err(P["FF"],P["TT"],P["SS"], -1,0,1, k)
    tp,ta,te = interp_err(TT[-40],TT[27],TT[125], -40,27,125, k)
    print(f"{name:<18}{pp:<18.3g}{pa:<10.3g}{pe:<+9.0f}   {tp:<18.3g}{ta:<10.3g}{te:<+9.0f}")
print("="*108)
print("Mean |err| process vs temp:")
pe_all = [abs(interp_err(P['FF'],P['TT'],P['SS'],-1,0,1,k)[2]) for k,_,_ in KEYS]
te_all = [abs(interp_err(TT[-40],TT[27],TT[125],-40,27,125,k)[2]) for k,_,_ in KEYS]
print(f"  PROCESS mean|err| = {sum(pe_all)/len(pe_all):.0f}%   TEMP mean|err| = {sum(te_all)/len(te_all):.0f}%")
