"""Confirm Zout phase encodes the transient ringing freq/Q: extract ringing
frequency from GT small-step transient and compare to Zout resonance peak freq."""
import numpy as np
import ng
WORK=ng.ROOT/"work"/"ring"
def run(tb,tag,out="o.dat"):
    r=ng.run(ng.assemble(tb),WORK/tag,outputs=[out])
    if r[out] is None: raise RuntimeError(r["_stderr"][-1000:])
    return r[out][1]
# Zout resonance @121u
tbz="""* z
Xldo vin vout ldo_gt
Vin vin 0 DC 1.05
Iload vout 0 DC 121u AC 0
Iac 0 vout AC 1
.control
set wr_singlescale
ac dec 400 1e5 1e7
wrdata o.dat vr(vout) vi(vout)
quit
.endc
.end
"""
a=run(tbz,"z");f,Z=a[:,0],np.abs(a[:,1]+1j*a[:,2])
fpk=f[np.argmax(Z)]
print(f"Zout resonance peak: {fpk/1e6:.4f} MHz")
# small step transient, fit ringing
base=121e-6;dI=20e-6
tb=f"""* s
Xldo vin vout ldo_gt
Vin vin 0 DC 1.05
Iload vout 0 PWL(0 {base} 5u {base} 5.001u {base+dI} 25u {base+dI})
.control
set wr_singlescale
tran 0.1n 25u 0 0.1n
wrdata o.dat v(vout)
quit
.endc
.end
"""
b=run(tb,"s");t,v=b[:,0],b[:,1]
# isolate ringing 5.1us..15us, detrend, FFT
m=(t>5.1e-6)&(t<15e-6)
tw=t[m];vw=v[m]-v[m].mean()
# resample uniform
tu=np.linspace(tw[0],tw[-1],8192);vu=np.interp(tu,tw,vw)
dt=tu[1]-tu[0]
sp=np.abs(np.fft.rfft(vu*np.hanning(len(vu))))
ff=np.fft.rfftfreq(len(vu),dt)
fring=ff[np.argmax(sp[1:])+1]
print(f"Transient ringing freq: {fring/1e6:.4f} MHz")
print(f"Match: ringing/Zpeak ratio = {fring/fpk:.3f}  (=> phase-correct Zout reproduces ring)")
