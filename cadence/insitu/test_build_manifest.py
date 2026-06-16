"""Unit/static tests for component C (build_manifest) -- no simulator, no session.

Covers: schema validity, the EXACT real-PMU measurement matrix (21 points), single-
supply current-PSRR, the in-situ bias policy (iload/dc only when the user supplies one),
pin-name traceability, the golden pmu_real.json template, and write_manifest round-trip.
"""
import json
import sys
import pathlib

import pytest

# put cadence/ on the path so `import insitu...` resolves like the package expects
CADENCE = pathlib.Path(__file__).resolve().parents[1]
if str(CADENCE) not in sys.path:
    sys.path.insert(0, str(CADENCE))

import insitu                              # noqa: E402  (exposes MANIFEST_DIR)
from insitu import manifest as M           # noqa: E402
from insitu import build_manifest as B     # noqa: E402
from insitu.build_manifest import BuildError  # noqa: E402

MANIFEST_DIR = insitu.MANIFEST_DIR


# ---------------------------------------------------------------------------
# the EXACT GUI inputs for the real PMU (3 v_out, 3 i_out, 1 supply)
# ---------------------------------------------------------------------------
def real_gui(**over):
    gui = dict(
        name="pmu_real",
        tb_lib="PMU_lib", tb_cell="PMU_TB", tb_view="schematic", dut_inst="I0",
        dut_lib="PMU_lib", dut_cell="PMU_top",
        supply={"pin": "AVDD1P0", "dc": 1.0},
        v_outs=["VDD0P8_DIG", "VDD0P8_PLL", "VDD0P8_VCO"],
        i_outs=["IBP_POLY_1P8U_VCO", "IBP_POLY_500N_VCO_Fit", "IBP_PTAT_TUNE_1P5U_VCO"],
        ground="VSS", corner="tt_25c",
    )
    gui.update(over)
    return gui


def real_netmap(gui=None):
    gui = gui or real_gui()
    pins = [gui["supply"]["pin"], *gui["v_outs"], *gui["i_outs"]]
    return {p: f"net_{p}" for p in pins}


@pytest.fixture
def built():
    gui = real_gui()
    return B.build_manifest(gui, real_netmap(gui))


# ---------------------------------------------------------------------------
# core contract: validate() accepts + measurements() yields the right matrix
# ---------------------------------------------------------------------------
def test_validate_accepts(built):
    assert M.validate(built) is True


def test_measurement_matrix_count(built):
    meas = M.measurements(built)
    keys = [x["key"] for x in meas]
    n_z = sum(1 for k in keys if k.startswith("z_"))
    n_couple = sum(1 for k in keys if k.startswith("couple_"))
    n_noise = sum(1 for k in keys if k.startswith("noise_"))
    n_psrr = sum(1 for k in keys if k.startswith("p_"))
    n_y = sum(1 for k in keys if k.startswith("y_"))
    n_pi = sum(1 for k in keys if k.startswith("pi_"))
    assert n_z == 3, "3 Zout (one per v_out rail)"
    assert n_couple == 6, "3 outs x 2 other outs"
    assert n_noise == 3, "3 output-noise points"
    assert n_psrr == 3, "3 outs x 1 supply"
    assert n_y == 3, "3 sink admittances"
    assert n_pi == 3, "3 sinks x 1 supply (single-supply current-PSRR)"
    assert n_z + n_couple + n_noise + n_psrr + n_y + n_pi == 21
    assert len(meas) == 21
    # tags are unique (no point silently collides)
    assert len({x["tag"] for x in meas}) == 21


def test_single_supply_current_psrr(built):
    assert built["current_psrr_supplies"] == ["avdd1p0"]
    assert list(built["supplies"]) == ["avdd1p0"]
    assert built["supplies"]["avdd1p0"]["dc"] == 1.0
    # exactly one supply means current-PSRR (pi_*) is measured only against it
    pis = [x for x in M.measurements(built) if x["key"].startswith("pi_")]
    assert all(x["tag"].endswith("_avdd1p0") for x in pis)


