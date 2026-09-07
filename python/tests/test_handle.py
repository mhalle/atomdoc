"""Tests for Handle values, tagged unions, and JsonValue fields."""

from typing import Annotated, Literal, Union

import pytest
from pydantic import BaseModel, Field

from atomdoc import Array, Doc, Handle, JsonValue, node
from atomdoc._tier import classify_field, frozen_models_in


class VoxelData(Handle):
    strength = "strong"


class Terminology(Handle):
    pass  # weak by default


class Scalar(BaseModel, frozen=True):
    kind: Literal["scalar"] = "scalar"
    v: float = 0.0


class Vec(BaseModel, frozen=True):
    kind: Literal["vec"] = "vec"
    v: tuple[float, float, float] = (0.0, 0.0, 0.0)


@node
class Volume:
    data: VoxelData | None = None
    term: Terminology | None = None
    value: Annotated[Union[Scalar, Vec], Field(discriminator="kind")] = Scalar()
    extra: JsonValue = None


@node
class Scene:
    volumes: Array[Volume] = []


def make():
    doc = Doc(Scene)
    with doc.transaction():
        v1 = doc.create_node(Volume, data=VoxelData(uri="s3://a/vol.nii", digest="sha256:1"))
        doc.root.volumes.append(v1)
        v2 = doc.create_node(Volume, term=Terminology(uri="snomed:123"))
        doc.root.volumes.append(v2)
    return doc, v1, v2


# --- Handles ---


def test_handle_strength_declared_on_type():
    assert Handle.strength == "weak"
    assert VoxelData.strength == "strong"
    assert Terminology.strength == "weak"
    assert VoxelData(uri="x").strength == "strong"


def test_handle_strength_validated():
    with pytest.raises(ValueError, match="strength"):

        class Bad(Handle):
            strength = "sometimes"  # type: ignore[assignment]


def test_handle_is_atomic():
    assert classify_field(VoxelData) == "atomic"
    assert classify_field(VoxelData | None) == "atomic"
    assert Volume._field_tiers["data"] == "atomic"


def test_doc_handles_lists_dependencies():
    doc, v1, v2 = make()
    strong = doc.handles(strength="strong")
    assert strong == [(v1, "data", VoxelData(uri="s3://a/vol.nii", digest="sha256:1"))]
    weak = doc.handles(strength="weak")
    assert weak == [(v2, "term", Terminology(uri="snomed:123"))]
    assert len(doc.handles()) == 2


def test_handle_round_trips_and_moves_as_a_unit():
    doc, v1, v2 = make()
    restored = Doc.restore(doc.dump(), Scene)
    rv1 = restored.get_node_by_id(v1.id)
    assert rv1.data == VoxelData(uri="s3://a/vol.nii", digest="sha256:1")
    assert restored.handles(strength="strong")[0][2].uri == "s3://a/vol.nii"


def test_schema_exports_handles():
    doc, *_ = make()
    schema = doc.atomdoc_schema()
    vol = schema["node_types"]["Volume"]
    assert vol["handles"] == {
        "data": {"value_type": "VoxelData", "strength": "strong"},
        "term": {"value_type": "Terminology", "strength": "weak"},
    }
    assert schema["value_types"]["VoxelData"]["handle"] == {"strength": "strong"}
    assert schema["value_types"]["Terminology"]["handle"] == {"strength": "weak"}
    assert "handle" not in schema["value_types"]["Scalar"]
    assert schema["node_types"]["Scene"]["handles"] == {}


# --- Tagged unions ---


def test_union_of_frozen_models_is_atomic():
    assert classify_field(Scalar | Vec) == "atomic"
    assert classify_field(Union[Scalar, Vec, None]) == "atomic"
    assert classify_field(Annotated[Union[Scalar, Vec], Field(discriminator="kind")]) == "atomic"
    assert classify_field(Scalar | int) == "mergeable"
    assert Volume._field_tiers["value"] == "atomic"


def test_frozen_models_in():
    assert frozen_models_in(Scalar | Vec) == [Scalar, Vec]
    assert frozen_models_in(Annotated[Union[Scalar, Vec], Field(discriminator="kind")]) == [Scalar, Vec]
    assert frozen_models_in(list[Scalar] | None) == [Scalar]
    assert frozen_models_in(int) == []


def test_tagged_union_round_trips_as_one_value():
    doc, v1, v2 = make()
    ops = []
    doc.on_change(lambda e: ops.append(e.operations))
    with doc.transaction():
        v1.value = Vec(v=(1.0, 2.0, 3.0))
    assert ops[-1][1] == {v1.id: {"value": {"kind": "vec", "v": [1.0, 2.0, 3.0]}}}
    restored = Doc.restore(doc.dump(), Scene)
    assert restored.get_node_by_id(v1.id).value == Vec(v=(1.0, 2.0, 3.0))
    assert isinstance(restored.get_node_by_id(v2.id).value, Scalar)


def test_union_members_exported_as_value_types_and_inlined():
    doc, *_ = make()
    schema = doc.atomdoc_schema()
    assert {"Scalar", "Vec"} <= set(schema["value_types"])
    prop = schema["node_types"]["Volume"]["json_schema"]["properties"]["value"]
    assert "$defs" not in prop
    assert prop["discriminator"]["propertyName"] == "kind"
    assert [v["title"] for v in prop["oneOf"]] == ["Scalar", "Vec"]
    assert prop["oneOf"][1]["properties"]["kind"]["const"] == "vec"
    assert prop["default"] == {"kind": "scalar", "v": 0.0}


# --- JsonValue ---


def test_json_value_field_holds_any_json():
    doc, v1, v2 = make()
    assert Volume._field_tiers["extra"] == "mergeable"
    for value in (3.5, "s", True, None, [1, "a", None], {"k": [1, {"n": 2}]}):
        with doc.transaction():
            v1.extra = value
        assert Doc.restore(doc.dump(), Scene).get_node_by_id(v1.id).extra == value


def test_json_value_exports_as_any():
    doc, *_ = make()
    prop = doc.atomdoc_schema()["node_types"]["Volume"]["json_schema"]["properties"]["extra"]
    assert "$defs" not in prop
    assert prop.get("default") is None
