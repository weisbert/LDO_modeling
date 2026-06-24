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


def test_probe_src_absent_by_default(built):
    # REUSE model: the builder no longer FABRICATES a probe_src (that would name a non-existent
    # source). Absent -> the netlister auto-detects the vdc on the net or falls back to inserting
    # Vprobe_<key>; the read derives the SAME name from the role key via manifest._probe_name.
    for c in built["i_out"]:
        assert "probe_src" not in built["i_out"][c]
        assert M._probe_name(built, c) == f"Vprobe_{c}"          # the fallback-insert name


def test_src_passthrough_when_gui_supplies_it():
    # when the GUI names the existing TB sources to reuse, they pass through to src/probe_src
    gui = dict(
        tb_lib="L", tb_cell="TB", dut_lib="L", dut_cell="D", ground="VSS",
        supply={"pin": "AVDD1P0", "dc": 1.0, "tb_src": "V_AVDD"},
        v_outs=["VDD0P8_PLL"], i_outs=["IBP_POLY_500N"],
        v_src={"VDD0P8_PLL": "Iload_pll"},
        i_src={"IBP_POLY_500N": "Vbias_500n"},
        vdc={"IBP_POLY_500N": 1.28})
    netmap = {p: p for p in ["AVDD1P0", "VDD0P8_PLL", "IBP_POLY_500N"]}
    m = B.build_manifest(gui, netmap)
    assert m["supplies"]["avdd1p0"]["tb_src"] == "V_AVDD"
    assert m["v_out"]["pll"]["src"] == "Iload_pll"
    assert m["i_out"]["i500n"]["probe_src"] == "Vbias_500n"


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
    # temps must land in the CONSUMED location (coverage.temps); a top-level m['temps'] is
    # invisible to manifest.temps()/pmu_corner -> the Extract-tab field would silently no-op.
    assert M.temps(m) == [-40.0, 55.0, 125.0]
    assert m.get("coverage", {}).get("temps") == [-40.0, 55.0, 125.0]
    assert m["tnom_c"] == 55.0                          # middle point = nominal
    i1p8 = next(c for c in m["i_out"].values() if c["pin"] == "IBP_POLY_1P8U_VCO")
    iptat = next(c for c in m["i_out"].values() if c["pin"] == "IBP_PTAT_TUNE_1P5U_VCO")
    assert i1p8["iv_sweep"] == [0.0, 1.1, 0.01]
    assert iptat["iv_sweep"] == "auto"
    # i_out without an iv_sweep entry carries none (single-OP); manifest still valid
    i500 = next(c for c in m["i_out"].values() if c["pin"] == "IBP_POLY_500N_VCO_Fit")
    assert "iv_sweep" not in i500
    M.validate(m)


def test_gui_inoise_flag_requests_current_noise():
    """The from-scratch GUI builder must be able to request current-output noise. inoise is
    OPT-IN (no tier auto-enables it), so build_manifest writes coverage.enable.inoise only when
    the GUI passes inoise=True -- and that ACTUALLY turns the measurement on (coverage_enabled +
    one noise_i_<sink> measurement per sink). Without the flag the key is ABSENT (not False)."""
    gui = real_gui(inoise=True)
    m = B.build_manifest(gui, real_netmap(gui))
    assert (m.get("coverage", {}).get("enable") or {}).get("inoise") is True
    assert M.coverage_enabled(m, "inoise") is True
    ni = [x for x in M.measurements(m) if x["key"].startswith("noise_i_")]
    assert len(ni) == 3, "one output-current-noise point per sink"
    assert all(x["analysis"] == "noise" and x.get("oprobe_src") for x in ni)
    # no flag -> the override is omitted entirely (so a future tier default is never masked)
    m_off = B.build_manifest(real_gui(), real_netmap())
    assert "inoise" not in ((m_off.get("coverage", {}).get("enable")) or {})
    assert M.coverage_enabled(m_off, "inoise") is False
    assert not [x for x in M.measurements(m_off) if x["key"].startswith("noise_i_")]


