"""Regression tests for the findings of the adversarial review."""

import copy
import json
from datetime import datetime
from decimal import Decimal
from enum import Enum

import pytest
from pydantic import BaseModel, Field, ValidationError

from atomdoc import Array, Doc, Handle, Ref, RefIntegrityError, UndoManagerConfig, node


@node
class Transform:
    name: str = ""


@node
class Volume:
    label: str = ""
    transform: Ref[Transform] | None = None
    required_ref: Ref[Transform] = None  # required: no `| None`


@node
class Scene:
    transforms: Array[Transform] = []
    volumes: Array[Volume] = []


def make_scene():
    doc = Doc(Scene, undo_manager=UndoManagerConfig(max_steps=10))
    with doc.transaction():
        t = doc.create_node(Transform, name="t")
        doc.root.transforms.append(t)
        v = doc.create_node(Volume, label="v", transform=t)
        doc.root.volumes.append(v)
    return doc, t, v


# --- abort ordering and exception safety ---


def test_body_exception_rolls_back_in_reverse_order():
    @node
    class Item:
        name: str = ""
        kids: Array["Item"] = []

    @node
    class Root:
        items: Array[Item] = []

    doc = Doc(Root)
    with doc.transaction():
        for n in ("one", "two", "three"):
            doc.root.items.append(doc.create_node(Item, name=n))
    n3 = doc.root.items[2]
    before = doc.dump()
    with pytest.raises(RuntimeError, match="boom"):
        with doc.transaction():
            box = doc.create_node(Item, name="box")
            doc.root.items.append(box)
            n3.move(box, "kids")
            raise RuntimeError("boom")
    assert doc.dump() == before
    assert doc.get_node_by_id(n3.id) is not None
    assert doc._lifecycle_stage == "idle"


def test_rollback_of_required_ref_write_does_not_wedge():
    doc, t, v = make_scene()
    with pytest.raises(RuntimeError):
        with doc.transaction():
            v.required_ref = t
            raise RuntimeError("user code failed")
    assert doc._lifecycle_stage == "idle"
    assert v.required_ref is None
    assert doc.referrers(t) == [v]  # only via transform
    doc.dump()  # would raise if wedged
    events = []
    doc.on_change(events.append)
    with doc.transaction():
        v.label = "after"
    assert len(events) == 1


def test_integrity_failure_on_required_ref_rolls_back_cleanly():
    doc, t, v = make_scene()
    with pytest.raises(RefIntegrityError):
        with doc.transaction():
            v.required_ref = "no-such-id"
    assert doc._lifecycle_stage == "idle"
    assert v.ref_id("required_ref") is None
    assert "no-such-id" not in doc._ref_index


def test_required_ref_round_trips_with_defaults():
    doc, t, v = make_scene()
    restored = Doc.restore(doc.dump(include_defaults=True), Scene)
    assert restored.get_node_by_id(v.id).required_ref is None


def test_unset_required_field_reads_as_none_not_sentinel():
    @node
    class N:
        x: int
        y: str | None = None

    @node
    class R:
        items: Array[N] = []

    doc = Doc(R)
    with doc.transaction():
        n = doc.create_node(N)
        doc.root.items.append(n)
    assert n.x is None
    assert n.y is None


# --- duplicate IDs ---


def test_append_same_node_twice_rejected():
    @node
    class G:
        name: str = ""

    @node
    class R:
        groups: Array[G] = []

    doc = Doc(R)
    with pytest.raises(RuntimeError, match="already exists"):
        with doc.transaction():
            g = doc.create_node(G)
            doc.root.groups.append(g, g)
    assert list(doc.root.groups) == []


def test_adopt_same_fragment_twice_in_one_call():
    doc, t, v = make_scene()
    lib = Doc(Scene)
    frag = doc.dump(v)
    with lib.transaction():
        lib.adopt(doc.dump(t), lib.root, "transforms")
        a, b = lib.adopt([copy.deepcopy(frag), copy.deepcopy(frag)], lib.root, "volumes")
    assert a.id == v.id
    assert b.id != v.id
    assert a.transform is b.transform
    restored = Doc.restore(lib.dump(), Scene)
    assert len(restored.root.volumes) == 2


