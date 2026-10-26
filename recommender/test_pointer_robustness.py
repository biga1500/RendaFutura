import pytest

from . import collect_market_data as collector
from .test_market_data import dataset_files


def test_transient_pointer_lock_retries_atomically_without_deleting_current(tmp_path, monkeypatch):
    first = tmp_path / "snapshots/first"
    report = dataset_files(first, price=20)
    second = tmp_path / "snapshots/second"
    dataset_files(second, price=40)
    collector.publish_pointer(tmp_path, first, report)
    previous = (tmp_path / "current.json").read_bytes()
    original = collector.os.replace
    calls, delays = [], []

    def replace(source, target):
        calls.append((source, target))
        if len(calls) < 3:
            assert target.read_bytes() == previous
            raise PermissionError("Synthetic temporary pointer lock")
        return original(source, target)

    monkeypatch.setattr(collector.os, "replace", replace)
    monkeypatch.setattr(collector.time, "sleep", delays.append)
    collector.publish_pointer(tmp_path, second, report)
    assert len(calls) == 3
    assert delays == [.05, .1]
    assert (tmp_path / "current.json").read_bytes() != previous
    assert list(tmp_path.glob(".current-*.tmp")) == []


def test_persistent_pointer_lock_preserves_old_snapshot_and_reports_failure(tmp_path, monkeypatch):
    first = tmp_path / "snapshots/first"
    report = dataset_files(first, price=20)
    second = tmp_path / "snapshots/second"
    dataset_files(second, price=40)
    collector.publish_pointer(tmp_path, first, report)
    previous = (tmp_path / "current.json").read_bytes()
    calls = []

    def replace(*args):
        calls.append(args)
        raise PermissionError("Synthetic persistent pointer lock")

    monkeypatch.setattr(collector.os, "replace", replace)
    monkeypatch.setattr(collector.time, "sleep", lambda _: None)
    with pytest.raises(PermissionError):
        collector.publish_pointer(tmp_path, second, report)
    assert len(calls) == 5
    assert (tmp_path / "current.json").read_bytes() == previous
    assert list(tmp_path.glob(".current-*.tmp")) == []
