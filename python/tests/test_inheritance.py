"""Node inheritance, inherited declarations, and containers of models."""

import json

import pytest
from pydantic import BaseModel, Field, ValidationError, model_validator

from atomdoc import Array, AtomNode, Doc, Ref, node


class Color(BaseModel, frozen=True):
    r: int = 0


@node
class Shape(BaseModel):
    name: str = ""
    opacity: float = Field(default=1.0, ge=0.0, le=1.0)
    colors: list[Color] = []

    @model_validator(mode="after")
    def no_bad_names(self):
        if self.name == "bad":
            raise ValueError("bad name")
        return self


@node
class Circle(Shape):
    radius: float = Field(default=1.0, gt=0)
    twin: Ref["Circle"] | None = None


@node
class Canvas:
    shapes: Array[Shape] = []


def test_node_subclass_is_a_real_subclass_and_fits_polymorphic_slots():
    assert issubclass(Circle, Shape)
    doc = Doc(Canvas)  # Circle discovered as a subclass of Shape
    assert set(doc._node_types) >= {"Shape", "Circle", "Canvas"}
    c = doc.create_node(Circle, name="c", radius=2.0, opacity=0.5)
    s = doc.create_node(Shape, name="s")
    doc.root.shapes.append(s)
    doc.root.shapes.append(c)
    assert [type(n).__name__ for n in doc.root.shapes] == ["Shape", "Circle"]
    assert (c.name, c.opacity, c.radius, c.colors) == ("c", 0.5, 2.0, [])
    schema = doc.atomdoc_schema()
    assert set(schema["node_types"]["Circle"]["field_defaults"]) == {
        "name", "opacity", "colors", "radius", "twin",
    }
    assert schema["node_types"]["Circle"]["refs"]["twin"]["target_type"] == "Circle"
    assert schema["node_types"]["Canvas"]["slots"]["shapes"]["allowed_type"] == "Shape"


def test_derived_keeps_base_validators_and_constraints():
    doc = Doc(Canvas)
    c = doc.create_node(Circle, name="c")
    doc.root.shapes.append(c)
    with pytest.raises(ValidationError):
        c.name = "bad"  # base model validator, inherited by the derived model
    assert c.name == "c"
    with pytest.raises(ValidationError):
        c.opacity = 2.0  # base Field constraint
    with pytest.raises(ValidationError):
        c.radius = -1.0  # derived Field constraint


def test_derived_round_trips_and_references_resolve():
    doc = Doc(Canvas)
    a = doc.create_node(Circle, name="a")
    b = doc.create_node(Circle, name="b", twin=a)
    doc.root.shapes.append(a)
    doc.root.shapes.append(b)
    data = doc.dump()
    json.dumps(data)
    restored = Doc.restore(data, root_type=Canvas)
    shapes = list(restored.root.shapes)
    assert [type(n).__name__ for n in shapes] == ["Circle", "Circle"]
    assert shapes[1].twin is shapes[0]
    assert restored.referrers(shapes[0]) == [shapes[1]]


class T(AtomNode, node_type="T"):
    name: str = ""


class B(AtomNode, node_type="B"):
    label: str = "base-default"
    opacity: float = Field(default=0.5, ge=0, le=1)
    tags: list[str] = Field(default_factory=lambda: ["x"])
    ref: Ref[T] | None = None


class D(B, node_type="D"):
    more: int = 0


@node
class R2:
    ds: Array[D] = []
    ts: Array[T] = []


def test_direct_subclass_inherits_defaults_constraints_factories_and_refs():
    assert D._field_defaults["label"] == "base-default"
    assert D._field_defaults["opacity"] == 0.5
    assert D._field_defaults["ref"] is None
    doc = Doc(R2)
    d1 = doc.create_node(D)
    d2 = doc.create_node(D)
    doc.root.ds.append(d1)
    doc.root.ds.append(d2)
    assert (d1.label, d1.opacity, d1.ref, d1.tags) == ("base-default", 0.5, None, ["x"])
    d1.tags.append("y")
    assert d2.tags == ["x"]  # the factory ran per node
    json.dumps(doc.dump())
    with pytest.raises(ValidationError):
        d1.opacity = 5
    t = doc.create_node(T)
    doc.root.ts.append(t)
    d1.ref = t
    assert doc.referrers(t) == [d1]
    assert doc.atomdoc_schema()["node_types"]["D"]["json_schema"]["properties"]["opacity"]["maximum"] == 1


@node
class Palette:
    colors: list[Color] = []
    by_name: dict[str, Color] = {}
    nested: dict[str, list[Color]] = {}


@node
class PRoot:
    palettes: Array[Palette] = []