def test_gui_temps_range_syntax_expands():
    """The temps field accepts Cadence start:step:stop ranges; build_manifest expands them into
    the explicit coverage.temps list and sets tnom_c = median of the sorted grid. The stored list
    is plain numbers, so the manifest still validates (a string temps would be rejected)."""
    nm = real_netmap()
    m = B.build_manifest(real_gui(temps="-40:10:120, 125"), nm)
    tps = M.temps(m)
    assert tps[0] == -40.0 and tps[-1] == 125.0 and 120.0 in tps
    assert len(tps) == 18                              # 17-pt range (incl 120) + explicit 125
    assert m["tnom_c"] == tps[len(tps) // 2]           # median of the expanded+sorted grid
    assert M.validate(m) is True
    # a list input (the GUI pre-expands) is idempotent
    m2 = B.build_manifest(real_gui(temps=[-40, 55, 125]), nm)
    assert M.temps(m2) == [-40.0, 55.0, 125.0]


def test_explicit_tnom_overrides_middle():
    nm = {p: f"net_{p}" for p in ["AVDD1P0", "VDD0P8_DIG", "VDD0P8_PLL", "VDD0P8_VCO",
                                  "IBP_POLY_1P8U_VCO", "IBP_POLY_500N_VCO_Fit",
                                  "IBP_PTAT_TUNE_1P5U_VCO"]}
    m = B.build_manifest(real_gui(temps=[-40, 27, 125], tnom_c=27), nm)
    assert m["tnom_c"] == 27.0
    assert M.temps(m) == [-40.0, 27.0, 125.0]           # and the run axis is the consumed location


def test_summary_warns_on_ptat_without_temps():
    """A PTAT current ref characterized at a SINGLE temp is the defining gap (its current IS a
    temperature function). summary() must shout it -- and name the PTAT ref -- when i_out is
    present but coverage.temps is empty; the warning clears once temps are declared."""
    gui = real_gui()                                    # no temps -> single session temp
    m = B.build_manifest(gui, real_netmap(gui))
    assert M.temps(m) == []                             # nothing declared
    s = M.summary(m)
    assert "NO temperature corners" in s, s
    assert "ptat" in s.lower(), "the PTAT ref must be named in the temperature warning"
    # declare temps -> the warning disappears
    m2 = B.build_manifest(real_gui(temps=[-40, 25, 125]), real_netmap())
    assert "NO temperature corners" not in M.summary(m2)


def test_summary_no_temp_warning_without_current_refs():
    """No current outputs -> the temperature warning is irrelevant (Idc(T)/PTAT is a current-ref
    concern); a voltage-only manifest with no temps must NOT trigger it."""
    gui = real_gui(i_outs=[])
    m = B.build_manifest(gui, {p: f"net_{p}" for p in [gui["supply"]["pin"], *gui["v_outs"]]})
    assert "NO temperature corners" not in M.summary(m)


# ---------------------------------------------------------------------------
# per-object analysis override (OPTIONAL dict {ac?, noise?}) -- validated by manifest.validate
# ---------------------------------------------------------------------------
def _min_manifest():
    return {"name": "t", "dut": {"lib": "L", "cell": "C", "tb_lib": "TB", "tb_cell": "TBC"},
            "supplies": {"s": {"net": "VS", "dc": 1.0}},
            "v_out": {"o": {"net": "VO"}}, "i_out": {"c": {"net": "VI", "dc": 0.5}}, "bias": {}}


def test_per_object_analysis_valid_dict_passes():
    m = M._fill_defaults(_min_manifest())
    m["v_out"]["o"]["analysis"] = {"ac": "ac dec=10", "noise": "noise dec=10"}
    m["supplies"]["s"]["analysis"] = {"ac": "ac dec=5"}
    m["i_out"]["c"]["analysis"] = {"ac": "ac dec=5"}
    assert M.validate(m) is True
    assert M.analysis_line_for(m, "v_out", "o", "ac") == "ac dec=10"
    assert M.analysis_line_for(m, "v_out", "o", "noise") == "noise dec=10"
    # absent override -> global
    assert M.analysis_override(m, "i_out", "c", "noise") is None
    assert M.analysis_line_for(m, "i_out", "c", "ac") == "ac dec=5"


def test_per_object_analysis_non_dict_raises():
    m = M._fill_defaults(_min_manifest())
    m["v_out"]["o"]["analysis"] = "ac dec=10"             # must be a dict
    with pytest.raises(M.ManifestError):
        M.validate(m)


def test_per_object_analysis_unknown_key_raises():
    m = M._fill_defaults(_min_manifest())
    m["supplies"]["s"]["analysis"] = {"noise": "noise dec=10"}   # supply may carry only ac
    with pytest.raises(M.ManifestError):
        M.validate(m)


def test_per_object_analysis_non_string_value_raises():
    m = M._fill_defaults(_min_manifest())
    m["v_out"]["o"]["analysis"] = {"ac": 123}            # value must be a string line
    with pytest.raises(M.ManifestError):
        M.validate(m)


# ---------------------------------------------------------------------------
# multi-supply: gui['supplies'] = [{pin,dc}, ...]  (back-compat with single gui['supply'])
# ---------------------------------------------------------------------------
def multi_gui(**over):
    gui = real_gui()
    gui.pop("supply")
    gui["supplies"] = [{"pin": "AVDD1P0", "dc": 1.0}, {"pin": "AVDD1P8", "dc": 1.8}]
    gui.update(over)
    return gui


def multi_netmap(gui):
    pins = [*(s["pin"] for s in gui["supplies"]), *gui["v_outs"], *gui["i_outs"]]
    return {p: f"net_{p}" for p in pins}


def test_supplies_list_back_compat_single():
    """supplies=[one] must produce an IDENTICAL supplies/current_psrr block to supply=one."""
    g1 = real_gui()
    g2 = real_gui(); g2.pop("supply"); g2["supplies"] = [{"pin": "AVDD1P0", "dc": 1.0}]
    m1 = B.build_manifest(g1, real_netmap(g1))
    m2 = B.build_manifest(g2, real_netmap(g1))
    assert m1["supplies"] == m2["supplies"]
    assert m1["current_psrr_supplies"] == m2["current_psrr_supplies"] == ["avdd1p0"]


def test_two_supplies_keys():
    g = multi_gui()
    m = B.build_manifest(g, multi_netmap(g))
    assert list(m["supplies"]) == ["avdd1p0", "avdd1p8"]
    assert m["supplies"]["avdd1p8"]["dc"] == 1.8
    assert m["supplies"]["avdd1p8"]["pin"] == "AVDD1P8"
    assert M.validate(m) is True


def test_current_psrr_default_all_supplies():
    g = multi_gui()
    m = B.build_manifest(g, multi_netmap(g))
    assert m["current_psrr_supplies"] == ["avdd1p0", "avdd1p8"]   # default = ALL


def test_two_supplies_matrix_count():
    g = multi_gui()
    m = B.build_manifest(g, multi_netmap(g))
    keys = [x["key"] for x in M.measurements(m)]
    n_psrr = sum(1 for k in keys if k.startswith("p_"))
    n_pi = sum(1 for k in keys if k.startswith("pi_"))
    assert n_psrr == 6, "3 v_out x 2 supplies"
    assert n_pi == 6, "3 sinks x 2 cpsrr supplies (default all)"
    # z(3) + couple(6) + noise(3) + psrr(6) + y(3) + pi(6) = 27
    assert len(M.measurements(m)) == 27


def test_current_psrr_subset_override_by_pin():
    g = multi_gui(current_psrr_supplies=["AVDD1P0"])     # a PIN name -> role key
    m = B.build_manifest(g, multi_netmap(g))
    assert m["current_psrr_supplies"] == ["avdd1p0"]
    n_pi = sum(1 for k in (x["key"] for x in M.measurements(m)) if k.startswith("pi_"))
    assert n_pi == 3, "current-PSRR only vs the one chosen supply"


def test_current_psrr_unknown_supply_rejected():
    g = multi_gui(current_psrr_supplies=["NOPE"])
    with pytest.raises(BuildError):
        B.build_manifest(g, multi_netmap(g))


def test_both_supply_forms_rejected():
    g = real_gui(); g["supplies"] = [{"pin": "AVDD1P0", "dc": 1.0}]   # supply AND supplies
    with pytest.raises(BuildError):
        B.build_manifest(g, real_netmap())


def test_duplicate_supply_pin_rejected():
    g = multi_gui()
    g["supplies"] = [{"pin": "AVDD1P0", "dc": 1.0}, {"pin": "AVDD1P0", "dc": 1.0}]
    with pytest.raises(BuildError):
        B.build_manifest(g, multi_netmap(g))


def test_per_supply_dc_required():
    g = multi_gui()
    g["supplies"][1].pop("dc")
    with pytest.raises(BuildError):
        B.build_manifest(g, multi_netmap(g))


def test_multi_supply_warns_psrr_only_first():
    """N>1 supplies: a LOUD _warnings line says PSRR is emitted only vs the first supply
    (the model exposes one input port) -- not silently dropped."""
    g = multi_gui(vdc={"IBP_POLY_1P8U_VCO": 0.5, "IBP_POLY_500N_VCO_Fit": 0.4,
                       "IBP_PTAT_TUNE_1P5U_VCO": 0.45})       # no compliance warning to confuse it
    m = B.build_manifest(g, multi_netmap(g))
    w = " ".join(m.get("_warnings", []))
    assert "avdd1p0" in w and "avdd1p8" in w and "PSRR" in w
    # single supply -> no such warning
    g1 = real_gui(vdc={"IBP_POLY_1P8U_VCO": 0.5, "IBP_POLY_500N_VCO_Fit": 0.4,
                       "IBP_PTAT_TUNE_1P5U_VCO": 0.45})
    assert "_warnings" not in B.build_manifest(g1, real_netmap(g1))


def test_cpsrr_dedup_pin_and_slug():
    """A pin plus its own slug must collapse to ONE current_psrr supply (no duplicate pi_*)."""
    g = multi_gui(current_psrr_supplies=["AVDD1P0", "avdd1p0"])
    m = B.build_manifest(g, multi_netmap(g))
    assert m["current_psrr_supplies"] == ["avdd1p0"]
    n_pi = sum(1 for k in (x["key"] for x in M.measurements(m)) if k.startswith("pi_"))
    assert n_pi == 3, "no duplicate current-PSRR points"


def test_supply_role_key_collision_disambiguated():
    """Two distinct pins that slug to the SAME key get suffixed (avdd1p0 / avdd1p02),
    never silently merged -- same protection v_out/i_out already have."""
    g = multi_gui()
    g["supplies"] = [{"pin": "AVDD1P0", "dc": 1.0}, {"pin": "AVDD1P0#", "dc": 1.0}]
    nm = multi_netmap(g)
    m = B.build_manifest(g, nm)
    assert list(m["supplies"]) == ["avdd1p0", "avdd1p02"]


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
