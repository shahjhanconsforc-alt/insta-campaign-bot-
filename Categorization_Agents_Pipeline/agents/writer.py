"""Agent 8 — Database Writer.

Persists finished verdicts into the existing MinIO table,
usernames_categories.txt, upserting by username so a re-run refreshes a handle
rather than duplicating it.

Two things it is careful about.

Batching: every save is a full read-modify-write of the object, so writing one
verdict at a time would be quadratic over thousands of rows. Verdicts are
buffered and flushed in batches (config.SAVE_EVERY), and the buffer is always
flushed at the end — including when a run is interrupted, so nothing decided is
thrown away.

Not breaking the panels: the table's existing `category` column stays a single
string, and the verdict's best category is what goes in it. The full list rides
in the new `categories` column and the tags in `tags`. That is what lets this
write into the table the UI already reads without touching the UI.
"""
from __future__ import annotations

import threading

from .. import config
from ..contracts import Verdict


class WriterAgent:
    name = "writer"

    def __init__(self, dry_run: bool = False, batch_size: int | None = None):
        self.dry_run = dry_run
        self.batch_size = batch_size or config.SAVE_EVERY
        self._buffer: list[dict] = []
        self._lock = threading.Lock()          # workers hand verdicts in concurrently
        self.written = 0
        self.errors: list[str] = []
        self.preview: list[dict] = []          # what a dry run would have saved

    def submit(self, verdict: Verdict) -> None:
        """Queue one verdict, flushing when the batch is full."""
        with self._lock:
            self._buffer.append(verdict.as_row())
            ready = len(self._buffer) >= self.batch_size
        if ready:
            self.flush()

    def flush(self) -> int:
        """Write whatever is buffered. Safe to call when empty."""
        with self._lock:
            batch, self._buffer = self._buffer, []
        if not batch:
            return 0
        if self.dry_run:
            self.preview.extend(batch)
            self.written += len(batch)
            return len(batch)
        try:
            from services.storage import insta_category_storage as cats

            result = cats.upsert(batch)
            self.written += result.get("added", 0) + result.get("updated", 0)
            return len(batch)
        except Exception as exc:  # noqa: BLE001 — a save hiccup mustn't end the run
            self.errors.append(f"{type(exc).__name__}: {exc}")
            return 0

    def report(self) -> dict:
        return {"agent": self.name, "written": self.written,
                "errors": list(self.errors), "dry_run": self.dry_run}