def test_collections_of_models_serialize_and_round_trip():
    doc = Doc(PRoot)
    events = []
    doc.on_change(events.append)
    p = doc.create_node(
        Palette,
        colors=[Color(r=1), Color(r=2)],
        by_name={"a": Color(r=3)},
        nested={"k": [Color(r=4)]},
    )
    doc.root.palettes.append(p)
    data = doc.dump()
    text = json.dumps(data)
    state = data[3]["palettes"][0][2]
    assert state["colors"] == [{"r": 1}, {"r": 2}]
    assert state["by_name"] == {"a": {"r": 3}}
    assert state["nested"] == {"k": [{"r": 4}]}
    # Patches on the wire carry JSON too.
    assert events[-1].operations[1][p.id]["colors"] == [{"r": 1}, {"r": 2}]
    p.colors = [Color(r=9)]
    assert events[-1].operations[1][p.id]["colors"] == [{"r": 9}]
    assert events[-1].inverse_operations[1][p.id]["colors"] == [{"r": 1}, {"r": 2}]
    restored = Doc.restore(json.loads(text), root_type=PRoot)
    rp = restored.root.palettes[0]
    assert rp.colors == [Color(r=1), Color(r=2)]
    assert rp.by_name == {"a": Color(r=3)}
    assert rp.nested == {"k": [Color(r=4)]}
    assert restored.to_json()["palettes"][0]["by_name"] == {"a": {"r": 3}}


# --- validators on a derived plain class; union and bare slots ---


@node
class Transform(BaseModel):
    name: str = ""

    @model_validator(mode="after")
    def one_line(self):
        if "\n" in self.name:
            raise ValueError("single line")
        return self


@node
class Deformable(Transform):
    kind: str = "rigid"
    field: str = ""
    scale: float = Field(default=1.0, gt=0)

    @model_validator(mode="after")
    def needs_field(self):
        if self.kind == "deformable" and not self.field:
            raise ValueError("a deformable transform needs a field")
        return self


@node
class TRoot:
    transforms: Array[Transform] = []


def test_model_validator_on_a_derived_node_class_runs():
    doc = Doc(TRoot)
    d = doc.create_node(Deformable, name="d")
    doc.root.transforms.append(d)
    with pytest.raises(ValidationError, match="needs a field"):
        d.kind = "deformable"
    assert d.kind == "rigid"
    with doc.transaction():
        d.field = "warp"
        d.kind = "deformable"
    assert d.kind == "deformable"
    with pytest.raises(ValidationError, match="single line"):
        d.name = "two\nlines"  # the base's rule still applies
    with pytest.raises(ValidationError):
        d.scale = 0.0  # the derived Field constraint is enforced too


@node
class A:
    x: int = 0


@node
class B:
    y: int = 0


@node
class C:
    z: int = 0


@node
class Mixed:
    either: Array[A | B] = []
    anything: Array = []


def test_union_and_bare_array_slots():
    # A bare Array names no type, so C is registered explicitly.
    doc = Doc(Mixed, nodes=[C])
    assert {"A", "B", "C", "Mixed"} <= set(doc._node_types)
    a, b, c = doc.create_node(A), doc.create_node(B), doc.create_node(C)
    doc.root.either.append(a)
    doc.root.either.append(b)
    with pytest.raises(TypeError, match="accepts A | B, not C"):
        doc.root.either.append(c)
    doc.root.anything.append(c)  # bare Array: any node
    assert [type(n).__name__ for n in doc.root.either] == ["A", "B"]
    slots = doc.atomdoc_schema()["node_types"]["Mixed"]["slots"]
    assert slots["either"] == {"allowed_type": None, "allowed_types": ["A", "B"]}
    assert slots["anything"] == {"allowed_type": None, "allowed_types": []}
    restored = Doc.restore(doc.dump(), root_type=Mixed, nodes=[C])
    assert [type(n).__name__ for n in restored.root.either] == ["A", "B"]
    assert type(restored.root.anything[0]).__name__ == "C"


def test_restore_needs_a_root_class():
    doc = Doc(Canvas(), nodes=[Circle])
    doc.root.shapes.append(doc.create_node(Circle, radius=2.0))
    wire = doc.dump()
    with pytest.raises(TypeError, match="root_type="):
        Doc.restore(wire)
    # Resolved from nodes= when the root class is registered there.
    back = Doc.restore(wire, nodes=[Canvas, Circle])
    assert isinstance(back.root, Canvas)
    assert back.root.shapes[0].radius == 2.0


def test_allowed_types_lists_accepted_subclasses():
    doc = Doc(Canvas(), nodes=[Circle])
    slot = doc.atomdoc_schema()["node_types"]["Canvas"]["slots"]["shapes"]
    assert slot["allowed_type"] == "Shape"
    assert slot["allowed_types"] == ["Shape", "Circle"]
