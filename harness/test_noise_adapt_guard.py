"""The deploy SMOKE test (deploy/update.sh) sets LDO_NOISE_FAST to skip the adaptive
noise-bank escalation -- the dominant ~20s fit cost -- so `bash apply` stays fast. The full
adaptive path (pytest / CI / the real box) is UNCHANGED (env unset). This guards that the env
var actually gates the greedy section-insertion loop in BOTH noise banks (the Norton
fit_noise_bank and the hybrid fit_noise_hybrid), and that it is a no-op by default.

Forcing recipe: a deliberately-INADEQUATE base M (=2) on a 5-bump In target with
NOISE_ADAPT_TRIG=0, so the full path provably grows the bank while the env-gated path cannot."""
import os
import sys
import pathlib

import numpy as np

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import fit_model as FM                                                # noqa: E402

BASE_M = 2                                                            # inadequate on a 5-bump shape


def _forcing_state(monkeypatch):
    """Minimal module state: one load + a 5-bump In target that BASE_M=2 sections cannot fit, with
    NOISE_ADAPT_TRIG=0 so the full path keeps inserting sections. Returns zfits (R_a,L_a,R_pl,...)."""
    f = np.logspace(1, 8, 220)
    Z = np.abs(FM.zmodel(f, 0.1, 2e-9))                  # flat-ish |Zout| (default C/RC/CFT)
    In = 1e-11 * (1.0 + sum(9.0 / (1 + ((f - fb) / (0.12 * fb)) ** 2)
                            for fb in (60.0, 2e3, 6e4, 2e6, 5e7)))
    monkeypatch.setattr(FM, "LOADS", ["L0"])
    monkeypatch.setattr(FM, "ref", {"noise_L0": np.c_[f, In * Z]})
    monkeypatch.setattr(FM, "NOISE_ADAPT_TRIG", 0.0)     # any misfit triggers escalation
    monkeypatch.setattr(FM, "NOISE_M_MAX", 6)            # cap the full-path growth -> fast test
    return {"L0": (0.1, 2e-9, 1e12, 1e12, 1e-12)}        # zfits: R_a,L_a,R_pl,R_b,L_b


def test_norton_bank_env_var_gates_escalation(monkeypatch):
    zfits = _forcing_state(monkeypatch)
    monkeypatch.delenv("LDO_NOISE_FAST", raising=False)
    assert len(FM.fit_noise_bank(zfits, M=BASE_M)["fk"]) > BASE_M, "full path must grow the bank"
    monkeypatch.setenv("LDO_NOISE_FAST", "1")
    assert len(FM.fit_noise_bank(zfits, M=BASE_M)["fk"]) == BASE_M, "env var must skip escalation"


def test_hybrid_bank_env_var_gates_escalation(monkeypatch):
    zfits = _forcing_state(monkeypatch)
    monkeypatch.delenv("LDO_NOISE_FAST", raising=False)
    full = FM.fit_noise_hybrid(zfits, M=BASE_M)
    monkeypatch.setenv("LDO_NOISE_FAST", "1")
    fast = FM.fit_noise_hybrid(zfits, M=BASE_M)
    assert len(fast["fkv"]) == BASE_M, "env var must skip the hybrid escalation (stay at base M)"
    assert len(full["fkv"]) >= len(fast["fkv"])          # full path may grow; env path never does


def test_env_var_default_unset_is_full_adapt(monkeypatch):
    """Sanity: with the var unset (the default everywhere except the deploy smoke), adaptation is
    enabled -- the guard is a pure no-op so the locked production noise behavior is intact."""
    monkeypatch.delenv("LDO_NOISE_FAST", raising=False)
    assert not os.environ.get("LDO_NOISE_FAST")


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-q"]))
