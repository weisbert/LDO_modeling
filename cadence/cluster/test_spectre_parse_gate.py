"""LOCAL-SPECTRE PARSE-GATE -- the desk-side guard that would have caught the current-noise
`oprobe=` token-order bug BEFORE the box.

The augmenter emits per-group Spectre decks that, until now, were only ever parsed by a real
Spectre/ALPS engine on the company box -- so a malformed analysis card (e.g. `nz oprobe=X noise
...`, oprobe= before the `noise` type) failed only after a Donau round-trip. Local Spectre 18.1
IS installed on the dev box (cadence/spectre_run.py), so we can netlist the shipped manifest and
run EVERY distinct emitted card shape through it at desk time.

Skips cleanly when Spectre is absent (CI / the box, which have no local Cadence) -- there the
decks are validated where Spectre actually exists. The fast default test runs ONE deck per
distinct card SHAPE (~6-7 sims); LDO_SPECTRE_GATE=1 runs the full per-group sweep."""
import os
import re
import json
import pathlib
import sys

import pytest

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))                       # .../cadence on sys.path

from insitu import manifest as M                           # noqa: E402
from insitu import run as RUN                              # noqa: E402
from cluster import netlist_augment as NA                  # noqa: E402
import spectre_run as sr                                   # noqa: E402

WUR = HERE.parent / "insitu" / "manifests" / "wur_pmu_top.json"

# behavioral WuR_PMU_TOP stand-in: pure passive, real pin signature, enough structure that every
# emitted analysis (Zout/PSRR/noise/y/pi/iv/dropout/tran) elaborates + runs. Realism is irrelevant
# -- the gate checks that Spectre ACCEPTS the deck, not that the numbers are physical.
MODELS_SCS = """\
simulator lang=spectre
subckt WuR_PMU_TOP (avdd pll vco lpf vco3 ptat vss)
  Rs_pll (avdd pll)  resistor r=300
  Ro_pll (pll vss)   resistor r=8
  Co_pll (pll vss)   capacitor c=2u
  Rs_vco (avdd vco)  resistor r=300
  Ro_vco (vco vss)   resistor r=8
  Co_vco (vco vss)   capacitor c=2u
  Rc_lpf  (avdd lpf)  resistor r=40k
  Rg_lpf  (lpf vss)   resistor r=3k
  Rc_3u   (avdd vco3) resistor r=40k
  Rg_3u   (vco3 vss)  resistor r=3k
  Rc_ptat (avdd ptat) resistor r=40k
  Rg_ptat (ptat vss)  resistor r=3k
ends WuR_PMU_TOP
"""

# base TB matching the shipped wur nets/sources EXACTLY (source-reuse model), so the augmenter
# augments the very decks that go to the box; only the DUT is a stand-in. {models} = absolute
# include (the per-group deck is relocated by spectre_run, so the include must be absolute).
BASE_TB = """\
simulator lang=spectre
include "{models}"
Xdut (AVDD1P0 VDD0P8_PLL VDD0P8_VCO IBP_POLY_500N_LPF IBP_POLY_3P6U_VCO IBP_PTAT_TUNE_1P5U_VCO VSS) WuR_PMU_TOP
V_AVDD (AVDD1P0 VSS) vsource dc=0.98
Iload_pll (VDD0P8_PLL VSS) isource dc=500u
Iload_vco (VDD0P8_VCO VSS) isource dc=2m
Vbias_500n_lpf (IBP_POLY_500N_LPF VSS) vsource dc=1.28
Vbias_3p6u_vco (IBP_POLY_3P6U_VCO VSS) vsource dc=1.28
Vbias_1p5u_ptat (IBP_PTAT_TUNE_1P5U_VCO VSS) vsource dc=0.667
Vgnd (VSS 0) vsource dc=0
tt tran stop=1u
"""

_COVERAGE_ALL = {                                          # fire EVERY card kind
    "tier": "T4", "enable": {"inoise": True}, "lin_gate": True,
    "iv": {k: {"sweep": {"type": "lin", "start": 0.0, "stop": 1.2, "n": 7}}
           for k in ("i500n_lpf", "i3p6u_vco", "i1p5u_ptat")},
    "dropout": {k: {"sweep": {"type": "log", "start": 1e-4, "stop": 3e-3, "n": 6}}
                for k in ("pll", "vco")},
    "transient": {"pll": {"steps": [{"from": 5e-4, "to": 2e-3, "label": "step1"}],
                          "edge": 1e-9, "tstop": 1e-5, "tstep": 1e-8}},
}

needs_spectre = pytest.mark.skipif(
    not sr.available(), reason="local Spectre 18.1 not available (CI / box without Cadence env)")


