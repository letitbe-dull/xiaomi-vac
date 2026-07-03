"""Pins the E5 finding: dreame's settings/audio fields duplicate core.*.

DreameSettingsCapability.cleaning_mode/mop_mode and DreameAudioCapability.locate
mirror core.fan_speed/core.water_level/core.locate in every dreame profile. The
runtime intentionally drives fan/water/locate via core.* only (see docstrings on
those dataclasses in spec/types.py) — this test makes that claim self-verifying
against future spec regenerations.
"""
from __future__ import annotations

import pytest

from spec.registry import MODEL_PROFILES

_DREAME_PROFILES = sorted(
    (model, profile) for model, profile in MODEL_PROFILES.items() if profile.brand == "dreame"
)


@pytest.mark.parametrize(("model", "profile"), _DREAME_PROFILES)
def test_settings_modes_mirror_core(model: str, profile) -> None:
    settings = profile.settings
    if settings is None:
        return
    if settings.cleaning_mode is not None:
        assert settings.cleaning_mode == profile.core.fan_speed
    if settings.mop_mode is not None:
        assert settings.mop_mode == profile.core.water_level


@pytest.mark.parametrize(("model", "profile"), _DREAME_PROFILES)
def test_voice_locate_mirrors_core(model: str, profile) -> None:
    voice = profile.voice
    if voice is None or voice.locate is None:
        return
    assert voice.locate == profile.core.locate