# ---------------------------------------------------------------------------
# role-key derivation + pin traceability
# ---------------------------------------------------------------------------
def test_role_keys(built):
    assert list(built["v_out"]) == ["dig", "pll", "vco"]
    # i_out keyed on the magnitude token
    assert set(built["i_out"]) == {"i1p8u", "i500n", "i1p5u"}
    assert list(built["supplies"]) == ["avdd1p0"]


def test_pin_traceability(built):
    assert built["supplies"]["avdd1p0"]["pin"] == "AVDD1P0"
    assert built["v_out"]["dig"]["pin"] == "VDD0P8_DIG"
    assert built["v_out"]["pll"]["pin"] == "VDD0P8_PLL"
    assert built["v_out"]["vco"]["pin"] == "VDD0P8_VCO"
    assert built["i_out"]["i500n"]["pin"] == "IBP_POLY_500N_VCO_Fit"
    assert built["i_out"]["i1p8u"]["pin"] == "IBP_POLY_1P8U_VCO"
    assert built["i_out"]["i1p5u"]["pin"] == "IBP_PTAT_TUNE_1P5U_VCO"


def test_nets_from_resolver(built):
    assert built["supplies"]["avdd1p0"]["net"] == "net_AVDD1P0"
    assert built["v_out"]["vco"]["net"] == "net_VDD0P8_VCO"
    assert built["i_out"]["i1p5u"]["net"] == "net_IBP_PTAT_TUNE_1P5U_VCO"


def test_probe_sources_stable(built):
    # importmp/augment both derive the probe name from the role key; check it's there
    assert built["i_out"]["i500n"]["probe_src"] == "Vprobe_i500n"
    assert built["i_out"]["i1p8u"]["probe_src"] == "Vprobe_i1p8u"


# ---------------------------------------------------------------------------
# in-situ bias policy: iload/dc only when the user supplies a value
# ---------------------------------------------------------------------------
def test_no_iload_no_vdc_by_default(built):
    for v in built["v_out"].values():
        assert "iload" not in v, "no iload unless the user supplies one (TB load biases)"
    # validate() defaults i_out.dc=0.0 for derivation, but we never WROTE a user dc:
    # the proof is that build_manifest itself emitted no dc -- check on a fresh build
    gui = real_gui()
    m_pre = _build_without_validate(gui, real_netmap(gui))
    for c in m_pre["i_out"].values():
        assert "dc" not in c, "no forced dc unless user supplies one (live source sets OP)"


def test_iload_and_vdc_passthrough_when_given():
    gui = real_gui(
        iload={"VDD0P8_DIG": 1e-3, "VDD0P8_PLL": 500e-6},
        vdc={"IBP_POLY_500N_VCO_Fit": 0.4},
    )
    m = B.build_manifest(gui, real_netmap(gui))
    assert m["v_out"]["dig"]["iload"] == 1e-3
    assert m["v_out"]["pll"]["iload"] == 500e-6
    assert "iload" not in m["v_out"]["vco"]      # not supplied -> omitted
    assert m["i_out"]["i500n"]["dc"] == 0.4
    # the other sinks got validate()'s default 0.0, but no USER value -> verify pre-validate
    m_pre = _build_without_validate(gui, real_netmap(gui))
    assert m_pre["i_out"]["i500n"]["dc"] == 0.4
    assert "dc" not in m_pre["i_out"]["i1p8u"]
    assert "dc" not in m_pre["i_out"]["i1p5u"]


def _build_without_validate(gui, netmap):
    """Re-run the builder body but skip the final validate() so we can observe what
    build_manifest actually EMITTED (validate mutates i_out.dc in place)."""
    import unittest.mock as mock
    with mock.patch.object(B._manifest, "validate", return_value=True):
        return B.build_manifest(gui, netmap)


def test_zero_iload_and_vdc_are_emitted():
    """0.0 is a DELIBERATE user value (is-not-None, not truthiness) -> it must survive,
    so a future refactor to a truthiness check would break this and get caught."""
    gui = real_gui(iload={"VDD0P8_DIG": 0.0}, vdc={"IBP_POLY_500N_VCO_Fit": 0.0})
    m_pre = _build_without_validate(gui, real_netmap(gui))
    assert m_pre["v_out"]["dig"]["iload"] == 0.0
    assert m_pre["i_out"]["i500n"]["dc"] == 0.0