def _setup(tmp_path, monkeypatch, temp=None):
    """Write the stand-in models + base TB into tmp_path, isolate the spectre work dir there,
    load the shipped wur manifest with coverage firing all card kinds, and return (m, netlister)."""
    tmp_path = pathlib.Path(tmp_path); tmp_path.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(sr, "WORK", tmp_path / "spectre_work")     # no repo pollution
    models = tmp_path / "models.scs"; models.write_text(MODELS_SCS)
    base = tmp_path / "base"; base.mkdir()
    (base / "input.scs").write_text(BASE_TB.format(models=models))
    d = json.loads(re.sub(r"<net:([^>]+)>", r"\1", WUR.read_text()))
    d["coverage"] = _COVERAGE_ALL
    mpath = tmp_path / "manifest.json"; mpath.write_text(json.dumps(d))
    m = M.load(str(mpath))
    gnl = NA.make_offline_group_netlister(str(base / "input.scs"), m, str(tmp_path / "net"), temp=temp)
    return m, gnl


def _run_group(gnl, g):
    """Netlist one group + run it through local Spectre. Returns "" on success, else the error."""
    netdir = gnl(g)
    scs = pathlib.Path(netdir, "input.scs").read_text()
    try:
        sr.run(scs, g["tag"], timeout=120)
        return ""
    except RuntimeError as e:
        errs = [ln for ln in str(e).splitlines() if "ERROR" in ln or "error:" in ln.lower()]
        return " | ".join(errs)[:400] or str(e)[-400:]


def _card_shape(g):
    """A group's distinct emitted-card SHAPE (analysis + the syntactic sub-variants). Groups with
    the same shape are syntactic dupes -- one representative is enough for a parse-gate."""
    return (g["analysis"], bool(g.get("oprobe_src")), float(g.get("amp", 1.0)) == 2.0)


@needs_spectre
def test_representative_cards_run_in_spectre(tmp_path, monkeypatch):
    """ONE emitted deck per distinct card SHAPE (ac / ac-2x / noise-node-pair / noise-oprobe /
    dc / tran) + the temperature `options temp=` card, each run through local Spectre. Fast
    (~6-7 sims). This is the regression that locks the oprobe fix: the noise-oprobe shape
    (g_ni_*) is one of the representatives and would go RED on the pre-fix token order."""
    m, gnl = _setup(tmp_path, monkeypatch)
    reps, seen = [], set()
    for g in RUN.groups(m):
        s = _card_shape(g)
        if s not in seen:
            seen.add(s); reps.append(g)
    assert any(g.get("oprobe_src") for g in reps), "the current-noise oprobe shape must be covered"
    failures = [f"{g['tag']} {_card_shape(g)}: {err}"
                for g in reps if (err := _run_group(gnl, g))]
    assert not failures, "Spectre rejected emitted deck(s):\n" + "\n".join(failures)

    # the temperature `options temp=` card is a netlister-level statement (not a per-group card),
    # so exercise it on one representative group at a non-nominal temp.
    _, gnl_t = _setup(tmp_path / "t", monkeypatch, temp=55)
    g0 = next(g for g in RUN.groups(m) if g["analysis"] == "ac")
    err = _run_group(gnl_t, g0)
    assert not err, f"temperature options-card deck rejected by Spectre: {err}"


@pytest.mark.skipif(not os.environ.get("LDO_SPECTRE_GATE"),
                    reason="full per-group Spectre sweep is opt-in (LDO_SPECTRE_GATE=1); ~2 min")
@needs_spectre
def test_all_emitted_decks_run_in_spectre(tmp_path, monkeypatch):
    """THOROUGH: every per-group deck the shipped manifest emits (all ports, all kinds) run
    through local Spectre. Opt-in (LDO_SPECTRE_GATE=1) because it is ~2 min."""
    m, gnl = _setup(tmp_path, monkeypatch)
    failures = [f"{g['tag']}: {err}"
                for g in RUN.groups(m) if (err := _run_group(gnl, g))]
    assert not failures, "Spectre rejected emitted deck(s):\n" + "\n".join(failures)


@needs_spectre
def test_spectre_rejects_oprobe_before_noise(tmp_path, monkeypatch):
    """ADVERSARIAL premise-check: prove local Spectre REJECTS the pre-fix token order
    `nz oprobe=<src> noise ...` (oprobe= before the `noise` type) and ACCEPTS the fixed order
    `nz noise ... oprobe=<src>`. This is why the gate exists -- it locks that the gate's engine
    actually catches the class of bug, independent of the augmenter."""
    monkeypatch.setattr(sr, "WORK", tmp_path / "sw")
    common = ("simulator lang=spectre\n"
              "Vs (in 0) vsource dc=1\n"
              "R1 (in mid) resistor r=1k\n"
              "V10 (mid 0) vsource dc=0 type=dc\n"
              "save V10:p\n")
    with pytest.raises(RuntimeError):                                  # bad order -> parse error
        sr.run(common + "nz oprobe=V10 noise start=10 stop=100M dec=20\n", "bad", timeout=120)
    sr.run(common + "nz noise oprobe=V10 start=10 stop=100M dec=20\n", "good", timeout=120)  # ok


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q", "-s"]))
