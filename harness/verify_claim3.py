"""Part 3 (clean): pin down the linear boundary and waveform-shape fidelity.
Decisively: GT transient vs LTI-from-its-own-Zout, fine dI sweep + full-shape NRMSE.
"""
import numpy as np
import ng
WORK = ng.ROOT / "work" / "verify3"
def run(tb, tag, out="o.dat"):
    r = ng.run(ng.assemble(tb), WORK / tag, outputs=[out])
    if r[out] is None: raise RuntimeError(f"{tag}:\n{r['_stderr'][-1200:]}")
    return r[out][1]

def get_Z(iload):
    tbz=f"""* z
Xldo vin vout ldo_gt
Vin vin 0 DC 1.05
Iload vout 0 DC {iload} AC 0
Iac 0 vout AC 1
.control
set wr_singlescale
ac dec 300 1 800meg
wrdata o.dat vr(vout) vi(vout)
quit
.endc
.end
"""
    a=run(tbz,"z"); return a[:,0], a[:,1]+1j*a[:,2]

def lti_step_imp(f,Z):
    """Return (t, step_impedance(t)) — voltage droop per unit current step."""
    fmax=400e6; N=1<<21; df=fmax/(N//2); fu=np.arange(N//2+1)*df
    lr=np.interp(np.log(np.clip(fu,f[0],f[-1])),np.log(f),Z.real)
    li=np.interp(np.log(np.clip(fu,f[0],f[-1])),np.log(f),Z.imag)
    Zu=lr+1j*li; Zu[0]=Z.real[0]
    zt=np.fft.irfft(Zu,n=N)*(2*fmax); dt=1/(2*fmax)
    sr=np.cumsum(zt)*dt; tt=np.arange(N)*dt
    return tt,sr

f,Z=get_Z("121u")
tt_l,sr=lti_step_imp(f,Z)
peak=sr[tt_l<5e-6].max()
print(f"LTI peak step-impedance @121u = {peak:.2f} ohm  (=> {peak:.2f} mV/mA, OP-fixed/linear)")
print(f"LTI DC step-impedance (settle) = {sr[(tt_l>3e-6)&(tt_l<5e-6)].mean():.2f} ohm\n")

base=121e-6
print("Linear-boundary sweep: GT droop/mA vs LTI-constant; deviation = nonlinearity")
print(f"{'dI':>8} {'GT_droop/mA':>12} {'dev_from_LTI':>13}")
for dI in [0.5e-6,1e-6,5e-6,10e-6,20e-6,50e-6,100e-6,200e-6,500e-6,1e-3,1.5e-3,2e-3,3e-3,5e-3]:
    tb=f"""* s
Xldo vin vout ldo_gt
Vin vin 0 DC 1.05
Iload vout 0 PWL(0 {base} 5u {base} 5.001u {base+dI} 15u {base+dI})
.control
set wr_singlescale
tran 0.5n 12u 0 0.5n
wrdata o.dat v(vout)
quit
.endc
.end
"""
    b=run(tb,"s"); t,v=b[:,0],b[:,1]
    pre=v[(t>3e-6)&(t<5e-6)].mean()
    vmin=v[(t>5e-6)&(t<11e-6)].min()
    dpm=(pre-vmin)*1e3/(dI*1e3)
    dev=100*(dpm-peak)/peak
    print(f"{dI*1e6:7.1f}u {dpm:12.2f} {dev:+12.1f}%")

# Full-shape fidelity: small step dI=50u, compare entire ringing waveform
print("\nFull-waveform shape fidelity (dI=50u, linear regime):")
dI=50e-6
tb=f"""* s
Xldo vin vout ldo_gt
Vin vin 0 DC 1.05
Iload vout 0 PWL(0 {base} 5u {base} 5.001u {base+dI} 15u {base+dI})
.control
set wr_singlescale
tran 0.1n 8u 0 0.1n
wrdata o.dat v(vout)
quit
.endc
.end
"""
b=run(tb,"s"); t,v=b[:,0],b[:,1]
pre=v[(t>3e-6)&(t<5e-6)].mean()
gt=(pre-v)
tloc=t-5.001e-6
lti=np.interp(np.clip(tloc,0,None),tt_l,sr)*dI
lti[tloc<0]=0
w=(t>5.001e-6)&(t<7e-6)
g,l=gt[w],lti[w]
nrmse=np.sqrt(np.mean((g-l)**2))/(g.max()-g.min())
# find ringing freq from GT (FFT of windowed)
print(f"  GT pk={g.max()*1e3:.4f}mV LTI pk={l.max()*1e3:.4f}mV  pk-err={100*(l.max()-g.max())/g.max():+.1f}%")
print(f"  full-shape NRMSE over 2us window = {nrmse*100:.2f}%")
