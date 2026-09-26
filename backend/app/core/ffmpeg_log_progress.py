"""Bounded, read-only progress parsing for FFmpeg's stderr statistics log.

The reader is intentionally synchronous and keeps only a small cursor and the
unfinished tail of the current stats record. Callers capture a baseline before
starting each FFmpeg attempt so older log entries cannot be mistaken for new
progress.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Literal


MAX_READ_BYTES = 32 * 1024
MAX_PENDING_RECORD_BYTES = 4 * 1024
ANCHOR_BYTES = 64

_TIME_TOKEN = re.compile(
    rb"(?<![A-Za-z0-9_])time=([0-9]{2,10}):([0-5][0-9]):"
    rb"([0-5][0-9])(?:\.([0-9]{1,9}))?(?![A-Za-z0-9_.])"
)

Outcome = Literal["available", "unavailable", "new_baseline"]
FileIdentity = tuple[int, int]


@dataclass(frozen=True)
class FFmpegLogBaseline:
    """File cursor captured immediately before one FFmpeg attempt."""

    offset: int
    file_identity: FileIdentity | None
    anchor: bytes = b""


@dataclass(frozen=True)
class FFmpegProgressReading:
    """One bounded poll result; unknown progress is represented by ``None``."""

    outcome: Outcome
    media_seconds: float | None
    advanced: bool
    bytes_read: int = 0
    reason: str | None = None


def _file_identity(stat_result: os.stat_result) -> FileIdentity:
    return int(stat_result.st_dev), int(stat_result.st_ino)


def _capture_anchor(
    path: Path, offset: int, expected_identity: FileIdentity
) -> bytes | None:
    """Read a small tail fingerprint used to detect truncate-and-regrow."""

    length = min(ANCHOR_BYTES, offset)
    if length == 0:
        return b""
    try:
        with path.open("rb", buffering=0) as stream:
            before = os.fstat(stream.fileno())
            if _file_identity(before) != expected_identity or before.st_size < offset:
                return None
            stream.seek(offset - length)
            anchor = stream.read(length)
            after = os.fstat(stream.fileno())
    except OSError:
        return None
    if (
        len(anchor) != length
        or _file_identity(after) != expected_identity
        or after.st_size < offset
    ):
        return None
    return anchor


def capture_ffmpeg_log_baseline(
    path: str | os.PathLike[str], *, start_offset: int | None = None
) -> FFmpegLogBaseline:
    """Capture the current log identity and per-attempt start offset.

    If the log does not exist or cannot be inspected yet, the returned baseline
    has no identity. A later reader poll will establish a new baseline and will
    not consume bytes already present in that file.
    """

    if start_offset is not None and (
        isinstance(start_offset, bool)
        or not isinstance(start_offset, int)
        or start_offset < 0
    ):
        raise ValueError("start_offset must be a non-negative integer")

    try:
        info = Path(path).stat()
    except OSError:
        return FFmpegLogBaseline(start_offset or 0, None)

    offset = info.st_size if start_offset is None else start_offset
    identity = _file_identity(info)
    anchor = _capture_anchor(Path(path), offset, identity)
    if anchor is None:
        # A moving or inaccessible file is not a trustworthy attempt baseline.
        return FFmpegLogBaseline(offset, None)
    return FFmpegLogBaseline(offset, identity, anchor)


def parse_ffmpeg_time_updates(data: bytes) -> tuple[float, ...]:
    """Return valid ``time=HH:MM:SS.xx`` values in CR/LF-separated records.

    The final record is parsed even if it has no trailing CR or LF, which is
    common while FFmpeg is still writing its in-place stderr status line.
    Invalid tokens, ``time=N/A``, and out-of-range minute/second fields are
    ignored.
    """

    if not isinstance(data, bytes):
        raise TypeError("data must be bytes")

    updates: list[float] = []
    for record in re.split(rb"[\r\n]", data):
        for match in _TIME_TOKEN.finditer(record):
            hours = int(match.group(1))
            minutes = int(match.group(2))
            seconds = int(match.group(3))
            fraction = match.group(4) or b""
            fractional_seconds = (
                int(fraction) / (10 ** len(fraction)) if fraction else 0.0
            )
            updates.append(hours * 3600 + minutes * 60 + seconds + fractional_seconds)
    return tuple(updates)


class FFmpegLogProgressReader:
    """Read only new stderr-log bytes, with a 32 KiB ceiling per ``read``."""

    def __init__(
        self,
        path: str | os.PathLike[str],
        baseline: FFmpegLogBaseline,
    ) -> None:
        if baseline.offset < 0:
            raise ValueError("baseline offset must be non-negative")
        self.path = Path(path)
        self._offset = baseline.offset
        self._file_identity = baseline.file_identity
        self._anchor = baseline.anchor
        self._pending_record = b""
        self._last_media_seconds: float | None = None

    @property
    def offset(self) -> int:
        """The next byte position this reader will inspect."""

        return self._offset

    def _new_baseline(
        self, info: os.stat_result, reason: str
    ) -> FFmpegProgressReading:
        self._offset = info.st_size
        identity = _file_identity(info)
        anchor = _capture_anchor(self.path, self._offset, identity)
        self._file_identity = identity if anchor is not None else None
        self._anchor = anchor or b""
        self._pending_record = b""
        self._last_media_seconds = None
        return FFmpegProgressReading("new_baseline", None, False, reason=reason)

    def _missing_file(self, reason: str) -> FFmpegProgressReading:
        # Forget the old identity. If a file appears again, the next poll will
        # establish a baseline and skip its already-written contents.
        self._file_identity = None
        self._anchor = b""
        self._pending_record = b""
        self._last_media_seconds = None
        return FFmpegProgressReading("unavailable", None, False, reason=reason)

    def read(self) -> FFmpegProgressReading:
        """Read at most 32 KiB of new log data and report its latest valid time."""

        try:
            path_info = self.path.stat()
        except OSError:
            return self._missing_file("log_unavailable")

        if self._file_identity is None:
            return self._new_baseline(path_info, "baseline_unavailable")
        if _file_identity(path_info) != self._file_identity:
            return self._new_baseline(path_info, "log_rotated")
        if path_info.st_size < self._offset:
            return self._new_baseline(path_info, "log_truncated")
        if path_info.st_size == self._offset:
            return FFmpegProgressReading(
                "unavailable", None, False, reason="no_new_data"
            )

        try:
            # Unbuffered I/O makes the size limit apply to bytes fetched from
            # the file, not just the number returned to the caller.
            with self.path.open("rb", buffering=0) as stream:
                opened_info = os.fstat(stream.fileno())
                opened_identity = _file_identity(opened_info)
                if opened_identity != self._file_identity:
                    try:
                        current = self.path.stat()
                    except OSError:
                        return self._missing_file("log_changed_during_read")
                    return self._new_baseline(current, "log_rotated")
                if opened_info.st_size < self._offset:
                    return self._new_baseline(opened_info, "log_truncated")

                read_start = self._offset - len(self._anchor)
                requested = min(MAX_READ_BYTES, opened_info.st_size - read_start)
                if requested <= len(self._anchor):
                    return FFmpegProgressReading(
                        "unavailable", None, False, reason="no_new_data"
                    )
                stream.seek(read_start)
                data = stream.read(requested)
                after_read = os.fstat(stream.fileno())
        except OSError:
            return FFmpegProgressReading(
                "unavailable", None, False, reason="log_read_error"
            )

        try:
            current_path_info = self.path.stat()
        except OSError:
            return self._missing_file("log_changed_during_read")

        if _file_identity(current_path_info) != self._file_identity:
            return self._new_baseline(current_path_info, "log_rotated")
        if (
            after_read.st_size < read_start
            or after_read.st_size < read_start + len(data)
        ):
            return self._new_baseline(after_read, "log_truncated")
        if len(data) < requested:
            # The file shrank or changed while being read. Do not trust a
            # partial chunk whose record boundaries may have been lost.
            return self._new_baseline(after_read, "log_changed_during_read")
        if not data:
            return FFmpegProgressReading(
                "unavailable", None, False, reason="no_new_data"
            )

        if data[: len(self._anchor)] != self._anchor:
            return self._new_baseline(current_path_info, "log_rewritten")

        new_data = data[len(self._anchor) :]
        if not new_data:
            return FFmpegProgressReading(
                "unavailable", None, False, reason="no_new_data"
            )

        self._offset += len(new_data)
        self._anchor = (self._anchor + new_data)[-ANCHOR_BYTES:]
        combined = self._pending_record + new_data
        last_separator = max(combined.rfind(b"\r"), combined.rfind(b"\n"))
        if last_separator >= 0:
            pending = combined[last_separator + 1 :]
        else:
            pending = combined
        self._pending_record = pending[-MAX_PENDING_RECORD_BYTES:]

        updates = parse_ffmpeg_time_updates(combined)
        if not updates:
            return FFmpegProgressReading(
                "unavailable",
                None,
                False,
                bytes_read=len(data),
                reason="no_usable_time_record",
            )

        media_seconds = updates[-1]
        advanced = (
            self._last_media_seconds is not None
            and media_seconds > self._last_media_seconds
        )
        self._last_media_seconds = media_seconds
        return FFmpegProgressReading(
            "available", media_seconds, advanced, bytes_read=len(data)
        )
