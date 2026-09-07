"""Tests for Ref[T] fields: resolution, reverse index, and referential integrity."""

import warnings

import pytest
from pydantic import BaseModel, Field

from atomdoc import Array, Doc, Ref, RefIntegrityError, UndoManagerConfig, node
from atomdoc._ref import parse_ref_annotation
from atomdoc._tier import classify_field


@node
class Transform:
    name: str = ""
    parent: Ref["Transform"] | None = None


@node
class Volume(BaseModel):
    opacity: float = Field(ge=0.0, le=1.0, default=1.0)
    transform: Ref[Transform] | None = None
    sources: list[Ref["Volume"]] = []


@node
class Scene:
    transforms: Array[Transform] = []
    volumes: Array[Volume] = []
    other: Array[Transform] = []


def make_scene():
    doc = Doc(Scene, undo_manager=UndoManagerConfig(max_steps=10))
    with doc.transaction():
        t1 = doc.create_node(Transform, name="t1")
        doc.root.transforms.append(t1)
        t2 = doc.create_node(Transform, name="t2", parent=t1)
        doc.root.transforms.append(t2)
        v1 = doc.create_node(Volume, transform=t2)
        doc.root.volumes.append(v1)
        v2 = doc.create_node(Volume)
        doc.root.volumes.append(v2)
        v2.sources = [v1]
    return doc, t1, t2, v1, v2


# --- Declaration ---


def test_parse_ref_annotation():
    assert parse_ref_annotation(Ref[Transform]) == (Transform, False, False)
    assert parse_ref_annotation(Ref[Transform] | None) == (Transform, False, True)
    assert parse_ref_annotation(list[Ref[Transform]]) == (Transform, True, False)
    assert parse_ref_annotation(list[Ref[Transform]] | None) == (Transform, True, True)
    assert parse_ref_annotation(Ref["Volume"]) == ("Volume", False, False)
    assert parse_ref_annotation(str) is None
    assert parse_ref_annotation(list[str]) is None
    assert parse_ref_annotation(Ref[Transform] | str) is None


def test_ref_is_its_own_tier():
    assert classify_field(Ref[Transform]) == "ref"
    assert classify_field(list[Ref[Transform]] | None) == "ref"
    assert Volume._field_tiers == {"opacity": "mergeable", "transform": "ref", "sources": "ref"}


def test_ref_defs():
    assert Volume._ref_defs["transform"].target is Transform
    assert Volume._ref_defs["transform"].many is False
    assert Volume._ref_defs["transform"].optional is True
    # self-reference inside a BaseModel resolves to the node class
    assert Volume._ref_defs["sources"].target is Volume
    assert Volume._ref_defs["sources"].many is True
    assert Transform._ref_defs["parent"].target_name == "Transform"


# --- Read / write ---


def test_resolves_to_nodes_and_stores_ids():
    doc, t1, t2, v1, v2 = make_scene()
    assert v1.transform is t2
    assert t2.parent is t1
    assert t1.parent is None
    assert v2.sources == [v1]
    assert v1.ref_id("transform") == t2.id
    assert v2.ref_id("sources") == [v1.id]
    assert v1._state["transform"] == t2.id


def test_assign_by_id():
    doc, t1, t2, v1, v2 = make_scene()
    with doc.transaction():
        v1.transform = t1.id
    assert v1.transform is t1


def test_ref_id_rejects_non_ref_field():
    doc, t1, *_ = make_scene()
    with pytest.raises(AttributeError):
        t1.ref_id("name")


def test_wrong_target_type_rejected_on_assignment():
    doc, t1, t2, v1, v2 = make_scene()
    with pytest.raises(TypeError, match="references Transform"):
        with doc.transaction():
            v1.transform = v2
    assert v1.transform is t2


def test_cross_document_reference_rejected():
    doc, t1, t2, v1, v2 = make_scene()
    other, ot1, *_ = make_scene()
    with pytest.raises(ValueError, match="another document"):
        with doc.transaction():
            v1.transform = ot1


