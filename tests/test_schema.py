"""Tests for atomdoc_schema() export."""

from pydantic import BaseModel

from atomdoc import Array, AtomNode, Doc, node


class Color(BaseModel, frozen=True):
    r: int = 0
    g: int = 0
    b: int = 0


@node
class Annotation:
    label: str = ""
    color: Color = Color()


@node
class Page:
    title: str = ""
    annotations: Array[Annotation] = []


def make_doc():
    return Doc(root_type=Page)


def test_schema_has_version():
    schema = make_doc().atomdoc_schema()
    assert schema["version"] == 1


def test_schema_has_root_type():
    schema = make_doc().atomdoc_schema()
    assert schema["root_type"] == "Page"


def test_schema_has_node_types():
    schema = make_doc().atomdoc_schema()
    assert "Page" in schema["node_types"]
    assert "Annotation" in schema["node_types"]


def test_node_type_has_json_schema():
    schema = make_doc().atomdoc_schema()
    ann = schema["node_types"]["Annotation"]
    assert "json_schema" in ann
    props = ann["json_schema"].get("properties", {})
    assert "label" in props
    assert "color" in props


def test_node_type_has_field_tiers():
    schema = make_doc().atomdoc_schema()
    ann = schema["node_types"]["Annotation"]
    assert ann["field_tiers"]["label"] == "mergeable"
    assert ann["field_tiers"]["color"] == "atomic"


def test_node_type_has_slots():
    schema = make_doc().atomdoc_schema()
    page = schema["node_types"]["Page"]
    assert "annotations" in page["slots"]
    assert page["slots"]["annotations"]["allowed_type"] == "Annotation"


def test_node_type_without_slots():
    schema = make_doc().atomdoc_schema()
    ann = schema["node_types"]["Annotation"]
    assert ann["slots"] == {}


def test_node_type_has_field_defaults():
    schema = make_doc().atomdoc_schema()
    ann = schema["node_types"]["Annotation"]
    assert ann["field_defaults"]["label"] == ""
    assert ann["field_defaults"]["color"] == {"r": 0, "g": 0, "b": 0}


def test_value_types_discovered():
    schema = make_doc().atomdoc_schema()
    assert "Color" in schema["value_types"]
    color_vt = schema["value_types"]["Color"]
    assert color_vt["frozen"] is True
    assert "json_schema" in color_vt
    props = color_vt["json_schema"].get("properties", {})
    assert "r" in props
    assert "g" in props
    assert "b" in props


def test_node_without_state_fields():
    @node
    class Container:
        items: Array[Annotation] = []

    doc = Doc(root_type=Container)
    schema = doc.atomdoc_schema()
    container = schema["node_types"]["Container"]
    assert container["json_schema"] == {"type": "object", "properties": {}}
    assert container["field_tiers"] == {}
    assert "items" in container["slots"]
