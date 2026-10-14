import fcntl

import pytest

from zog.box_control.locking import ProjectLock


def test_project_mutation_lock_is_exclusive_across_process_handles(tmp_path):
    lock_path = tmp_path / "state" / "box-control.lock"
    with ProjectLock(lock_path):
        with lock_path.open("a+") as competing:
            with pytest.raises(BlockingIOError):
                fcntl.flock(competing.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
