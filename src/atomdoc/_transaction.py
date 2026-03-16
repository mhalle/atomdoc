"""Transaction context manager and with_transaction helper."""

from __future__ import annotations

from contextlib import contextmanager
from collections.abc import Generator
from typing import TYPE_CHECKING, Callable

if TYPE_CHECKING:
    from ._doc import Doc


def with_transaction(doc: Doc, fn: Callable[[], None], is_apply_operations: bool = False) -> None:
    """Execute ``fn`` within a transaction.

    If the doc is already in an update, join the existing transaction.
    If idle, open a new transaction and auto-commit when the outermost
    ``with_transaction`` call returns.
    """
    stage = doc._lifecycle_stage

    if stage in ("change", "init", "disposed"):
        raise RuntimeError(
            f"Cannot trigger an update during the '{stage}' stage"
        )
    if stage == "normalize2":
        raise RuntimeError(
            "Strict mode: normalize listeners are not idempotent "
            "(they must not mutate the document on the second pass)"
        )

    is_new_tx = stage == "idle"
    if is_new_tx:
        doc._lifecycle_stage = "update"

    try:
        fn()
    except Exception:
        try:
            doc.abort()
        except Exception:
            pass
        if not is_apply_operations:
            raise
        return

    if is_new_tx:
        try:
            doc.force_commit()
        except Exception:
            try:
                doc.abort()
            except Exception:
                pass
            if not is_apply_operations:
                raise


@contextmanager
def transaction_context(doc: Doc) -> Generator[None, None, None]:
    """Context manager: ``with doc.transaction(): ...``

    Commits on clean exit, aborts on exception.
    """
    stage = doc._lifecycle_stage
    if stage in ("change", "init", "disposed"):
        raise RuntimeError(
            f"Cannot start a transaction during the '{stage}' stage"
        )

    is_new_tx = stage == "idle"
    if is_new_tx:
        doc._lifecycle_stage = "update"

    try:
        yield
    except Exception:
        try:
            doc.abort()
        except Exception:
            pass
        raise
    else:
        if is_new_tx:
            try:
                doc.force_commit()
            except Exception:
                try:
                    doc.abort()
                except Exception:
                    pass
                raise
