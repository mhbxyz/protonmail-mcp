from __future__ import annotations

import pytest

from protonmail_mcp.journal import MoveJournal


class FakeClock:
    def __init__(self) -> None:
        self.now = 100.0

    def __call__(self) -> float:
        return self.now


def test_record_and_get() -> None:
    journal = MoveJournal(clock=FakeClock())
    assert journal.last() is None
    first = journal.record("INBOX", "Archive", ["<a@x>"])
    second = journal.record("Archive", "Trash", ["<b@x>"])
    assert first.id == 1
    assert second.id == 2
    assert journal.last() == second
    assert journal.get(1) == first
    assert journal.get(99) is None


def test_undone_entries_are_skipped() -> None:
    journal = MoveJournal(clock=FakeClock())
    entry = journal.record("INBOX", "Archive", ["<a@x>"])
    journal.mark_undone(entry.id)
    assert journal.last() is None
    stored = journal.get(entry.id)
    assert stored is not None
    assert stored.undone is True


def test_bounded_journal_prunes_oldest() -> None:
    journal = MoveJournal(max_entries=2, clock=FakeClock())
    first = journal.record("INBOX", "Archive", ["<a@x>"])
    journal.record("INBOX", "Archive", ["<b@x>"])
    journal.record("INBOX", "Archive", ["<c@x>"])
    assert journal.get(first.id) is None
    assert journal.last() is not None


def test_clear_and_invalid_arguments() -> None:
    journal = MoveJournal(clock=FakeClock())
    journal.record("INBOX", "Archive", ["<a@x>"])
    journal.clear()
    assert journal.last() is None
    with pytest.raises(ValueError):
        MoveJournal(max_entries=0)
