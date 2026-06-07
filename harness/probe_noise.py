"""SCRATCH probe: what does the decoupled Norton-@vout noise target look like?

For each variant/corner: fit Zout (current fitter), compute In_target = Sv/|Zout|,
characterize its shape, and test candidate parametric forms:
  (1) white + 1/f          (current model's form, but at vout)
  (2) white + 1/f + NNLS Lorentzian bank (LP+HP basis) -> arbitrary smooth shape
Report log-RMS residual of the RECONSTRUCTED output PSD (In_fit*|Zout|) vs Sv_meas.
"""
import numpy as np
import fit_model as fm
import ng

np.set_printoptions(suppress=True, linewidth=140)
TWO_PI = 2 * np.pi


def lorentzian_bank(fn, corners):
    """Basis matrix: columns = white, 1/f, LP(fk), HP(fk) for each corner fk.
    Each column is a non-negative PSD shape [unit at its plateau]. NNLS picks
    non-negative weights -> physically realizable as parallel noise sources."""
    cols = [np.ones_like(fn)]                      # white
    names = ["white"]
    cols.append(1.0 / fn); names.append("1/f")     # flicker (PSD ~ 1/f)
    for fk in corners:
        x2 = (fn / fk) ** 2
        cols.append(1.0 / (1.0 + x2)); names.append(f"LP{fk:.0e}")
        cols.append(x2 / (1.0 + x2)); names.append(f"HP{fk:.0e}")
    return np.array(cols).T, names


def nnls_fit(fn, In2_target, corners):
    from scipy.optimize import nnls
    A, names = lorentzian_bank(fn, corners)
    # relative weighting in log-power: weight rows by 1/target so we fit the SHAPE
    w = 1.0 / (In2_target + 1e-40)
    coef, _ = nnls(A * np.sqrt(w)[:, None], In2_target * np.sqrt(w))
    return coef, names, A


def foster_logfit(fn, In_target, N, flicker=True):
    """Fit In(f)^2 = white [+ flicker(a/f)] + sum_k Lor_k in the LOG domain (free
    corners & amplitudes, positive via exp). Returns (params, In_fit). Realizable
    Foster form: white-R floor [+ 1/f ladder] + N RC-Lorentzian noise sections."""
    from scipy.optimize import least_squares
    y = np.log(In_target ** 2 + 1e-80)
    f0, f1 = fn[0], fn[-1]
    wht0 = np.log(In_target[-3:].mean() ** 2 + 1e-80)
    flk0 = np.log(In_target[0] ** 2 * fn[0] + 1e-80)
    cs = np.log(np.logspace(np.log10(f0 * 1.5), np.log10(f1 / 1.5), N))
    amps = np.log(np.interp(np.exp(cs), fn, In_target ** 2) + 1e-80)

    def model(p):
        out = np.exp(np.clip(p[0], -180, 40)) * np.ones_like(fn)
        i0 = 1
        if flicker:
            out = out + np.exp(np.clip(p[1], -180, 40)) / fn; i0 = 2
        for k in range(N):
            ak = np.exp(np.clip(p[i0 + 2 * k], -180, 40))
            ck = np.exp(np.clip(p[i0 + 1 + 2 * k], np.log(f0/10), np.log(f1*10)))
            out = out + ak / (1.0 + (fn / ck) ** 2)
        return out

    def resid(p):
        return np.log(model(p) + 1e-80) - y
    head = [wht0, flk0] if flicker else [wht0]
    p0 = np.concatenate([head, np.ravel(np.column_stack([amps, cs]))])
    s = least_squares(resid, p0, method="lm", max_nfev=12000)
    return s.x, np.sqrt(model(s.x))


def main():
    for vk in ("base", "v1_nmos", "v2_capless", "v3_miller", "v4_ffpsrr"):
        try:
            fm.load(vk)
        except Exception as e:
            print(f"{vk}: load failed {e}"); continue
        print(f"\n================ {vk}  (Cout={fm.C*1e12:.0f}pF ESR={fm.RC:.2f}) ================")
        for il in fm.LOADS:
            gz = fm.ref[f"z_{il}"]; fz = gz[:, 0]; Z = gz[:, 1] + 1j * gz[:, 2]
            gn = fm.ref[f"noise_{il}"]; fn = gn[:, 0]; Sv = gn[:, 1]
            R_a, L_a, R_pl, R_b, L_b = fm.fit_zout(fz, Z)
            Zmod = np.abs(fm.zmodel(fn, R_a, L_a, R_pl, R_b, L_b))
            In = Sv / Zmod                                   # Norton-@vout target [A/rtHz]
            # shape descriptors
            lf = np.interp(1e2, fn, In); mid = np.interp(2e5, fn, In); hf = np.interp(5e7, fn, In)
            # candidate (1): white + 1/f fit to In
            vnw = mid; fc = max(np.median((np.interp(np.logspace(1,3,20),fn,In)**2/vnw**2 - 1)
                                          * np.logspace(1,3,20)), 1.0)
            In1 = np.sqrt(vnw**2 * (1 + fc/fn))
            r1 = np.sqrt(np.mean((20*np.log10((In1*Zmod)/(Sv+1e-30)))**2))
            # candidate (2): Foster with analytic flicker (white+1/f+N Lor)
            def rfit(N, flk):
                try:
                    _, InF = foster_logfit(fn, In, N, flicker=flk)
                    return np.sqrt(np.mean((20*np.log10((InF*Zmod)/(Sv+1e-30)))**2))
                except Exception:
                    return float("nan")
            # with flicker: N=3,4 ; pure-Lorentzian (no analytic 1/f): M=5,6,8
            print(f"  {il:>5}: In LF={lf*1e12:8.0f} mid={mid*1e12:7.1f} hf={hf*1e12:6.1f} "
                  f"| flk+N3={rfit(3,True):4.2f} N4={rfit(4,True):4.2f} "
                  f"| LorOnly M5={rfit(5,False):4.2f} M6={rfit(6,False):4.2f} M8={rfit(8,False):4.2f} dB")


if __name__ == "__main__":
    main()