def test_adopt_rejects_fragment_with_internal_duplicate():
    @node
    class Item:
        kids: Array["Item"] = []

    @node
    class R:
        items: Array[Item] = []

    src = Doc(R)
    with src.transaction():
        top = src.create_node(Item)
        src.root.items.append(top)
        top.kids.append(src.create_node(Item))
    frag = src.dump(top)
    frag[3]["kids"].append(copy.deepcopy(frag[3]["kids"][0]))
    lib = Doc(R)
    with pytest.raises(ValueError, match="more than once"):
        with lib.transaction():
            lib.adopt(frag, lib.root, "items")


def test_restore_rejects_duplicate_ids():
    doc, t, v = make_scene()
    data = doc.dump()
    data[3]["transforms"].append(copy.deepcopy(data[3]["transforms"][0]))
    with pytest.raises(ValueError, match="Duplicate node id"):
        Doc.restore(data, Scene)


def test_adopt_reseeds_id_session(monkeypatch):
    from atomdoc import _id

    monkeypatch.setattr(_id, "random_base64", lambda n: "A" * n)
    monkeypatch.setattr(_id.time, "time", lambda: 1_700_000_000.0)
    a = Doc(Scene)
    with a.transaction():
        for i in range(3):
            a.root.transforms.append(a.create_node(Transform, name=str(i)))
    b = Doc(Scene, doc_id=a.id)  # same root => same session under the patches
    assert b._id_gen.session_id == a._id_gen.session_id
    with b.transaction():
        b.adopt(a.dump(a.root.transforms[0]), b.root, "transforms")
        b.adopt(a.dump(a.root.transforms[1]), b.root, "transforms")
        b.adopt(a.dump(a.root.transforms[2]), b.root, "transforms")
    with b.transaction():
        b.root.transforms.append(b.create_node(Transform, name="new"))
    assert len(b.root.transforms) == 4


# --- moves, undo, construction ---


def test_move_into_detached_parent_rejected():
    doc, t, v = make_scene()
    with pytest.raises(ValueError, match="not in the document"):
        with doc.transaction():
            t.move(doc.create_node(Scene), "transforms")
    assert doc.get_node_by_id(t.id) is t
    assert list(doc.root.transforms) == [t]


def test_undo_keeps_step_when_it_cannot_apply():
    doc, t, v = make_scene()
    with doc.transaction():
        t2 = doc.create_node(Transform, name="t2")
        doc.root.transforms.append(t2)
    with doc.transaction(skip_undo=True):
        v.transform = t2  # a peer's change referencing t2
    um = doc.undo_manager
    depth = len(um._undo_stack)
    with pytest.raises(RefIntegrityError):
        um.undo()
    assert len(um._undo_stack) == depth
    assert doc.get_node_by_id(t2.id) is not None
    assert doc._lifecycle_stage == "idle"
    with doc.transaction(skip_undo=True):
        v.transform = None
    um.undo()
    assert doc.get_node_by_id(t2.id) is None


def test_apply_operations_raise_on_error():
    doc, t, v = make_scene()
    bad = ([(1, t.id, 0)], {})
    assert doc.apply_operations(bad) == []  # lenient: skipped, rolled back
    assert doc.get_node_by_id(t.id) is not None
    with pytest.raises(RefIntegrityError):
        doc.apply_operations(bad, raise_on_error=True)
    with pytest.raises(ValueError, match="not found"):
        doc.apply_operations(([], {"ghost": {"label": "x"}}), strict=True)
    doc.apply_operations(([], {"ghost": {"label": "x"}}))  # lenient no-op


def test_snapshot_construction_checks_references():
    with pytest.raises(RefIntegrityError):
        Doc(Scene(volumes=[Volume(label="v", transform="does-not-exist")]))
    with pytest.warns(UserWarning):
        Doc(Scene(volumes=[Volume(label="v", transform="does-not-exist")]), strict_mode=False)


# --- defaults ---


def test_default_factory_is_per_node_on_plain_class():
    @node
    class Item:
        tags: list[str] = Field(default_factory=list)
        meta: dict[str, int] = {}

    @node
    class R:
        items: Array[Item] = []

    doc = Doc(R(items=[Item(), Item()]))
    a, b = list(doc.root.items)
    assert a.tags is not b.tags
    assert a.meta is not b.meta
    a.tags.append("only-a")
    assert b.tags == []
    assert Item._field_defaults["tags"] == []
    assert doc.atomdoc_schema()["node_types"]["Item"]["field_defaults"] == {"tags": [], "meta": {}}
    with doc.transaction():
        c = doc.create_node(Item)
        doc.root.items.append(c)
    assert c.tags == [] and c.tags is not a.tags


