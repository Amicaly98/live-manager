"""Regression tests for the offline lifecycle-endurance harness.

These tests deliberately exercise the failure edges that can otherwise make a
tool run look green: the first steady window, cleanup ownership, process
identity, suspend/resume symmetry, and command-result handling.
"""

import hashlib
import importlib.util
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


MODULE_PATH = Path(__file__).with_name("lifecycle_endurance.py")
SPEC = importlib.util.spec_from_file_location("lifecycle_endurance_under_test", MODULE_PATH)
ENDURANCE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(ENDURANCE)


def test_phase_b_records_baseline_before_any_sleep():
    events = []
    clock = {"t": 100.0}

    def monotonic():
        return clock["t"]

    def sleep(seconds):
        events.append(("sleep", seconds))
        clock["t"] += seconds

    def sample(phase):
        events.append(("sample", phase, clock["t"]))

    ENDURANCE._collect_phase_b(
        0.1, sample, sleep=sleep, monotonic=monotonic, window_seconds=5.0
    )

    assert events[0] == ("sample", "steady", 100.0)


def test_deterministic_coverage_includes_opening_middle_tail_stalls():
    coverage = ENDURANCE._assert_deterministic_window_coverage()

    assert coverage["steady_gap_count"] == 3
    assert coverage["steady_gaps"][0]["growth_bytes"] == 0


def test_cleanup_freezes_product_residual_and_fixture_rescue_failure():
    class Controller:
        video_process = None

        def stop_streaming(self):
            return False

    record = {
        "stream_established": True,
        "injected_gap_detected": True,
        "product_self_recovered": True,
        "steady_gaps": [],
        "tail_fresh": True,
        "product_stop_ok": False,
        "pusher_residual": [4242],
        "sample_count": 8,
        "suspend_ok": True,
        "resume_ok": True,
        "killed_ffmpeg": True,
        "old_owner_exited": True,
    }
    resources = {
        "record": record,
        "controller": Controller(),
        "controller_stopped": False,
        "ffmpeg_pids_seen": {4242},
    }

    with patch.object(ENDURANCE, "_pid_state",
                      return_value={"pid": 4242, "alive": False, "status": "absent"}):
        ENDURANCE._cleanup_product_resources(resources)

    assert record["product_pusher_residual"] == [4242]
    assert record["pusher_residual_after_cleanup"] == []
    assert record["fixture_rescue_needed"] is True
    assert record["pass"] is False


def _passing_record_for_cleanup_gate():
    return {
        "stream_established": True,
        "injected_gap_detected": True,
        "product_self_recovered": True,
        "steady_gaps": [],
        "tail_fresh": True,
        "product_stop_ok": True,
        "sample_count": 8,
        "steady_sample_count": 2,
        "suspend_ok": True,
        "resume_ok": True,
        "killed_ffmpeg": True,
        "old_owner_exited": True,
    }


def test_pass_requires_sink_thread_to_join():
    class Controller:
        video_process = None

        def stop_streaming(self):
            return True

    class NeverJoined:
        def join(self, timeout=None):
            return None

        def is_alive(self):
            return True

    record = _passing_record_for_cleanup_gate()
    resources = {
        "record": record,
        "controller": Controller(),
        "stats": {"stop": False},
        "sink_thread": NeverJoined(),
        "ffmpeg_pids_seen": set(),
    }

    with patch.object(ENDURANCE, "_pid_state",
                      return_value={"pid": 1, "alive": False, "status": "absent"}):
        ENDURANCE._cleanup_product_resources(resources)

    assert record["cleanup_sink_joined"] is False
    assert record["pass"] is False


def test_pass_requires_no_sink_error_and_sufficient_samples():
    class Controller:
        video_process = None

        def stop_streaming(self):
            return True

    record = _passing_record_for_cleanup_gate()
    record["sample_count"] = 1
    resources = {
        "record": record,
        "controller": Controller(),
        "stats": {"stop": False, "error": "synthetic sink failure"},
        "ffmpeg_pids_seen": set(),
    }

    ENDURANCE._cleanup_product_resources(resources)

    assert record["sink_error"] == "synthetic sink failure"
    assert record["pass"] is False


def test_metrics_collection_failure_is_unknown_not_gone():
    with patch.object(ENDURANCE, "_ps_output", return_value=""):
        metrics = ENDURANCE.proc_metrics(12345)

    assert metrics["alive"] is None
    assert metrics["status"] == "unknown"


