"""``Handle`` — a frozen value that names something outside the document.

Bulk data (a volume, a mesh, a Zarr store), another document's node, or an
ontology term never live *in* a document: the document carries a handle
to them. A handle is an ordinary atomic value (frozen, replaced as a unit,
one operation on the wire), so undo and sync move only the handle.

A handle declares its **strength**:

- ``weak`` (default): the document is usable without resolving it. A
  terminology binding, a citation, a thumbnail.
- ``strong``: the document is unusable without it. A volume's voxel data.

Strength is declared on the handle type, so a consumer can read a
document's dependency list — ``doc.handles(strength="strong")`` — and
answer "can I open this?" without resolving anything. The schema export
carries it per field (``handles``), so a remote client can do the same.
"""

from __future__ import annotations

from typing import ClassVar, Literal

from pydantic import BaseModel

Strength = Literal["weak", "strong"]


class Handle(BaseModel, frozen=True):
    """Base class for handles. Subclass and set ``strength`` as needed::

        class VoxelData(Handle):
            strength = "strong"

    Only ``uri`` is required; ``media_type`` and ``digest`` are optional
    and conventional (a digest lets a consumer verify what it fetched).
    """

    strength: ClassVar[Strength] = "weak"

    uri: str
    media_type: str = ""
    digest: str = ""

    def __init_subclass__(cls, **kwargs: object) -> None:
        if "strength" in cls.__dict__.get("__annotations__", {}):
            raise TypeError(
                f"{cls.__name__}: declare strength as a plain class attribute "
                f"(strength = \"strong\"), not as an annotated field"
            )
        super().__init_subclass__(**kwargs)
        strength = cls.__dict__.get("strength", None)
        if strength is not None and strength not in ("weak", "strong"):
            raise ValueError(
                f"{cls.__name__}.strength must be 'weak' or 'strong', got {strength!r}"
            )


def is_handle_type(annotation: object) -> bool:
    return isinstance(annotation, type) and issubclass(annotation, Handle)