def test_warns_on_missing_current_compliance(built):
    """Current outputs without a supplied vdc -> recorded in m['_warnings'] (validate
    will clamp dc=0.0, almost never the real OP)."""
    assert "_warnings" in built
    w = " ".join(built["_warnings"])
    for key in ("i1p8u", "i500n", "i1p5u"):
        assert key in w
    # a fully-supplied set of compliances => no warning
    gui = real_gui(vdc={"IBP_POLY_1P8U_VCO": 0.5, "IBP_POLY_500N_VCO_Fit": 0.4,
                        "IBP_PTAT_TUNE_1P5U_VCO": 0.45})
    assert "_warnings" not in B.build_manifest(gui, real_netmap(gui))


def test_unresolved_net_rejected():
    """The B resolver's '<unresolved>' marker is truthy but must NOT leak into the manifest
    -> a hard BuildError naming the pin (not a silent bogus net / 'tagged twice' red herring)."""
    from insitu.resolve import UNRESOLVED
    gui = real_gui()
    nm = real_netmap(gui)
    nm["VDD0P8_PLL"] = UNRESOLVED
    with pytest.raises(BuildError) as ei:
        B.build_manifest(gui, nm)
    assert "VDD0P8_PLL" in str(ei.value) and "unresolved" in str(ei.value).lower()


def test_supply_dc_none_rejected():
    """supply.dc = None must be rejected (key-presence alone passed it before)."""
    gui = real_gui(supply={"pin": "AVDD1P0", "dc": None})
    with pytest.raises(BuildError):
        B.build_manifest(gui, real_netmap(gui))


# ---------------------------------------------------------------------------
# held bias ports (optional)
# ---------------------------------------------------------------------------
def test_optional_biases():
    gui = real_gui(biases={"VREF_1P0": 1.0})
    nm = real_netmap(gui)
    nm["VREF_1P0"] = "net_VREF_1P0"
    m = B.build_manifest(gui, nm)
    assert m["bias"]["vref_1p0"] == {"net": "net_VREF_1P0", "dc": 1.0, "pin": "VREF_1P0"}
    assert M.validate(m) is True


# ---------------------------------------------------------------------------
# error paths
# ---------------------------------------------------------------------------
def test_missing_net_raises():
    gui = real_gui()
    nm = real_netmap(gui)
    nm.pop("VDD0P8_VCO")            # resolver gap
    with pytest.raises(BuildError):
        B.build_manifest(gui, nm)


def test_missing_supply_raises():
    gui = real_gui()
    gui["supply"] = {"dc": 1.0}    # no pin
    with pytest.raises(BuildError):
        B.build_manifest(gui, real_netmap())


def test_no_outputs_raises():
    gui = real_gui(v_outs=[], i_outs=[])
    with pytest.raises(BuildError):
        B.build_manifest(gui, real_netmap(gui))


def test_required_dut_fields():
    gui = real_gui()
    del gui["dut_cell"]
    with pytest.raises(BuildError):
        B.build_manifest(gui, real_netmap())


def test_duplicate_role_key_disambiguated():
    # two pins that slug to the same v_out key must not collide-merge
    gui = real_gui(v_outs=["VDD0P8_DIG", "VOUT_DIG"])
    nm = real_netmap()
    nm["VOUT_DIG"] = "net_VOUT_DIG"
    m = B.build_manifest(gui, nm)
    assert len(m["v_out"]) == 2
    assert "dig" in m["v_out"] and "dig2" in m["v_out"]


# ---------------------------------------------------------------------------
# write_manifest round-trip + the shipped golden template
# ---------------------------------------------------------------------------
def test_write_manifest_roundtrip(tmp_path, built):
    gui = real_gui()
    p = B.write_manifest(gui, real_netmap(gui), tmp_path / "out.json")
    assert p.exists()
    on_disk = json.loads(p.read_text())
    assert on_disk["name"] == "pmu_real"
    assert M.validate(on_disk) is True
    assert len(M.measurements(on_disk)) == 21