def test_process_tree_tracks_grandchildren_by_pid_and_creation():
    root = {"pid": 100, "creation_time": "root"}
    table = [
        {"pid": 100, "parent_pid": 0, "creation_time": "root"},
        {"pid": 101, "parent_pid": 100, "creation_time": "child"},
        {"pid": 102, "parent_pid": 101, "creation_time": "grandchild"},
        {"pid": 999, "parent_pid": 100, "creation_time": "unrelated"},
    ]

    tree = ENDURANCE._bfs_process_tree(root, table)

    assert [(item["pid"], item["creation_time"]) for item in tree] == [
        (100, "root"),
        (101, "child"),
        (999, "unrelated"),
        (102, "grandchild"),
    ]
    assert ENDURANCE._same_process_identity(
        {"pid": 101, "creation_time": "child"},
        {"pid": 101, "creation_time": "reused"},
    ) is False


def test_suspend_failure_still_attempts_resume_in_finally():
    calls = []

    def suspend(pid, should_suspend):
        calls.append((pid, should_suspend))
        if should_suspend:
            raise RuntimeError("synthetic suspend failure")
        return True

    with patch.object(ENDURANCE, "_suspend_windows", side_effect=suspend):
        result = ENDURANCE._suspend_and_resume(77, 0.0, sleep=lambda _seconds: None)

    assert calls == [(77, True), (77, False)]
    assert result["suspend_ok"] is False
    assert result["resume_ok"] is True


def test_kill_success_requires_taskkill_returncode_and_old_owner_exit():
    class Process:
        pid = 88

        def __init__(self):
            self.exited = False

        def poll(self):
            return 0 if self.exited else None

        def wait(self, timeout=None):
            self.exited = True
            return 0

    proc = Process()
    failed_taskkill = SimpleNamespace(returncode=1, stdout="", stderr="denied")
    with patch.object(ENDURANCE.subprocess, "run", return_value=failed_taskkill):
        result = ENDURANCE._kill_owned_process(proc)

    assert result["taskkill_ok"] is False
    assert result["old_owner_exited"] is False
    assert result["killed_ffmpeg"] is False


def test_result_contains_source_and_tool_version():
    provenance = ENDURANCE._tool_provenance()

    assert provenance["tool_version"]
    assert len(provenance["source_sha256"]) == 64


def test_provenance_includes_backend_sources_and_packaged_executable_hash():
    provenance = ENDURANCE._tool_provenance()
    python_executable = ENDURANCE._executable_provenance([sys.executable])
    with tempfile.TemporaryDirectory(dir=str(ENDURANCE.REPO_ROOT),
                                      prefix="lifecycle-provenance-") as temp_dir:
        packaged_path = Path(temp_dir) / "synthetic-backend.exe"
        payload = b"synthetic packaged backend bytes\x00\x01"
        packaged_path.write_bytes(payload)
        executable = ENDURANCE._executable_provenance([str(packaged_path)])

    backend_hashes = provenance["backend_source_sha256"]
    assert any(path.endswith("app/core/live_controller.py") for path in backend_hashes)
    assert any(path.endswith("app/core/task_manager.py") for path in backend_hashes)
    assert any(path.endswith("app/core/db.py") for path in backend_hashes)
    assert any(path.endswith("app/core/email_sender.py") for path in backend_hashes)
    assert any(path.endswith("app/api/live.py") for path in backend_hashes)
    assert any(path.endswith("app/models/schemas.py") for path in backend_hashes)
    assert any(path.endswith("backend/run.py") for path in backend_hashes)
    assert python_executable["backend_exe_sha256"] is None
    assert executable["backend_exe_sha256"] == hashlib.sha256(payload).hexdigest()


def test_tail_fresh_scales_short_final_window_but_keeps_full_window_gate():
    short_healthy = [
        {"t": 100.0, "bytes": 0},
        {"t": 100.75, "bytes": 30_000},
    ]
    short_stall = [
        {"t": 100.0, "bytes": 0},
        {"t": 100.75, "bytes": 0},
    ]
    full_below_gate = [
        {"t": 100.0, "bytes": 0},
        {"t": 104.0, "bytes": ENDURANCE.GAP_MIN_BYTES - 1},
    ]

    assert ENDURANCE._tail_fresh(short_healthy) is True
    assert ENDURANCE._tail_fresh(short_stall) is False
    assert ENDURANCE._tail_fresh(full_below_gate) is False
