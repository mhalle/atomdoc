"""Tests for field tier classification."""

from pydantic import BaseModel

from atomdoc._tier import classify_field


class FrozenColor(BaseModel, frozen=True):
    r: int = 0


class MutableModel(BaseModel):
    x: int = 0


def test_str_is_mergeable():
    assert classify_field(str) == "mergeable"


def test_int_is_mergeable():
    assert classify_field(int) == "mergeable"


def test_float_is_mergeable():
    assert classify_field(float) == "mergeable"


def test_bool_is_mergeable():
    assert classify_field(bool) == "mergeable"


def test_bytes_is_opaque():
    assert classify_field(bytes) == "opaque"


def test_frozen_model_is_atomic():
    assert classify_field(FrozenColor) == "atomic"


def test_mutable_model_is_mergeable():
    assert classify_field(MutableModel) == "mergeable"


def test_optional_frozen_is_atomic():
    from typing import Optional
    assert classify_field(Optional[FrozenColor]) == "atomic"


def test_optional_str_is_mergeable():
    from typing import Optional
    assert classify_field(Optional[str]) == "mergeable"
