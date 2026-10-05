"""Pure tests for generated runtime profile goldens."""
from __future__ import annotations

import re
from dataclasses import fields, is_dataclass, replace

import pytest

import spec.types as spec_types
from spec.profiles.ijai import IJAI_V17
from spec.registry import MODEL_PROFILES

try:
    from scripts.specs import generate_runtime_specs, promote_profiles
except ModuleNotFoundError:
    generate_runtime_specs = None
    promote_profiles = None

_PROMOTED_BRANDS = ("dreame", "viomi")
_HAS_SPEC_LIBRARY = (
    generate_runtime_specs is not None
    and any(generate_runtime_specs.LIBRARY_DIR.glob("*.json"))
)
# Hand differences from the generated baseline: (profile_id, capability) -> (fields, reason).
# Capability None covers the whole profile; empty fields cover the whole capability.
_DEVIATIONS: dict[tuple[str, str | None], tuple[tuple[str, ...], str]] = {
    ("ijai.v17", "consumables"): (
        ("side_brush_life", "main_brush_life", "hypa_life", "mop_life"),
        "hardware-verified consumable life props, beyond the generator's mapping (914f44d)",
    ),
    ("dreame.p2149o", "core"): (("fan_speeds",), "camelCase spec labels (ModeQuiet) split by hand (0344c89)"),
    ("dreame.r2211o", "core"): (("fan_speeds",), "camelCase spec labels (ModeQuiet) split by hand (0344c89)"),
    ("xiaomi.c107", "core"): (("status_map",), "keeps ov21gl's codes 22-24 its spec doesn't declare (d6f017f)"),
    ("xiaomi.d102ev", "core"): (("status_map",), "keeps ov21gl's codes 22-24 its spec doesn't declare (d6f017f)"),
    ("xiaomi.d102gl", "core"): (("status_map",), "keeps ov21gl's codes 22-24 its spec doesn't declare (d6f017f)"),
    ("xiaomi.d109gl", "core"): (("status_map",), "keeps ov21gl's codes 22-24 its spec doesn't declare (d6f017f)"),
    ("xiaomi.ov21gl", "consumables"): ((), "hardware-confirmed by an ov21gl owner, mop pad not detergent (5e5076a)"),
    ("xiaomi.pv21cn", "core"): (("status_map",), "status 25 SelfChecking and 26 Summoning mapped by hand, not in the generator's table"),
    ("xiaomi.ov31gl", None): ((), "no spec in library"),
    ("xiaomi.ov42gl", None): ((), "no spec in library"),
}


def _exec_profiles(source: str) -> dict:
    """Execute a generated or promoted profile module against spec.types.

    @param source: module source text.
    @returns: the module's globals.
    """
    scope = {name: getattr(spec_types, name) for name in dir(spec_types) if name[:1].isupper()}
    exec(compile(re.sub(r"from \.+types import \([^)]*\)", "", source), "<generated>", "exec"), scope)
    return scope


def _cleared(profile, profile_id: str):
    """Clear the listed deviations for profile_id plus profile_id, notes and max_properties.

    @param profile: registered or generated ModelProfile.
    @param profile_id: registered profile id whose deviations apply.
    @returns: the cleared ModelProfile.
    """
    for (pid, attr), (names, _reason) in _DEVIATIONS.items():
        if pid != profile_id or attr is None:
            continue
        cap = getattr(profile, attr)
        cleared = replace(cap, **dict.fromkeys(names)) if names and cap is not None else None
        profile = replace(profile, **{attr: cleared})
    return replace(profile, profile_id="", notes=(), max_properties=None)


def _differences(model: str, profile, baseline) -> list[str]:
    """List each capability where profile and baseline differ once deviations are cleared.

    @param model: model id the profile is registered under.
    @param profile: registered ModelProfile.
    @param baseline: generated or promoted ModelProfile for the same model.
    @returns: one "model (profile_id): capability [fields]" line per differing capability.
    """
    ours, theirs = _cleared(profile, profile.profile_id), _cleared(baseline, profile.profile_id)
    lines = []
    for f in fields(ours):
        mine, base = getattr(ours, f.name), getattr(theirs, f.name)
        if mine == base:
            continue
        sub = (
            [g.name for g in fields(mine) if getattr(mine, g.name) != getattr(base, g.name)]
            if is_dataclass(mine) and type(mine) is type(base)
            else []
        )
        lines.append(f"{model} ({profile.profile_id}): {f.name} {sub}")
    return lines


@pytest.fixture(scope="module")
def generated_modules() -> dict[str, str]:
    """Generate every brand's draft module from the spec library.

    @returns: brand -> draft module source.
    """
    if not _HAS_SPEC_LIBRARY:
        pytest.skip("private generator tools or raw MIoT spec library not present in this checkout")
    library = generate_runtime_specs.index_library(generate_runtime_specs.LIBRARY_DIR)
    return {brand: generate_runtime_specs.emit_brand_module(brand, indexes) for brand, indexes in library.items()}


def test_registry_profiles_match_generated_baseline(generated_modules) -> None:
    generated = {}
    for source in generated_modules.values():
        generated.update(_exec_profiles(source)["DRAFT_PROFILES"])

    differences = []
    for model, profile in MODEL_PROFILES.items():
        if (profile.profile_id, None) in _DEVIATIONS:
            continue
        if model not in generated:
            differences.append(f"{model} ({profile.profile_id}): no generated profile")
            continue
        differences += _differences(model, profile, generated[model])

    assert not differences, "\n".join(differences)


@pytest.mark.parametrize("brand", _PROMOTED_BRANDS)
def test_promoter_recreates_registered_profiles(generated_modules, brand: str) -> None:
    module, registry = promote_profiles.promote(brand, generated_modules[brand])
    promoted = _exec_profiles(module)

    differences = []
    for model, constant in registry:
        if model in MODEL_PROFILES:
            differences += _differences(model, MODEL_PROFILES[model], promoted[constant])
    assert not differences, "\n".join(differences)


def test_v17_core_keeps_spec_accurate_labels() -> None:
    assert "slient" in IJAI_V17.core.fan_speeds
    assert "sweep_and_mop" in IJAI_V17.core.modes