def test_unattached_snapshot_cannot_be_referenced():
    doc, t1, t2, v1, v2 = make_scene()
    with pytest.raises(ValueError, match="create_node"):
        with doc.transaction():
            v1.transform = Transform()


# --- Reverse index ---


def test_referrers():
    doc, t1, t2, v1, v2 = make_scene()
    assert doc.referrers(t2) == [v1]
    assert doc.referrers(t1) == [t2]
    assert doc.referrers(t1, field="transform") == []
    assert doc.referrers(t1, field="parent") == [t2]
    assert doc.referrers(v1) == [v2]
    assert doc.referrers(v2) == []


def test_index_follows_updates():
    doc, t1, t2, v1, v2 = make_scene()
    with doc.transaction():
        v1.transform = t1
    assert doc.referrers(t2) == []
    assert doc.referrers(t1) == [t2, v1]
    with doc.transaction():
        v2.sources = []
    assert doc.referrers(v1) == []


def test_index_is_not_serialized():
    doc, *_ = make_scene()
    assert "_ref_index" not in str(doc.dump())


# --- Integrity: restrict ---


def test_delete_referenced_node_fails_and_rolls_back():
    doc, t1, t2, v1, v2 = make_scene()
    with pytest.raises(RefIntegrityError, match="still referenced"):
        with doc.transaction():
            t2.delete()
    # Rollback re-inserts the node under its original ID (as with undo, the
    # Python object is new), so the reference still resolves.
    restored = doc.get_node_by_id(t2.id)
    assert restored is not None
    assert v1.transform is restored
    assert doc.referrers(restored) == [v1]
    assert doc._lifecycle_stage == "idle"


def test_repoint_and_delete_in_one_transaction():
    doc, t1, t2, v1, v2 = make_scene()
    with doc.transaction():
        v1.transform = t1
        t2.delete()
    assert doc.get_node_by_id(t2.id) is None
    assert doc.referrers(t1) == [v1]


def test_deleting_referrer_and_target_together():
    doc, t1, t2, v1, v2 = make_scene()
    with doc.transaction():
        v2.delete()
        v1.delete()
        t2.delete()
    assert doc.get_node_by_id(t2.id) is None
    assert doc.referrers(t1) == []


def test_delete_subtree_with_internal_references():
    @node
    class Item:
        buddy: Ref["Item"] | None = None
        kids: Array["Item"] = []

    @node
    class Root:
        items: Array[Item] = []

    doc = Doc(Root)
    with doc.transaction():
        top = doc.create_node(Item)
        doc.root.items.append(top)
        kid = doc.create_node(Item)
        top.kids.append(kid)
        kid.buddy = top
        top.buddy = kid
    with doc.transaction():
        top.delete()  # both referrers go with the subtree
    assert not doc.root.items


# --- Integrity: dangling / type ---


def test_reference_to_unknown_id_fails_at_commit():
    doc, t1, t2, v1, v2 = make_scene()
    with pytest.raises(RefIntegrityError, match="not in the document"):
        with doc.transaction():
            v1.transform = "nope"
    assert v1.transform is t2


def test_reference_to_created_but_uninserted_node_fails_at_commit():
    doc, t1, t2, v1, v2 = make_scene()
    with pytest.raises(RefIntegrityError):
        with doc.transaction():
            v1.transform = doc.create_node(Transform)
    assert v1.transform is t2


def test_reference_to_node_inserted_in_same_transaction():
    doc, t1, t2, v1, v2 = make_scene()
    with doc.transaction():
        t3 = doc.create_node(Transform, name="t3")
        v1.transform = t3
        doc.root.transforms.append(t3)
    assert v1.transform is t3
    assert doc.referrers(t3) == [v1]


def test_remote_patch_with_wrong_type_is_rejected():
    doc, t1, t2, v1, v2 = make_scene()
    remaining = doc.apply_operations(([], {v1.id: {"transform": v2.id}}))
    assert remaining == []
    assert v1.transform is t2  # rolled back


