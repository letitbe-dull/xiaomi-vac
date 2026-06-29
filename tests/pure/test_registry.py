"""Pure registry/spec integrity tests."""
from __future__ import annotations

import pytest

from spec.registry import MODEL_PROFILES, card_baseline_gaps, get_profile, is_supported

_ACTIVITIES = {"cleaning", "paused", "idle", "returning", "docked", "error"}
_VALUE_TABLES = ("fan_speeds", "water_levels", "modes", "sweep_types")


@pytest.mark.parametrize("model", sorted(MODEL_PROFILES))
def test_every_registered_model_resolves(model: str) -> None:
    assert get_profile(model) is MODEL_PROFILES[model]


def test_unknown_model_resolves_to_none_and_is_not_supported() -> None:
    assert get_profile("roborock.vacuum.a01") is None
    assert is_supported("roborock.vacuum.a01") is False


def test_registry_counts_match_card_baseline() -> None:
    supported = [model for model in MODEL_PROFILES if is_supported(model)]
    rejected = [model for model in MODEL_PROFILES if not is_supported(model)]

    assert len(MODEL_PROFILES) == 83
    assert len(supported) == 65
    assert len(rejected) == 18


def test_distinct_core_count_matches_promoted_profiles() -> None:
    cores = {repr(profile.core) for profile in MODEL_PROFILES.values() if profile.core}

    assert len(cores) == 21


@pytest.mark.parametrize(("model", "profile"), sorted(MODEL_PROFILES.items()))
def test_core_policy_for_registered_models(model: str, profile) -> None:
    if model.startswith("roidmi."):
        assert profile.core is None
        assert card_baseline_gaps(profile) == ("core",)
        assert is_supported(model) is False
        return

    assert profile.core is not None


@pytest.mark.parametrize(("model", "profile"), sorted(MODEL_PROFILES.items()))
def test_support_gate_matches_card_baseline(model: str, profile) -> None:
    assert is_supported(model) is (not card_baseline_gaps(profile))


@pytest.mark.parametrize(("model", "profile"), sorted(MODEL_PROFILES.items()))
def test_value_tables_are_unambiguous(model: str, profile) -> None:
    core = profile.core
    if core is None:
        return

    for table_name in _VALUE_TABLES:
        table = getattr(core, table_name)
        if not table:
            continue
        assert all(isinstance(key, str) and key for key in table), (model, table_name)
        assert all(isinstance(value, int) for value in table.values()), (
            model,
            table_name,
        )
        assert len(set(table.values())) == len(table), (model, table_name)


@pytest.mark.parametrize(("model", "profile"), sorted(MODEL_PROFILES.items()))
def test_status_maps_use_home_assistant_activities(model: str, profile) -> None:
    core = profile.core
    if core is None:
        return

    assert core.status_map, model
    assert set(core.status_map.values()) <= _ACTIVITIES
