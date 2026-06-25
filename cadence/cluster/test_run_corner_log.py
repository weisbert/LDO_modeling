"""run_corner.find_alps_log / alps_log_tail -- the engine-log discovery that turns a useless
'job FAILED' into the REAL abort reason. Best-effort + read-only: globs *.log/*.warn/*.out/
logFile/CDS.log across the netlist + PSF dirs, picks the freshest, returns its tail. Surfaced
inline on failure (pmu_corner) and via the GUI 'Open ALPS log' right-click."""
import os
import pathlib
import sys

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))                       # .../cadence on sys.path

from cluster import run_corner as RC                        # noqa: E402


def test_find_alps_log_picks_freshest_across_dirs(tmp_path):
    nd = tmp_path / "netlist"; nd.mkdir()
    pd = tmp_path / "psf"; pd.mkdir()
    old = nd / "psf.log"; old.write_text("Reading netlist...\nminor note\n")
    os.utime(old, (1000, 1000))                             # older
    new = pd / "psf.warn"; new.write_text("ERROR: parse error near 'oprobe'\n")
    os.utime(new, (2000, 2000))                             # newer -> wins by mtime
    f = RC.find_alps_log(str(nd), str(pd))
    assert f is not None and f.name == "psf.warn", "the freshest log across both dirs must win"


def test_alps_log_tail_returns_last_lines(tmp_path):
    d = tmp_path / "run"; d.mkdir()
    (d / "alps.log").write_text("\n".join(f"line{i}" for i in range(100)))
    f, tail = RC.alps_log_tail(str(d), lines=5)
    assert f.name == "alps.log"
    assert tail.splitlines() == ["line95", "line96", "line97", "line98", "line99"]


def test_find_alps_log_globs_all_known_log_kinds(tmp_path):
    d = tmp_path / "run"; d.mkdir()
    for i, name in enumerate(("CDS.log", "spectre.out", "logFile")):
        p = d / name; p.write_text(f"log {name}\n"); os.utime(p, (1000 + i, 1000 + i))
    f = RC.find_alps_log(str(d))
    assert f is not None and f.name == "logFile", "logFile (freshest here) must be discoverable"


def test_find_alps_log_none_when_absent_or_missing_dir(tmp_path):
    assert RC.find_alps_log(str(tmp_path), None, "/no/such/dir") is None    # empty + None + missing
    assert RC.alps_log_tail(str(tmp_path)) == (None, "")                    # graceful empty tuple


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-q"]))