# --- Moves, undo, restore ---


def test_move_is_not_a_delete():
    doc, t1, t2, v1, v2 = make_scene()
    with doc.transaction():
        t2.move(doc.root, "other")
    assert t2._slot_name == "other"
    assert v1.transform is t2
    assert doc.referrers(t2) == [v1]


def test_undo_restores_deleted_target_and_referrers():
    doc, t1, t2, v1, v2 = make_scene()
    with doc.transaction():
        v1.transform = None
        t2.delete()
    doc.undo_manager.undo()
    restored = doc.get_node_by_id(t2.id)
    assert restored is not None
    assert v1.transform is restored
    assert doc.referrers(restored) == [v1]
    doc.undo_manager.redo()
    assert doc.get_node_by_id(t2.id) is None
    assert v1.transform is None


def test_dump_restore_rebuilds_index():
    doc, t1, t2, v1, v2 = make_scene()
    restored = Doc.restore(doc.dump(), Scene)
    rt2 = restored.get_node_by_id(t2.id)
    rv1 = restored.get_node_by_id(v1.id)
    assert rv1.transform is rt2
    assert restored.referrers(rt2) == [rv1]
    with pytest.raises(RefIntegrityError):
        with restored.transaction():
            rt2.delete()


def _dump_without(doc, node_id):
    data = doc.dump()

    def strip(entry):
        if len(entry) > 3:
            for slot, children in entry[3].items():
                entry[3][slot] = [c for c in children if c[0] != node_id]
                for child in entry[3][slot]:
                    strip(child)

    strip(data)
    return data


def test_restore_dangling_strict_raises():
    doc, t1, t2, v1, v2 = make_scene()
    with pytest.raises(RefIntegrityError, match="unresolved reference"):
        Doc.restore(_dump_without(doc, t2.id), Scene)


def test_restore_dangling_lenient_warns_and_reads_none():
    doc, t1, t2, v1, v2 = make_scene()
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        restored = Doc.restore(_dump_without(doc, t2.id), Scene, strict_mode=False)
    assert len(caught) == 1
    rv1 = restored.get_node_by_id(v1.id)
    assert rv1.transform is None
    assert rv1.ref_id("transform") == t2.id


# --- Serialization / schema ---


def test_to_json_emits_reference_paths():
    """The ID-free export names a target by its document path, not its ID."""
    doc, t1, t2, v1, v2 = make_scene()
    root = doc.to_json()
    t_paths = [f"/transforms/{i}" for i in range(len(doc.root.transforms))]
    v_paths = [f"/volumes/{i}" for i in range(len(doc.root.volumes))]
    assert doc.to_json(v1) == {"transform": t_paths[list(doc.root.transforms).index(t2)]}
    assert doc.to_json(v2) == {"sources": [v_paths[list(doc.root.volumes).index(v1)]]}
    assert root["volumes"][list(doc.root.volumes).index(v1)]["transform"].startswith("/transforms/")


def test_schema_export_describes_refs():
    doc, *_ = make_scene()
    vol = doc.atomdoc_schema()["node_types"]["Volume"]
    assert vol["field_tiers"]["transform"] == "ref"
    assert vol["refs"] == {
        "transform": {"target_type": "Transform", "many": False, "policy": "restrict"},
        "sources": {"target_type": "Volume", "many": True, "policy": "restrict"},
    }
    props = vol["json_schema"]["properties"]
    assert props["transform"] == {"anyOf": [{"type": "string"}, {"type": "null"}], "default": None}
    assert props["sources"] == {"type": "array", "items": {"type": "string"}, "default": []}
    assert doc.atomdoc_schema()["node_types"]["Scene"]["refs"] == {}


def test_snapshot_construction_with_ids():
    doc, t1, t2, v1, v2 = make_scene()
    with doc.transaction():
        v3 = doc.create_node(Volume, transform=t1.id)
        doc.root.volumes.append(v3)
    assert v3.transform is t1
