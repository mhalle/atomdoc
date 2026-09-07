"""Tests for AtomNode schema via __init_subclass__."""

from pydantic import BaseModel

from atomdoc import AtomNode


class Vec3(BaseModel, frozen=True):
    x: float = 0.0
    y: float = 0.0
    z: float = 0.0


class ShapeNode(AtomNode, node_type="shape"):
    label: str = ""
    position: Vec3 = Vec3()
    data: bytes = b""


def test_node_type():
    assert ShapeNode._node_type == "shape"


def test_field_tiers():
    assert ShapeNode._field_tiers["label"] == "mergeable"
    assert ShapeNode._field_tiers["position"] == "atomic"
    assert ShapeNode._field_tiers["data"] == "opaque"


def test_field_defaults():
    assert ShapeNode._field_defaults["label"] == ""
    assert ShapeNode._field_defaults["position"] == Vec3()
    assert ShapeNode._field_defaults["data"] == b""


def test_schema_model_exists():
    assert ShapeNode._schema_model is not None
    schema = ShapeNode._schema_model.model_json_schema()
    assert "properties" in schema
    assert "label" in schema["properties"]


def test_mixin_fields():
    class Timestamped(BaseModel):
        created_at: str = ""
        updated_at: str = ""

    class ProjectNode(AtomNode, Timestamped, node_type="project"):
        name: str = ""

    assert "name" in ProjectNode._field_tiers
    assert "created_at" in ProjectNode._field_tiers
    assert "updated_at" in ProjectNode._field_tiers


def test_is_abstract_without_node_type():
    class AbstractNode(AtomNode):
        pass

    assert AbstractNode._is_abstract is True
