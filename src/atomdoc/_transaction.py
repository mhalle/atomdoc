"""Transaction context manager and with_transaction helper."""

from __future__ import annotations

from collections.abc import Callable, Generator
from contextlib import contextmanager
from typing import TYPE_CHECKING

from ._types import TransactionFlags

if TYPE_CHECKING:
    from ._doc import Doc


def _check_stage(doc: Doc) -> None:
    stage = doc._lifecycle_stage
    if stage in ("change", "disposed"):
        raise RuntimeError(
            f"Cannot trigger an update during the '{stage}' stage"
        )
    if stage == "normalize2":
        raise RuntimeError(
            "Strict mode: normalize listeners are not idempotent "
            "(they must not mutate the document on the second pass)"
        )


def _begin(doc: Doc, flags: TransactionFlags | None) -> bool:
    """Open a transaction if the doc is idle. Returns whether one was opened.

    A ``skip_undo`` transaction is always isolated: if a transaction is
    already open it is committed first, so the caller's own pending edits
    keep their undo entry and only the flagged work is excluded.
    """
    if flags is not None and flags.skip_undo and doc._lifecycle_stage == "update":
        doc.force_commit()

    is_new_tx = doc._lifecycle_stage == "idle"
    if is_new_tx:
        doc._lifecycle_stage = "update"
    if flags is not None and flags.skip_undo:
        doc._transaction_flags = TransactionFlags(skip_undo=True)
    return is_new_tx


def with_transaction(
    doc: Doc,
    fn: Callable[[], None],
    is_apply_operations: bool = False,
    flags: TransactionFlags | None = None,
) -> None:
    """Execute ``fn`` within a transaction.

    If the doc is already in an update, join the existing transaction.
    If idle, open a new transaction and auto-commit when the outermost
    ``with_transaction`` call returns. During the ``init`` stage (extension
    registration) mutations accumulate and are committed by the document
    constructor.

    A failure inside a *joined* transaction propagates to the caller
    without touching the document: only the outermost boundary rolls
    back, and it rolls back everything. (There are no savepoints, so a
    nested failure cannot be undone on its own.) ``is_apply_operations``
    therefore only swallows a failure when this call opened the
    transaction; inside an open transaction the failure always
    propagates so the enclosing transaction aborts as a whole.
    """
    _check_stage(doc)
    is_new_tx = _begin(doc, flags)

    try:
        fn()
    except Exception:
        if not is_new_tx:
            raise
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
def transaction_context(
    doc: Doc, flags: TransactionFlags | None = None
) -> Generator[None, None, None]:
    """Context manager: ``with doc.transaction(): ...``

    Commits on clean exit, aborts on exception. A nested block that fails
    re-raises without rolling anything back; the outermost block aborts
    the whole transaction (see :func:`with_transaction`).
    """
    _check_stage(doc)
    is_new_tx = _begin(doc, flags)

    try:
        yield
    except Exception:
        if is_new_tx:
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
