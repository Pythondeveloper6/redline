from __future__ import annotations

import os
import socket

from redline_core.conf import Settings
from redline_core.storage.safety import filesystem_type, prepare_path, probe_wal


def test_storage_path_role_hostname_and_pid_placeholders(tmp_path):
    settings = Settings(
        storage_path=str(tmp_path / "{role}-{hostname}-{pid}.db"), storage_role="worker"
    )
    path = prepare_path(settings)
    assert path == str(tmp_path / f"worker-{socket.gethostname()}-{os.getpid()}.db")


def test_wal_probe_creates_and_removes_its_database(tmp_path):
    target = str(tmp_path / "store.db")
    assert probe_wal(target) is True
    assert sorted(p.name for p in tmp_path.iterdir()) == []


def test_filesystem_type_is_a_linux_hint_not_a_requirement(tmp_path):
    kind = filesystem_type(str(tmp_path))
    assert kind is None or isinstance(kind, str)
