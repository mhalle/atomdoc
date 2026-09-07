"""to_json(): references as document paths; ChildrenView.remove."""

import pytest

from atomdoc import Array, Doc, Ref, node


@node
class Transform:
    name: str = ""
    parent: Ref["Transform"] | None = None


@node
class Volume:
    name: str = ""
    transform: Ref[Transform] | None = None
    extras: list[Ref[Transform]] = []


@node
class Group:
    name: str = ""
    items: Array["Group"] = []
    volumes: Array[Volume] = []


@node
class Scene:
    transforms: Array[Transform] = []
    groups: Array[Group] = []


def make():
    doc = Doc(Scene)
    with doc.transaction():
        hub = doc.create_node(Transform, name="hub")
        t1 = doc.create_node(Transform, name="t1", parent=hub)
        doc.root.transforms.append(hub)
        doc.root.transforms.append(t1)
        g = doc.create_node(Group, name="g")
        inner = doc.create_node(Group, name="inner")
        doc.root.groups.append(g)
        g.items.append(inner)
        v = doc.create_node(Volume, name="v", transform=t1, extras=[hub, t1])
        inner.volumes.append(v)
    return doc, hub, t1, g, inner, v


def test_refs_export_as_document_paths():
    doc, hub, t1, g, inner, v = make()
    data = doc.to_json()
    assert data["transforms"][1]["parent"] == "/transforms/0"
    vol = data["groups"][0]["items"][0]["volumes"][0]
    assert vol["transform"] == "/transforms/1"
    assert vol["extras"] == ["/transforms/0", "/transforms/1"]
    assert "id" not in vol and "-" not in vol["transform"]


def test_subtree_export_keeps_absolute_paths_and_nulls_dangling():
    doc, hub, t1, g, inner, v = make()
    data = doc.to_json(inner)
    assert data["volumes"][0]["transform"] == "/transforms/1"
    # A reference that no longer resolves (lenient restore of a dump with
    # a dangling ID) exports as null.
    v._state["transform"] = "gone"
    assert doc.to_json(inner)["volumes"][0]["transform"] is None


def test_remove_deletes_and_points_at_move():
    doc, hub, t1, g, inner, v = make()
    doc.root.transforms.remove(t1) if False else None
    with pytest.raises(ValueError, match="not in slot"):
        doc.root.transforms.remove(v)
    with pytest.raises(RuntimeError, match="node.move"):
        doc.root.transforms.append(hub)
    inner.volumes.remove(v)
    assert list(inner.volumes) == []
    assert doc.get_node_by_id(v.id) is None
