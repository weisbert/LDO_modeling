"""Edge-rate dependence: the linear/slew boundary depends on di/dt, not just dI.
Claim fixes boundary at amplitude (~1mA) for a 1ns edge. Test: same amplitude,
vary edge; and small amplitude with very fast edge."""
import numpy as np
import ng
WORK=ng.ROOT/"work"/"edge"
def run(tb,tag,out="o.dat"):
    r=ng.run(ng.assemble(tb),WORK/tag,outputs=[out])
    if r[out] is None: raise RuntimeError(r["_stderr"][-1000:])
    return r[out][1]
# LTI reference peak droop/mA @121u = 95.19 ohm (from prior runs)
LTI=95.19
base=121e-6
print("Edge-rate dependence at FIXED amplitude dI=500u (claim says 'linear' <1mA):")
print(f"{'edge':>8} {'GT droop/mA':>12} {'dev_vs_LTI':>11}")
for edge in [0.1e-9,1e-9,10e-9,100e-9,1e-6]:
    dI=500e-6
    tb=f"""* s
Xldo vin vout ldo_gt
Vin vin 0 DC 1.05
Iload vout 0 PWL(0 {base} 5u {base} {5e-6+edge} {base+dI} 15u {base+dI})
.control
set wr_singlescale
tran {edge/4 if edge<1e-9 else 0.5e-9} 12u 0 {edge/4 if edge<1e-9 else 0.5e-9}
wrdata o.dat v(vout)
quit
.endc
.end
"""
    b=run(tb,"s");t,v=b[:,0],b[:,1]
    pre=v[(t>3e-6)&(t<5e-6)].mean()
    vmin=v[(t>5e-6)&(t<11e-6)].min()
    dpm=(pre-vmin)*1e3/(dI*1e3)
    print(f"{edge*1e9:6.1f}ns {dpm:12.2f} {100*(dpm-LTI)/LTI:+10.1f}%")
print("\n=> If droop/mA shrinks a lot for slow edges, boundary is di/dt not dI.")
