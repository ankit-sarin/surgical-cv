from pathlib import Path

from pipeline.paths import NasPaths, resolve_paths


def test_default_root_is_mnt_nas(monkeypatch):
    monkeypatch.delenv("PIPELINE_NAS_ROOT", raising=False)
    p = resolve_paths()
    assert p.root == Path("/mnt/nas")
    assert p.or_raw == Path("/mnt/nas/or-raw")


def test_env_var_supplies_root_when_no_arg(monkeypatch):
    monkeypatch.setenv("PIPELINE_NAS_ROOT", "/srv/nas")
    p = resolve_paths()
    assert p.root == Path("/srv/nas")
    assert p.or_raw == Path("/srv/nas/or-raw")


def test_explicit_arg_overrides_env_var(monkeypatch):
    monkeypatch.setenv("PIPELINE_NAS_ROOT", "/srv/nas")
    p = resolve_paths("/elsewhere")
    assert p.root == Path("/elsewhere")
    assert p.or_raw == Path("/elsewhere/or-raw")


def test_all_derived_paths_from_custom_root():
    p = resolve_paths("/custom")
    assert p.state_csv == Path("/custom/or-raw/pipeline_state.csv")
    assert p.manifest_csv == Path("/custom/or-raw/case_manifest.csv")
    assert p.audit_log == Path("/custom/or-raw/pipeline.log")


def test_raw_dir_and_deid_dir_interpolate_surgeon():
    p = resolve_paths("/custom")
    assert p.raw_dir("sarin") == Path("/custom/raw-sarin")
    assert p.deid_dir("noren") == Path("/custom/deid-noren")
    assert p.raw_dir("miller") == Path("/custom/raw-miller")


def test_resolve_paths_creates_no_directories(tmp_path):
    root = tmp_path / "nas-not-yet"
    assert not root.exists()
    p = resolve_paths(root)
    assert p.root == root
    assert not root.exists()
    assert not p.or_raw.exists()
    assert not p.state_csv.exists()


def test_nas_paths_is_frozen():
    p = resolve_paths("/x")
    try:
        p.root = Path("/y")  # type: ignore[misc]
    except Exception:
        return
    raise AssertionError("NasPaths should be frozen")


def test_path_or_string_root_both_accepted():
    p1 = resolve_paths("/x")
    p2 = resolve_paths(Path("/x"))
    assert p1 == p2
