#!/usr/bin/env python3
"""Cross-check the PVT findings on a 2nd, robustly-biased DUT: the two-stage
Miller LDO (ground_truth/ldo_v3_miller.lib). Its pass-gate bias is decoupled
from the input-mirror, so FF should NOT push a mirror into triode (the ldo_gt
caveat). Process axis FF/TT/SS @27C + hot 125C. Local ngspice, scratchpad."""
import os, re, subprocess, json
REPO="/home/yusheng/cadence_work/Test/workarea/LDO_modeling"
WD="/tmp/claude-1000/-home-yusheng-cadence-work-Test-workarea-LDO-modeling/95e8958d-7617-4337-a59f-6e96a1e43594/scratchpad/pvt_v3"
NG="/home/yusheng/.local/bin/ngspice"
os.makedirs(WD, exist_ok=True)
NMOD=open(f"{REPO}/models/nmos_lv.mod").read(); PMOD=open(f"{REPO}/models/pmos_lv.mod").read()
LIB=f"{REPO}/ground_truth/ldo_v3_miller.lib"

def _sub(t,k,fn):
    return re.compile(r"("+re.escape(k)+r"=\s*)([-+]?[0-9][0-9.eE+\-]*)").sub(
        lambda m:m.group(1)+fn(float(m.group(2))), t, count=1)
def skew(s):
    dvth,ku,kt=0.03*s,1.0-0.08*s,1.0+0.02*s
    n=_sub(_sub(_sub(NMOD,"Vth0",lambda v:f"{v+dvth:.5g}"),"U0",lambda v:f"{v*ku:.6g}"),"Tox",lambda v:f"{v*kt:.4g}")
    p=_sub(_sub(_sub(PMOD,"Vth0",lambda v:f"{v-dvth:.5g}"),"U0",lambda v:f"{v*ku:.6g}"),"Tox",lambda v:f"{v*kt:.4g}")
    tag={-1:"ff",0:"tt",1:"ss"}[s]
    np_,pp_=f"{WD}/n_{tag}.mod",f"{WD}/p_{tag}.mod"; open(np_,"w").write(n); open(pp_,"w").write(p)
    return np_,pp_

def deck(cid, np_, pp_, kind, temp):
    head=f".include {np_}\n.include {pp_}\n.include {LIB}\n"
    opts=f".options temp={temp}\n"
    if kind=="acdc":
        src=("Vin vin 0 DC 1.05 AC 0\nIload vout 0 DC 121u AC 1\nXdut vin vout ldo_v3_miller\n")
        ctrl=f""".control
op
print @m.xdut.mp[vds] @m.xdut.mp[vdsat] @m.xdut.m4[vds] @m.xdut.m4[vdsat] @m.xdut.m2g[vds] @m.xdut.m2g[vdsat] @m.xdut.m2l[vds] @m.xdut.m2l[vdsat] > {WD}/op_{cid}.txt
ac dec 30 10 100meg
let zmag=abs(v(vout))
wrdata {WD}/zout_{cid}.dat zmag
dc Iload 1u 60m 60u
wrdata {WD}/drop_{cid}.dat v(vout)
.endc
.end
"""
    elif kind=="psrr":
        src=("Vin vin 0 DC 1.05 AC 1\nIload vout 0 DC 121u AC 0\nXdut vin vout ldo_v3_miller\n")
        ctrl=f""".control
ac dec 30 10 100meg
let p=-db(abs(v(vout)))
wrdata {WD}/psrr_{cid}.dat p
.endc
.end
"""
    else:
        src=("Vin vin 0 DC 1.05\nIload vout 0 PWL(0 121u 0.999u 121u 1u 1.121m 5u 1.121m)\nXdut vin vout ldo_v3_miller\n")
        ctrl=f""".control
tran 2n 4u uic
wrdata {WD}/tran_{cid}.dat v(vout)
.endc
.end
"""
    path=f"{WD}/{cid}_{kind}.cir"; open(path,"w").write(head+src+opts+ctrl); return path

