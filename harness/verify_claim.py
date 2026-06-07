"""Adversarial test of the claim:
  'matching Zout(s) [mag+phase] reproduces the LINEAR transient for free,
   failing only at slew/dropout (>1mA).'

Method: GT load-step transient v(t) vs LTI prediction from GT's OWN measured
Zout(jw). For a current step dI*u(t), small-signal dVout(t) = -dI * z(t)
where z(t)=IFFT of Zout(jw). If claim true: LTI matches GT transient closely
for small dI, diverges only for large/dropout dI.

We compute LTI step response from measured Zout complex spectrum directly.
"""
import numpy as np
import ng

WORK = ng.ROOT / "work" / "verify"
ILOAD = "121u"

def run(tb, tag, out="o.dat"):
    r = ng.run(ng.assemble(tb), WORK / tag, outputs=[out])
    if r[out] is None:
        raise RuntimeError(f"{tag} no data:\n{r['_stderr'][-1500:]}")
    return r[out][1]

# 1) Measure Zout(jw) on a fine linear-ish grid (need it to ~200MHz for 1ns edge)
tbz = f"""* z fine
Xldo vin vout ldo_gt
Vin vin 0 DC 1.05
Iload vout 0 DC {ILOAD} AC 0
Iac 0 vout AC 1
.control
set wr_singlescale
ac dec 200 1 500meg
wrdata o.dat vr(vout) vi(vout)
quit
.endc
.end
"""
a = run(tbz, "z")
f, Z = a[:,0], a[:,1] + 1j*a[:,2]
print(f"Zout grid: {len(f)} pts, {f[0]:.0f}..{f[-1]:.2e} Hz")

# 2) Step response from Zout via inverse transform.
# Step current dI*u(t): Vout(s) = -Zout(s)*dI/s ; v(t) = -dI * integral_0^t z(tau) dtau
# Easier: use the relation step_resp(t) = -dI * (1/pi)*integral Re{Zout(jw)} * sin(wt)/w dw ... messy.
# Cleanest: build z(t) impulse via IFFT then cumulative-integrate.
# Construct Hermitian spectrum on uniform grid by interpolation.
fmax = 400e6
N = 1<<20
df = fmax/(N//2)
fu = np.arange(N//2+1)*df
# interp Re/Im of Zout in log-f (extrapolate flat below f[0], cap at f[-1])
def interp_c(fq):
    lr = np.interp(np.log(np.clip(fq,f[0],f[-1])), np.log(f), Z.real)
    li = np.interp(np.log(np.clip(fq,f[0],f[-1])), np.log(f), Z.imag)
    return lr + 1j*li
Zu = interp_c(np.clip(fu,1e-9,None))
Zu[0] = Z.real[0]  # DC = LF resistance, real
# impulse response (real) via irfft
zt = np.fft.irfft(Zu, n=N) * (2*fmax)   # scale: irfft gives 1/dt? handle scaling below
dt = 1.0/(2*fmax)
t = np.arange(N)*dt
# step response of impedance: integral of impulse response
# Actually z(t) here IS impulse response of Zout (units ohm/s after scaling). The
# voltage response to current step dI is dI * convolution(step, h) = dI*integral h.
step_resp = np.cumsum(zt)*dt   # ohms, -> approaches Z_dc as t->inf? check
print(f"step_resp tail (should ~ Zdc={Z.real[0]:.2f}): {step_resp[N//8]:.2f} ohm")

# Predicted droop/mA from LTI = peak of step_resp (transient overshoot of impedance)
peak_imp = step_resp[:N//8].max()
dc_imp = Z.real[0]
print(f"LTI: DC impedance={dc_imp:.2f} ohm  peak step-impedance={peak_imp:.2f} ohm")
print(f"LTI predicted peak droop/mA = {peak_imp*1e-3*1e3:.2f} mV/mA  (=peak_imp[ohm]*1mA)")
print(f"   i.e. {peak_imp:.2f} mV per mA step")

# 3) GT transients at several dI; extract peak droop/mA
base=121e-6
print("\nGT transient (1ns edge) vs LTI-predicted (constant droop/mA):")
print(f"{'dI':>8} {'GTdroop/mA':>12} {'LTIdroop/mA':>12} {'ratio':>7}")
for dI in [1e-6,10e-6,50e-6,200e-6,1e-3,2e-3,5e-3]:
    tb=f"""* step
Xldo vin vout ldo_gt
Vin vin 0 DC 1.05
Iload vout 0 PWL(0 {base} 5u {base} 5.001u {base+dI} 15u {base+dI} 15.001u {base} 25u {base})
.control
set wr_singlescale
tran 0.5n 16u 0 0.5n
wrdata o.dat v(vout)
quit
.endc
.end
"""
    b=run(tb,"t")
    tt,v=b[:,0],b[:,1]
    pre=v[(tt>3e-6)&(tt<5e-6)].mean()
    m=(tt>5e-6)&(tt<13e-6)
    vmin=v[m].min()
    droop=(pre-vmin)*1e3
    dpm=droop/(dI*1e3)
    print(f"{dI*1e6:7.0f}u {dpm:12.2f} {peak_imp:12.2f} {dpm/peak_imp:7.3f}")
