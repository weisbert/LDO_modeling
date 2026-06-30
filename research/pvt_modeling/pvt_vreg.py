#!/usr/bin/env python3
"""V-axis DONE RIGHT: the LDO OUTPUT setpoint vreg (~0.8V, the exposed `vreg_<rail>`
knob), NOT the supply. Fix supply Vin=1.05, sweep vreg via Vref=vreg/1.5 (ldo_gt
divider R1=500k/R2=1meg -> Vout=Vref*1.5). Run at T=27 and T=125 to expose the
vreg x PVT interaction (raising vreg eats headroom Vin-vreg -> lower current ceiling
& earlier dropout, worst at hot/SS). Local ngspice, scratchpad."""
import os, re, subprocess, json
REPO="/home/yusheng/cadence_work/Test/workarea/LDO_modeling"
WD="/tmp/claude-1000/-home-yusheng-cadence-work-Test-workarea-LDO-modeling/95e8958d-7617-4337-a59f-6e96a1e43594/scratchpad/pvt_vreg"
NG="/home/yusheng/.local/bin/ngspice"; os.makedirs(WD,exist_ok=True)
NMOD=open(f"{REPO}/models/nmos_lv.mod").read(); PMOD=open(f"{REPO}/models/pmos_lv.mod").read()
def _sub(t,k,fn):
    return re.compile(r"("+re.escape(k)+r"=\s*)([-+]?[0-9][0-9.eE+\-]*)").sub(
        lambda m:m.group(1)+fn(float(m.group(2))),t,count=1)
def skew(s):
    dvth,ku,kt=0.03*s,1.0-0.08*s,1.0+0.02*s
    n=_sub(_sub(_sub(NMOD,"Vth0",lambda v:f"{v+dvth:.5g}"),"U0",lambda v:f"{v*ku:.6g}"),"Tox",lambda v:f"{v*kt:.4g}")
    p=_sub(_sub(_sub(PMOD,"Vth0",lambda v:f"{v-dvth:.5g}"),"U0",lambda v:f"{v*ku:.6g}"),"Tox",lambda v:f"{v*kt:.4g}")
    np_,pp_=f"{WD}/n{s}.mod",f"{WD}/p{s}.mod"; open(np_,"w").write(n); open(pp_,"w").write(p); return np_,pp_

VIN=1.05
DUT="""\
Vin vin 0 DC {vin} AC {acv}
Vref vref 0 DC {vref}
{il}
mp vout ng vin vin plv W=400u L=0.2u
m1 n1 fb vtail 0 nlv W=10u L=0.5u
m2 ng vref vtail 0 nlv W=10u L=0.5u
m3 n1 n1 vin vin plv W=40u L=0.5u
m4 ng n1 vin vin plv W=40u L=0.5u
mtail vtail nb 0 0 nlv W=20u L=0.5u
mb nb nb 0 0 nlv W=20u L=0.5u
Ibias vin nb DC 10u
R1 vout fb 500k
R2 fb 0 1meg
Cout vout ncesr 100p
Resr ncesr 0 0.5
.options temp={t}
"""
def run(p):
    subprocess.run([NG,"-b",p],capture_output=True,text=True,cwd=WD,timeout=180)
def cols(fn):
    xs,ys=[],[]
    for ln in open(fn):
        ln=ln.strip()
        if not ln or ln[0].isalpha(): continue
        q=ln.split()
        try: xs.append(float(q[0])); ys.append(float(q[1]))
        except: pass
    return xs,ys
def deck(cid,h,body,ctrl):
    open(f"{WD}/{cid}.cir","w").write(h+body+ctrl); run(f"{WD}/{cid}.cir")