def test_default_factory_and_none_default_on_basemodel_source():
    @node
    class BM(BaseModel):
        tags: list[str] = Field(default_factory=list)
        note: str | None = None

    @node
    class R:
        items: Array[BM] = []

    doc = Doc(R)
    with doc.transaction():
        a = doc.create_node(BM)
        b = doc.create_node(BM)
        doc.root.items.append(a, b)
    assert a.tags == [] and a.tags is not b.tags
    assert a.note is None
    assert BM._field_defaults["note"] is None
    assert doc.atomdoc_schema()["node_types"]["BM"]["field_defaults"] == {"tags": [], "note": None}


def test_alias_with_constraint_still_validated():
    @node
    class Al:
        x: int = Field(default=0, alias="ex", ge=0)

    doc = Doc(Al)
    with pytest.raises(ValidationError):
        with doc.transaction():
            doc.root.x = -1
    assert doc.root.x == 0


def test_non_json_defaults_export():
    class Kind(Enum):
        A = "a"

    @node
    class N:
        when: datetime = datetime(2020, 1, 1)
        amount: Decimal = Decimal("1.5")
        kind: Kind = Kind.A
        tags: set[str] = set()

    schema = Doc(N).atomdoc_schema()
    dumped = json.loads(json.dumps(schema))
    assert dumped["node_types"]["N"]["field_defaults"] == {
        "when": "2020-01-01T00:00:00",
        "amount": "1.5",
        "kind": "a",
        "tags": [],
    }


# --- handles and export ---


class Weak(Handle):
    pass


class Strong(Weak):
    strength = "strong"


def test_handles_inside_containers_and_union_strength():
    @node
    class N:
        many: list[Strong] = []
        mapping: dict[str, Weak] = {}
        either: Weak | Strong | None = None

    doc = Doc(N)
    with doc.transaction():
        doc.root.many = [Strong(uri="a"), Strong(uri="b")]
        doc.root.mapping = {"k": Weak(uri="c")}
        doc.root.either = Strong(uri="d")
    strong = sorted(h.uri for _, _, h in doc.handles(strength="strong"))
    assert strong == ["a", "b", "d"]
    assert [h.uri for _, _, h in doc.handles(strength="weak")] == ["c"]
    exported = doc.atomdoc_schema()["node_types"]["N"]["handles"]
    assert exported["either"] == {"value_type": "Strong", "strength": "strong"}
    assert exported["many"]["strength"] == "strong"


def test_handle_strength_as_annotation_rejected():
    with pytest.raises(TypeError, match="plain class attribute"):

        class Bad(Handle):
            strength: str = "strong"  # type: ignore[assignment]


def test_value_type_name_collision_is_an_error():
    def make(name):
        return type(name, (BaseModel,), {"__annotations__": {"r": int}, "r": 0, "model_config": {"frozen": True}})

    C1, C2 = make("Color"), make("Color")

    @node
    class N:
        a: C1 = C1()
        b: C2 = C2()

    with pytest.raises(ValueError, match="Two value types named 'Color'"):
        Doc(N).atomdoc_schema()


def test_inlined_export_has_no_defs_or_mapping():
    from typing import Annotated, Literal, Union

    class Sc(BaseModel, frozen=True):
        kind: Literal["sc"] = "sc"
        v: float = 0.0

    class Vc(BaseModel, frozen=True):
        kind: Literal["vc"] = "vc"
        v: float = 0.0

    class Tree(BaseModel, frozen=True):
        kids: list["Tree"] = []

    @node
    class N:
        value: Annotated[Union[Sc, Vc], Field(discriminator="kind")] = Sc()
        tree: Tree = Tree()

    text = json.dumps(Doc(N).atomdoc_schema())
    assert "$defs" not in text and "$ref" not in text
    prop = Doc(N).atomdoc_schema()["node_types"]["N"]["json_schema"]["properties"]["value"]
    assert prop["discriminator"] == {"propertyName": "kind"}
