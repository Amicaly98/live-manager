"""Read-only parser and bounded file reader for FFmpeg stderr progress."""

import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from app.core.ffmpeg_log_progress import (
    MAX_READ_BYTES,
    FFmpegLogProgressReader,
    capture_ffmpeg_log_baseline,
    parse_ffmpeg_time_updates,
)


FFMPEG = shutil.which("ffmpeg") or (
    "C:/ffmpeg/bin/ffmpeg.exe" if Path("C:/ffmpeg/bin/ffmpeg.exe").exists() else None
)
real_ffmpeg = unittest.skipIf(FFMPEG is None, "本机没有可用的 ffmpeg")


class FFmpegTimeParserTests(unittest.TestCase):
    def test_parses_cr_lf_and_unterminated_trailing_record(self):
        updates = parse_ffmpeg_time_updates(
            b"frame=1 time=00:00:01.25 speed=1.0x\r"
            b"frame=2 time=00:02:03.500\n"
            b"frame=3 time=01:00:00.01"
        )
        self.assertEqual(updates, (1.25, 123.5, 3600.01))

    def test_ignores_malformed_and_unavailable_time_fields(self):
        updates = parse_ffmpeg_time_updates(
            b"time=N/A\r"
            b"time=-00:00:01.00\r"
            b"time=00:60:01.00\r"
            b"time=00:00:60.00\r"
            b"xtime=00:00:02.00\r"
            b"time=00:00:2.00\r"
            b"time=00:00:03.25"
        )
        self.assertEqual(updates, (3.25,))


class FFmpegLogProgressReaderTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory(prefix="ffmpeg-log-progress-")
        self.addCleanup(self.temp_dir.cleanup)
        self.log_path = Path(self.temp_dir.name) / "ffmpeg.log"
        self.log_path.touch()

    def _reader(self, start_offset=None):
        baseline = capture_ffmpeg_log_baseline(
            self.log_path, start_offset=start_offset
        )
        return FFmpegLogProgressReader(self.log_path, baseline)

    def test_reads_only_new_bytes_after_attempt_start(self):
        self.log_path.write_bytes(b"frame=10 time=00:00:49.00\r")
        baseline = capture_ffmpeg_log_baseline(self.log_path)
        reader = FFmpegLogProgressReader(self.log_path, baseline)

        with self.log_path.open("ab") as log:
            log.write(b"frame=1 time=00:00:00.80\r")
        reading = reader.read()

        self.assertEqual(reading.outcome, "available")
        self.assertEqual(reading.media_seconds, 0.8)
        self.assertFalse(reading.advanced)

    def test_explicit_attempt_offset_can_be_supplied(self):
        self.log_path.write_bytes(b"stale time=00:00:50.00\r")
        attempt_offset = self.log_path.stat().st_size
        reader = self._reader(start_offset=attempt_offset)
        with self.log_path.open("ab") as log:
            log.write(b"time=00:00:02.25\r")

        reading = reader.read()
        self.assertEqual(reading.outcome, "available")
        self.assertEqual(reading.media_seconds, 2.25)

    def test_partial_record_is_carried_across_bounded_reads(self):
        reader = self._reader()
        with self.log_path.open("ab") as log:
            log.write(b"frame=1 time=00:00:01.25")
        first = reader.read()
        self.assertEqual(first.outcome, "available")
        self.assertEqual(first.media_seconds, 1.25)
        self.assertFalse(first.advanced)

        with self.log_path.open("ab") as log:
            log.write(b" speed=1.0x\r")
        continued = reader.read()
        self.assertEqual(continued.outcome, "available")
        self.assertEqual(continued.media_seconds, 1.25)
        self.assertFalse(continued.advanced)

        with self.log_path.open("ab") as log:
            log.write(b"frame=2 time=00:00:02.00")
        advanced = reader.read()
        self.assertEqual(advanced.outcome, "available")
        self.assertEqual(advanced.media_seconds, 2.0)
        self.assertTrue(advanced.advanced)

    def test_unknown_when_there_is_no_new_usable_record(self):
        reader = self._reader()
        no_data = reader.read()
        self.assertEqual(no_data.outcome, "unavailable")
        self.assertIsNone(no_data.media_seconds)

        with self.log_path.open("ab") as log:
            log.write(b"time=N/A\rtime=00:60:01.00\rmalformed\r")
        malformed = reader.read()
        self.assertEqual(malformed.outcome, "unavailable")
        self.assertIsNone(malformed.media_seconds)
        self.assertFalse(malformed.advanced)

    def test_rotation_establishes_new_baseline_before_accepting_progress(self):
        self.log_path.write_bytes(b"old-attempt time=00:00:40.00\r")
        reader = self._reader()
        replacement = self.log_path.with_suffix(".new")
        replacement.write_bytes(b"rotated old time=00:00:55.00\r")
        replacement.replace(self.log_path)

        rotated = reader.read()
        self.assertEqual(rotated.outcome, "new_baseline")
        self.assertIsNone(rotated.media_seconds)
        self.assertEqual(rotated.reason, "log_rotated")

        with self.log_path.open("ab") as log:
            log.write(b"new attempt time=00:00:00.75\r")
        current_attempt = reader.read()
        self.assertEqual(current_attempt.outcome, "available")
        self.assertEqual(current_attempt.media_seconds, 0.75)

    def test_truncation_establishes_new_baseline_and_skips_existing_bytes(self):
        self.log_path.write_bytes(b"x" * 128)
        reader = self._reader()
        self.log_path.write_bytes(b"truncated time=00:00:33.00\r")

        truncated = reader.read()
        self.assertEqual(truncated.outcome, "new_baseline")
        self.assertIsNone(truncated.media_seconds)
        self.assertEqual(truncated.reason, "log_truncated")

        with self.log_path.open("ab") as log:
            log.write(b"current time=00:00:00.50\r")
        current_attempt = reader.read()
        self.assertEqual(current_attempt.outcome, "available")
        self.assertEqual(current_attempt.media_seconds, 0.5)

    def test_same_file_truncate_and_regrow_is_detected_by_anchor(self):
        self.log_path.write_bytes(b"old attempt " + b"a" * 100)
        reader = self._reader()
        self.log_path.write_bytes(b"replacement " + b"b" * 200)

        rewritten = reader.read()
        self.assertEqual(rewritten.outcome, "new_baseline")
        self.assertIsNone(rewritten.media_seconds)
        self.assertEqual(rewritten.reason, "log_rewritten")

        with self.log_path.open("ab") as log:
            log.write(b"current time=00:00:01.50\r")
        current_attempt = reader.read()
        self.assertEqual(current_attempt.outcome, "available")
        self.assertEqual(current_attempt.media_seconds, 1.5)

    def test_each_poll_reads_at_most_32_kib(self):
        reader = self._reader(start_offset=0)
        self.log_path.write_bytes(b"time=00:00:01.00\r" * 5000)

        reading = reader.read()
        self.assertEqual(reading.outcome, "available")
        self.assertEqual(reading.bytes_read, MAX_READ_BYTES)
        self.assertEqual(reader.offset, MAX_READ_BYTES)
        self.assertLessEqual(reading.bytes_read, 32 * 1024)

    @real_ffmpeg
    def test_real_local_ffmpeg_stderr_stats_are_readable(self):
        reader = self._reader()
        command = [
            FFMPEG,
            "-hide_banner",
            "-nostdin",
            "-f",
            "lavfi",
            "-i",
            "testsrc=size=16x16:rate=5:duration=1.2",
            "-f",
            "null",
            "-",
        ]
        with self.log_path.open("ab") as log:
            completed = subprocess.run(
                command,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=log,
                timeout=15,
                check=False,
            )

        self.assertEqual(completed.returncode, 0)
        reading = reader.read()
        self.assertEqual(reading.outcome, "available")
        self.assertIsNotNone(reading.media_seconds)
        self.assertGreater(reading.media_seconds, 0)


if __name__ == "__main__":
    unittest.main()
