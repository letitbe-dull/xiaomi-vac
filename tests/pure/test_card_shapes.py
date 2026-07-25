"""Pure tests for the bundled card's MODEL_SHAPE table.

The card looks up `MODEL_SHAPE[modelShort(model)]` and silently falls back to
shape 1 on a miss, so a malformed key is invisible at runtime.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from spec.registry import MODEL_PROFILES, is_supported

_ROOT = Path(__file__).resolve().parents[2]
_WWW = _ROOT / "custom_components" / "xiaomi_vac" / "www"
_CARD = _WWW / "xiaomi-vac-card.js"
_LOTTIE = _WWW / "lottie"

_STATES = ("charging", "paused", "returning", "vacuuming")

# Supported models with no MODEL_SHAPE entry: they render shape 1 (the wrong
# robot, no error). Shrink this list, never grow it.
_KNOWN_MISSING_SHAPE = {
    "dreame.vacuum.p2027",
    "dreame.vacuum.p2140",
    "dreame.vacuum.p2148o",
    "dreame.vacuum.p2149o",
    "dreame.vacuum.p2150a",
    "dreame.vacuum.p2187",
    "dreame.vacuum.r2104",
    "dreame.vacuum.r2205",
    "ijai.vacuum.v15",
    "viomi.vacuum.v18",
}


def _model_short(model: str) -> str:
    """Mirror of modelShort() in xiaomi-vac-card.js."""
    parts = model.split(".")
    if len(parts) >= 3 and parts[1] == "vacuum":
        return parts[0] + "." + ".".join(parts[2:])
    return model


def _shape_pairs() -> list[tuple[str, int]]:
    src = _CARD.read_text(encoding="utf-8")
    mo = re.search(r"const MODEL_SHAPE = Object\.fromEntries\(\[(.*?)\]\.flatMap", src, re.S)
    assert mo, "MODEL_SHAPE table not found in xiaomi-vac-card.js"

    pairs: list[tuple[str, int]] = []
    for shape, ids in re.findall(r"\[(\d+),\s*\[(.*?)\]\]", mo.group(1), re.S):
        pairs.extend((model_id, int(shape)) for model_id in re.findall(r'"([^"]+)"', ids))
    assert pairs, "MODEL_SHAPE parsed as empty"
    return pairs


def test_shape_keys_are_short_form() -> None:
    """A key containing '.vacuum.' can never match modelShort() output."""
    long_form = sorted(key for key, _shape in _shape_pairs() if ".vacuum." in key)

    assert not long_form


def test_shape_keys_are_unique() -> None:
    keys = [key for key, _shape in _shape_pairs()]
    duplicates = sorted({key for key in keys if keys.count(key) > 1})

    assert not duplicates


@pytest.mark.parametrize("state", _STATES)
def test_every_shape_has_its_lottie_assets(state: str) -> None:
    shapes = {shape for _key, shape in _shape_pairs()}
    missing = sorted(shape for shape in shapes if not (_LOTTIE / f"shape-{shape}-{state}.json").is_file())

    assert not missing


def test_supported_models_have_a_card_shape() -> None:
    keys = {key for key, _shape in _shape_pairs()}
    missing = {
        model
        for model in MODEL_PROFILES
        if is_supported(model) and _model_short(model) not in keys
    }

    assert missing == _KNOWN_MISSING_SHAPE
