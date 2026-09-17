"""Any field name can be passed to create_node as a keyword."""

from __future__ import annotations

from atomdoc import Array, Doc, node


@node
class Widget:
    node_cls: str = ""            # create_node's own parameter name
    state: str = ""               # and the name of its **kwargs


@node
class Box:
    widgets: Array[Widget] = []


def test_a_field_named_like_create_nodes_parameters():
    d = Doc(Box())
    with d.transaction():
        w = d.create_node(Widget, node_cls="button", state="pressed")
        d.root.widgets.append(w)
    assert (w.node_cls, w.state) == ("button", "pressed")