def test_golden_pmu_real_loads_and_validates():
    path = MANIFEST_DIR / "pmu_real.json"
    assert path.exists(), "shipped golden template must exist"
    m = M.load(path)               # load() also runs validate()
    assert m["name"] == "pmu_real"
    assert list(m["v_out"]) == ["dig", "pll", "vco"]
    assert set(m["i_out"]) == {"i1p8u", "i500n", "i1p5u"}
    assert m["current_psrr_supplies"] == ["avdd1p0"]
    assert len(M.measurements(m)) == 21
    # placeholder nets are clearly resolver-fill markers
    assert m["supplies"]["avdd1p0"]["net"].startswith("<net:")


def test_golden_matches_builder_output():
    """The shipped golden file should be structurally what build_manifest emits for the
    same pins (placeholder nets), so the template never drifts from the code."""
    gui = real_gui(
        name="pmu_real",
        tb_lib="<tb_lib>", tb_cell="<tb_cell>", dut_inst="<dut_inst>",
        dut_lib="<dut_lib>", dut_cell="<dut_cell>", corner="<corner>",
    )
    netmap = {p: f"<net:{p}>" for p in
              [gui["supply"]["pin"], *gui["v_outs"], *gui["i_outs"]]}
    built = B.build_manifest(gui, netmap)
    golden = M.load(MANIFEST_DIR / "pmu_real.json")
    # compare the load-bearing role structure (nets, keys, pins), ignoring cosmetic keys
    for role in ("v_out", "i_out", "supplies"):
        assert set(built[role]) == set(golden[role])
        for k in built[role]:
            assert built[role][k]["net"] == golden[role][k]["net"]
            assert built[role][k]["pin"] == golden[role][k]["pin"]
    assert built["current_psrr_supplies"] == golden["current_psrr_supplies"]


def test_user_defined_iv_sweep_and_temps():
    """The reusable knobs: per-i_out I-V knee sweep + temperature points flow into the
    manifest (and the nominal-temp default = middle of temps). Cross-project reuse."""
    nm = {p: f"net_{p}" for p in ["AVDD1P0", "VDD0P8_DIG", "VDD0P8_PLL", "VDD0P8_VCO",
                                  "IBP_POLY_1P8U_VCO", "IBP_POLY_500N_VCO_Fit",
                                  "IBP_PTAT_TUNE_1P5U_VCO"]}
    gui = real_gui(
        vdc={"IBP_POLY_1P8U_VCO": 0.5, "IBP_POLY_500N_VCO_Fit": 0.5,
             "IBP_PTAT_TUNE_1P5U_VCO": 0.5},
        iv_sweep={"IBP_POLY_1P8U_VCO": [0.0, 1.1, 0.01],
                  "IBP_PTAT_TUNE_1P5U_VCO": "auto"},
        temps=[-40, 55, 125],
    )
    m = B.build_manifest(gui, nm)
    assert m["temps"] == [-40.0, 55.0, 125.0]
    assert m["tnom_c"] == 55.0                          # middle point = nominal
    i1p8 = next(c for c in m["i_out"].values() if c["pin"] == "IBP_POLY_1P8U_VCO")
    iptat = next(c for c in m["i_out"].values() if c["pin"] == "IBP_PTAT_TUNE_1P5U_VCO")
    assert i1p8["iv_sweep"] == [0.0, 1.1, 0.01]
    assert iptat["iv_sweep"] == "auto"
    # i_out without an iv_sweep entry carries none (single-OP); manifest still valid
    i500 = next(c for c in m["i_out"].values() if c["pin"] == "IBP_POLY_500N_VCO_Fit")
    assert "iv_sweep" not in i500
    M.validate(m)


def test_explicit_tnom_overrides_middle():
    nm = {p: f"net_{p}" for p in ["AVDD1P0", "VDD0P8_DIG", "VDD0P8_PLL", "VDD0P8_VCO",
                                  "IBP_POLY_1P8U_VCO", "IBP_POLY_500N_VCO_Fit",
                                  "IBP_PTAT_TUNE_1P5U_VCO"]}
    m = B.build_manifest(real_gui(temps=[-40, 27, 125], tnom_c=27), nm)
    assert m["tnom_c"] == 27.0


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