def measure(cid,vreg,temp,s=0):
    np_,pp_=skew(s); h=f".include {np_}\n.include {pp_}\n"; vref=vreg/1.5; o={"vreg":vreg,"temp":temp}
    body=DUT.format(vin=VIN,acv=0,vref=vref,t=temp,il="Iload vout 0 DC 121u AC 1")
    deck(f"ac_{cid}",h,body,f""".control
op
print @mp[vds] @mp[vdsat] @m4[vds] @m4[vdsat] > {WD}/op_{cid}.txt
ac dec 30 10 100meg
let z=abs(v(vout))
wrdata {WD}/z_{cid}.dat z
dc Iload 1u 60m 60u
wrdata {WD}/d_{cid}.dat v(vout)
.endc
.end
""")
    f,z=cols(f"{WD}/z_{cid}.dat"); o["Zlf"]=z[0]; o["Zpk"]=max(z); o["Q"]=max(z)/z[0]
    il,vo=cols(f"{WD}/d_{cid}.dat"); j=min(range(len(il)),key=lambda k:abs(il[k]-121e-6))
    o["Vout"]=vo[j]; ref=vo[j]; ceil=il[0]
    for i,v in zip(il,vo):
        if v>=0.90*ref: ceil=i
        else: break
    o["Iceil"]=ceil; o["headroom"]=VIN-vreg
    op={}
    for ln in open(f"{WD}/op_{cid}.txt"):
        m=re.match(r"\s*@(\w+)\[(vds|vdsat)\]\s*=\s*([-\d.eE+]+)",ln)
        if m: op[(m.group(1),m.group(2))]=float(m.group(3))
    bad=[d for d in ("mp","m4") if (d,"vds") in op and op[(d,"vds")]<op[(d,"vdsat")]-1e-4]
    o["OP"]="ok" if not bad else "TRI:"+",".join(bad)
    body=DUT.format(vin=VIN,acv=1,vref=vref,t=temp,il="Iload vout 0 DC 121u AC 0")
    deck(f"p_{cid}",h,body,f""".control
ac dec 30 10 100meg
let p=-db(abs(v(vout)))
wrdata {WD}/p_{cid}.dat p
.endc
.end
""")
    f,p=cols(f"{WD}/p_{cid}.dat"); o["Pworst"]=min(p)
    body=DUT.format(vin=VIN,acv=0,vref=vref,t=temp,il="Iload vout 0 PWL(0 121u 0.999u 121u 1u 1.121m 5u 1.121m)")
    deck(f"t_{cid}",h,body,f""".control
tran 2n 4u uic
wrdata {WD}/tr_{cid}.dat v(vout)
.endc
.end
""")
    t,v=cols(f"{WD}/tr_{cid}.dat"); pre=[vv for tt,vv in zip(t,v) if tt<1e-6]; vp=pre[-1] if pre else v[0]
    post=[vv for tt,vv in zip(t,v) if tt>=1e-6]; o["dip_mV"]=(vp-min(post))*1e3 if post else float("nan")
    return o

VREGS=[0.70,0.80,0.90,0.95]
rows27=[measure(f"r{int(vr*100)}_27",vr,27) for vr in VREGS]
rows125=[measure(f"r{int(vr*100)}_125",vr,125) for vr in VREGS]
json.dump({"T27":rows27,"T125":rows125},open(f"{WD}/vreg.json","w"),indent=1)

def show(title,rows):
    print("\n"+"="*84); print(title); print("="*84)
    print(f"{'output vreg [V]':<18}"+"".join(f"{r['vreg']:<15.2f}" for r in rows))
    print(f"{'headroom Vin-vreg':<18}"+"".join(f"{r['headroom']:<15.2f}" for r in rows))
    print("-"*84)
    for k,nm,fmt in [("Vout","Vout actual [V]","{:.3f}"),("Iceil","I ceiling [A]","{:.3g}"),
                     ("Zlf","Zout LF [ohm]","{:.3g}"),("Zpk","Zout peak [ohm]","{:.3g}"),
                     ("Q","Z peak/LF [x]","{:.1f}"),("Pworst","PSRR worst [dB]","{:.1f}"),
                     ("dip_mV","loadstep dip[mV]","{:.0f}")]:
        print(f"{nm:<18}"+"".join(f"{fmt.format(r[k]):<15}" for r in rows))
    print(f"{'OP valid':<18}"+"".join(f"{r['OP']:<15}" for r in rows))
    print("="*84)
print("V-AXIS = LDO OUTPUT SETPOINT vreg (supply fixed Vin=1.05). Real WuR rail = 0.8V.")
show("vreg sweep @ T=27C (TT)", rows27)
show("vreg sweep @ T=125C (hot) — the vreg x PVT interaction", rows125)
print("\nNOTE: ceiling at vreg=0.8 vs 0.95, and how hot collapses it further:")
for a,b in [(rows27,"27C"),(rows125,"125C")]:
    c8=[r for r in a if r['vreg']==0.80][0]['Iceil']; c95=[r for r in a if r['vreg']==0.95][0]['Iceil']
    print(f"  T={b}: I-ceil  vreg0.80={c8*1e3:.1f}mA  vreg0.95={c95*1e3:.1f}mA  ({c8/c95:.1f}x drop raising setpoint)")
