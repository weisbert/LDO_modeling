"""Part 2: (a) waveform-shape match (ringing), not just peak;
(b) is the <1mA deviation real compression or numeric? Test SMALL step at
different absolute load points to show it's gm(operating-point) dependent;
(c) PSRR/line-step linearity check; (d) phase necessity demo.
"""
import numpy as np
import ng
WORK = ng.ROOT / "work" / "verify2"

def run(tb, tag, out="o.dat"):
    r = ng.run(ng.assemble(tb), WORK / tag, outputs=[out])
    if r[out] is None:
        raise RuntimeError(f"{tag} no data:\n{r['_stderr'][-1500:]}")
    return r[out][1]

# --- measure Zout(jw) once for LTI waveform reconstruction ---
def get_Z(iload):
    tbz=f"""* z
Xldo vin vout ldo_gt
Vin vin 0 DC 1.05
Iload vout 0 DC {iload} AC 0
Iac 0 vout AC 1
.control
set wr_singlescale
ac dec 200 1 500meg
wrdata o.dat vr(vout) vi(vout)
quit
.endc
.end
"""
    a=run(tbz,"z")
    return a[:,0], a[:,1]+1j*a[:,2]

def lti_step_waveform(f,Z,dI,tgrid):
    # build hermitian spectrum, irfft -> impulse, cumsum -> step impedance, *dI
    fmax=400e6; N=1<<20; df=fmax/(N//2); fu=np.arange(N//2+1)*df
    lr=np.interp(np.log(np.clip(fu,f[0],f[-1])),np.log(f),Z.real)
    li=np.interp(np.log(np.clip(fu,f[0],f[-1])),np.log(f),Z.imag)
    Zu=lr+1j*li; Zu[0]=Z.real[0]
    zt=np.fft.irfft(Zu,n=N)*(2*fmax); dt=1/(2*fmax)
    sr=np.cumsum(zt)*dt
    tt=np.arange(N)*dt
    # voltage droop(t) = dI * step_impedance(t)
    return np.interp(tgrid, tt, sr)*dI

f,Z = get_Z("121u")

# (a) compare full waveform shape for a small step dI=50u
base=121e-6; dI=50e-6
tb=f"""* step
Xldo vin vout ldo_gt
Vin vin 0 DC 1.05
Iload vout 0 PWL(0 {base} 5u {base} 5.001u {base+dI} 15u {base+dI})
.control
set wr_singlescale
tran 0.2n 7u 0 0.2n
wrdata o.dat v(vout)
quit
.endc
.end
"""
b=run(tb,"t")
tt,v=b[:,0],b[:,1]
pre=v[(tt>3e-6)&(tt<5e-6)].mean()
gt_droop=(pre-v)  # volts, positive=droop
# LTI prediction aligned to step at 5us
tloc=tt-5.001e-6
lti=np.zeros_like(tt)
mpos=tloc>0
lti[mpos]=lti_step_waveform(f,Z,dI,tloc[mpos])
# compare in window 5..7us
w=(tt>5.001e-6)&(tt<6.5e-6)
print("(a) WAVEFORM SHAPE match, small step dI=50u (linear regime):")
print(f"  GT peak droop={gt_droop[w].max()*1e3:.4f}mV  LTI peak droop={lti[w].max()*1e3:.4f}mV")
# correlation/NRMSE of shape
g=gt_droop[w]; l=lti[w]
nrmse=np.sqrt(np.mean((g-l)**2))/ (g.max()-g.min())
print(f"  shape NRMSE={nrmse*100:.2f}%   (does ringing match?)")
# count ringing zero-crossings of residual around settle
print(f"  GT settles to {gt_droop[(tt>9e-6)&(tt<10e-6)].mean()*1e3:.4f}mV (DC load reg drop) LTI->{lti[(tt>9e-6)].mean()*1e3 if (tt>9e-6).any() else 0:.4f}")

# (b) operating-point gm dependence: SAME small dI=20u step at 3 base loads
print("\n(b) Is sub-1mA droop/mA truly OP-dependent (=> genuinely nonlinear set)?")
print("    small dI=20u step from 3 different base loads; LTI droop/mA = Zpeak-step:")
for bl,bltag in [(20e-6,"20u"),(121e-6,"121u"),(250e-6,"250u")]:
    fb_,Zb=get_Z(bltag)
    lti_pk=lti_step_waveform(fb_,Zb,1e-3,np.linspace(1e-9,5e-6,4000)).max()  # per mA
    dI2=20e-6
    tb2=f"""* s
Xldo vin vout ldo_gt
Vin vin 0 DC 1.05
Iload vout 0 PWL(0 {bl} 5u {bl} 5.001u {bl+dI2} 15u {bl+dI2})
.control
set wr_singlescale
tran 0.5n 13u 0 0.5n
wrdata o.dat v(vout)
quit
.endc
.end
"""
    c=run(tb2,"s")
    t2,v2=c[:,0],c[:,1]
    pre2=v2[(t2>3e-6)&(t2<5e-6)].mean()
    vmin2=v2[(t2>5e-6)&(t2<12e-6)].min()
    dpm=(pre2-vmin2)*1e3/(dI2*1e3)
    print(f"  base={bltag:>5}: GT droop/mA={dpm:7.2f}  LTI(from that-load Zout)={lti_pk:7.2f}  ratio={dpm/lti_pk:.3f}")

# (c) LINE step (PSRR transient) linearity: step Vin by dV, small vs large
print("\n(c) LINE-step (PSRR transient) linearity: Vout response per mV-of-Vin-step")
for dV in [1e-3,10e-3,100e-3,300e-3]:
    tb3=f"""* line step
Xldo vin vout ldo_gt
Vin vin 0 PWL(0 1.05 5u 1.05 5.001u {1.05+dV} 15u {1.05+dV})
Iload vout 0 DC 121u
.control
set wr_singlescale
tran 0.5n 13u 0 0.5n
wrdata o.dat v(vout)
quit
.endc
.end
"""
    d=run(tb3,"l")
    t3,v3=d[:,0],d[:,1]
    pre3=v3[(t3>3e-6)&(t3<5e-6)].mean()
    vmax=v3[(t3>5e-6)&(t3<8e-6)].max()  # feedthrough spike
    settled=v3[(t3>9e-6)&(t3<12e-6)].mean()
    spike=(vmax-pre3)*1e3
    print(f"  dV={dV*1e3:6.0f}mV: peak feedthrough spike={spike:8.4f}mV  per-V={spike/dV:8.2f}mV/V  settled d={ (settled-pre3)*1e3:7.4f}mV")
