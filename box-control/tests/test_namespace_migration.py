from pathlib import Path
import re
import tomllib

import zog.box_control as box_control


def test_canonical_namespace_and_version():
    assert box_control.__name__ == "zog.box_control"
    assert box_control.__version__ == "0.1.0"


def test_no_legacy_top_level_package_layout():
    root = Path(__file__).resolve().parents[1]
    assert (root / "src" / "zog" / "box_control" / "__init__.py").is_file()
    assert not (root / "src" / "zog" / "__init__.py").exists()
    assert not (root / "box_control").exists()
    assert not (root / "root_control").exists()


def test_no_legacy_python_import_paths():
    root = Path(__file__).resolve().parents[1]
    patterns = (
        re.compile(r"\bfrom\s+box_control\b"),
        re.compile(r"\bimport\s+box_control\b"),
        re.compile(r"(?P<q>['\"])box_control\."),
        re.compile(r"\bfrom\s+root_control\b"),
        re.compile(r"\bimport\s+root_control\b"),
        re.compile(r"(?P<q>['\"])root_control\."),
        re.compile(r"\bfrom\s+image_build\b"),
        re.compile(r"\bimport\s+image_build\b"),
        re.compile(r"(?P<q>['\"])image_build\."),
    )
    offenders = []
    for base in (root / "src", root / "tests", root.parent / "acceptance"):
        if not base.exists():
            continue
        for path in base.rglob("*.py"):
            if path == Path(__file__):
                continue
            text = path.read_text(encoding="utf-8")
            if any(pattern.search(text) for pattern in patterns):
                offenders.append(str(path.relative_to(root.parent)))
    assert offenders == []


def test_box_control_source_does_not_import_root_control_implementation():
    root = Path(__file__).resolve().parents[1] / "src" / "zog" / "box_control"
    offenders = []
    for path in root.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        if "zog.root_control" in text or "zog.box_control.root_control" in text:
            offenders.append(str(path.relative_to(root)))
    assert offenders == []


def test_pyproject_does_not_package_root_control():
    root = Path(__file__).resolve().parents[1]
    metadata = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    assert metadata["project"]["scripts"] == {
        "box-control": "zog.box_control.cli:main",
    }
    assert "package-data" not in metadata.get("tool", {}).get("setuptools", {})
    assert "zog.box_control.root_control" not in (root / "pyproject.toml").read_text(encoding="utf-8")
