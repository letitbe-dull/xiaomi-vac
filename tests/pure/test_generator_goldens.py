"""Pure tests for generated runtime profile goldens."""
from __future__ import annotations

import re
from dataclasses import replace
from pathlib import Path

import pytest

import spec.types as spec_types
from spec.profiles.ijai import IJAI_V17
from spec.registry import MODEL_PROFILES
from tools.specs import generate_runtime_specs, promote_profiles

_BRANDS = ("dreame", "ijai", "roidmi", "viomi", "xiaomi")
_PROMOTED_BRANDS = ("dreame", "viomi")
_REPO_ROOT = Path(__file__).resolve().parents[2]
_DRAFTS = _REPO_ROOT / "custom_components" / "xiaomi_vac" / "spec" / "profiles" / "_drafts"
_REVIEWED = _REPO_ROOT / "custom_components" / "xiaomi_vac" / "spec" / "profiles"
_HAS_SPEC_LIBRARY = any(generate_runtime_specs.LIBRARY_DIR.glob("*.json"))


def _normalize(text: str) -> str:
    return text.replace("\r\n", "\n").strip()


@pytest.mark.parametrize("brand", _BRANDS)
def test_generator_recreates_committed_draft_modules(tmp_path, brand: str) -> None:
    if not _HAS_SPEC_LIBRARY:
        pytest.skip("raw MIoT spec library is not present in this checkout")

    assert generate_runtime_specs.main(["--brand", brand, "--out", str(tmp_path)]) == 0

    generated = (tmp_path / f"{brand}.py").read_text(encoding="utf-8")
    committed = (_DRAFTS / f"{brand}.py").read_text(encoding="utf-8")

    assert _normalize(generated) == _normalize(committed)


@pytest.mark.parametrize("brand", _PROMOTED_BRANDS)
def test_promoter_recreates_reviewed_modules(brand: str) -> None:
    module, registry = promote_profiles.promote(brand)
    reviewed = (_REVIEWED / f"{brand}.py").read_text(encoding="utf-8")

    assert _normalize(module) == _normalize(reviewed)
    assert {model for model, _constant in registry} <= set(MODEL_PROFILES)


def _load_draft_profiles(brand: str):
    src = (_DRAFTS / f"{brand}.py").read_text(encoding="utf-8")
    src = re.sub(r"from \.\.\.types import \([^)]*\)", "", src)
    scope = {name: getattr(spec_types, name) for name in dir(spec_types) if name[:1].isupper()}
    exec(compile(src, str(_DRAFTS / f"{brand}.py"), "exec"), scope)
    return scope["DRAFT_PROFILES"]


def test_registry_profiles_are_value_equal_to_generated_drafts() -> None:
    drafts = {}
    for brand in _BRANDS:
        drafts.update(_load_draft_profiles(brand))

    for model, profile in MODEL_PROFILES.items():
        assert model in drafts
        assert replace(profile, profile_id="") == replace(drafts[model], profile_id="")


def test_v17_core_keeps_spec_accurate_labels() -> None:
    assert "slient" in IJAI_V17.core.fan_speeds
    assert "sweep_and_mop" in IJAI_V17.core.modes