def run(p):
    r=subprocess.run([NG,"-b",p],capture_output=True,text=True,cwd=WD,timeout=180)
    if r.returncode!=0 and "fatal" in (r.stdout+r.stderr).lower():
        print(f"FAIL {p}\n{(r.stdout+r.stderr)[-1500:]}")
    return r
def cols(fn):
    xs,ys=[],[]
    for ln in open(fn):
        ln=ln.strip()
        if not ln or ln[0].isalpha(): continue
        q=ln.split()
        try: xs.append(float(q[0])); ys.append(float(q[1]))
        except: pass
    return xs,ys

def measure(cid,s,temp):
    np_,pp_=skew(s); o={"id":cid}
    run(deck(cid,np_,pp_,"acdc",temp))
    f,z=cols(f"{WD}/zout_{cid}.dat"); o["Zlf"]=z[0]; o["Zpk"]=max(z); o["Q"]=max(z)/z[0]
    il,vo=cols(f"{WD}/drop_{cid}.dat")
    j=min(range(len(il)),key=lambda k:abs(il[k]-121e-6)); vref=vo[j]; o["Vreg121"]=vref
    ceil=il[0]
    for i,v in zip(il,vo):
        if v>=0.90*vref: ceil=i
        else: break
    o["Iceil"]=ceil
    op={}
    for ln in open(f"{WD}/op_{cid}.txt"):
        m=re.match(r"\s*@m\.xdut\.(\w+)\[(vds|vdsat)\]\s*=\s*([-\d.eE+]+)",ln)
        if m: op[(m.group(1),m.group(2))]=float(m.group(3))
    bad=[d for d in ("mp","m4","m2g","m2l") if (d,"vds") in op and op[(d,"vds")]<op[(d,"vdsat")]-1e-4]
    o["OP"]="ok" if not bad else "TRI:"+",".join(bad)
    run(deck(cid,np_,pp_,"psrr",temp))
    f,p=cols(f"{WD}/psrr_{cid}.dat"); o["Plf"]=p[0]; o["Pworst"]=min(p)
    run(deck(cid,np_,pp_,"tran",temp))
    t,v=cols(f"{WD}/tran_{cid}.dat")
    pre=[vv for tt,vv in zip(t,v) if tt<1e-6]; vp=pre[-1] if pre else v[0]
    post=[vv for tt,vv in zip(t,v) if tt>=1e-6]; o["dip_mV"]=(vp-min(post))*1e3 if post else float("nan")
    return o

rows=[measure("FF",-1,27),measure("TT",0,27),measure("SS",1,27),measure("HOT",0,125)]
json.dump(rows,open(f"{WD}/v3.json","w"),indent=1)
print("="*92)
print("v3_miller (two-stage Miller, Cout=1nF) — process axis @1.05V/27C + hot 125C")
print("="*92)
hdr=[r["id"] for r in rows]
print(f"{'observable':<18}"+"".join(f"{h:<16}" for h in hdr))
print("-"*92)
for k,nm,fmt in [("Zlf","Zout LF [ohm]","{:.3g}"),("Zpk","Zout peak [ohm]","{:.3g}"),
                 ("Q","Z peak/LF [x]","{:.2f}"),("Plf","PSRR LF [dB]","{:.1f}"),
                 ("Pworst","PSRR worst [dB]","{:.1f}"),("Vreg121","Vout@121u [V]","{:.3f}"),
                 ("Iceil","I ceiling [A]","{:.3g}"),("dip_mV","dip [mV]","{:.0f}")]:
    b=rows[1][k]  # TT reference
    cells=[]
    for r in rows:
        d=(r[k]-b)/b*100 if b else 0
        cells.append(fmt.format(r[k])+("" if r["id"]=="TT" else f" ({d:+.0f}%)"))
    print(f"{nm:<18}"+"".join(f"{c:<16}" for c in cells))
print(f"{'OP valid':<18}"+"".join(f"{r['OP']:<16}" for r in rows))
print("="*92)
