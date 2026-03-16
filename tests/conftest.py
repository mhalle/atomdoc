"""Shared fixtures for atomdoc tests."""

from __future__ import annotations

import pytest
from pydantic import BaseModel

from atomdoc import Doc, AtomNode


class Color(BaseModel, frozen=True):
    r: int = 0
    g: int = 0
    b: int = 0


class PageNode(AtomNode, node_type="page"):
    title: str = ""
    body: str = ""


class AnnotationNode(AtomNode, node_type="annotation"):
    label: str = ""
    color: Color = Color()
    opacity: float = 1.0
    visible: bool = True
    thumbnail: bytes = b""


@pytest.fixture
def node_classes():
    return [PageNode, AnnotationNode]


@pytest.fixture
def doc(node_classes):
    return Doc(root_type="page", nodes=node_classes)
