"""Safe, deterministic image discovery, deduplication, and output processing."""

from __future__ import annotations

import csv
from datetime import datetime
import hashlib
import json
import logging
import os
import platform
import re
import select
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path, PureWindowsPath
from typing import Callable, Final, Iterable
from uuid import uuid4

from PIL import Image, ImageOps
from pillow_heif import register_heif_opener

from dpf_files.visual import is_distinctive_signature, nearest_visual_match, visual_signature

SUPPORTED_EXTENSIONS: Final[frozenset[str]] = frozenset(
    {".jpg", ".jpeg", ".png", ".bmp", ".heic", ".heif"}
)
HEIF_EXTENSIONS: Final[frozenset[str]] = frozenset({".heic", ".heif"})
VIDEO_EXTENSIONS: Final[frozenset[str]] = frozenset(
    {".mp4", ".mov", ".m4v", ".avi", ".mkv", ".wmv"}
)
FAMILY_ALBUM_DIRECTORIES: Final[frozenset[str]] = frozenset({"frame_1080x1920", "frame_1920x1080"})
HASH_CHUNK_SIZE: Final[int] = 1024 * 1024
WSL_CLOUD_READ_TIMEOUT_SECONDS: Final[int] = 30
MIN_CAPTURE_YEAR: Final[int] = 1900
MACHINE_DATE_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"(?<!\d)(?P<year>(?:19|20)\d{2})[._-]?(?P<month>\d{2})[._-]?(?P<day>\d{2})"
    r"(?:[T _-]?(?P<hour>[01]\d|2[0-3])(?P<minute>[0-5]\d)(?P<second>[0-5]\d))?(?!\d)"
)
MONTH_NAMES: Final[dict[str, int]] = {
    "jan": 1, "january": 1, "feb": 2, "february": 2, "mar": 3, "march": 3,
    "apr": 4, "april": 4, "may": 5, "jun": 6, "june": 6,
    "jul": 7, "july": 7, "aug": 8, "august": 8, "sep": 9, "sept": 9, "september": 9,
    "oct": 10, "october": 10, "nov": 11, "november": 11, "dec": 12, "december": 12,
}
MONTH_NAME_PATTERN: Final[str] = "|".join(sorted(MONTH_NAMES, key=len, reverse=True))
HUMAN_MONTH_FIRST_PATTERN: Final[re.Pattern[str]] = re.compile(
    rf"(?<![a-z])(?P<month>{MONTH_NAME_PATTERN})[ ._-]+(?P<day>\d{{1,2}})(?:st|nd|rd|th)?"
    rf"[ ,._-]+(?P<year>(?:19|20)\d{{2}})(?!\d)",
    re.IGNORECASE,
)
HUMAN_DAY_FIRST_PATTERN: Final[re.Pattern[str]] = re.compile(
    rf"(?<!\d)(?P<day>\d{{1,2}})(?:st|nd|rd|th)?[ ._-]+(?P<month>{MONTH_NAME_PATTERN})"
    rf"[ ,._-]+(?P<year>(?:19|20)\d{{2}})(?!\d)",
    re.IGNORECASE,
)
FOLDER_YEAR_PATTERN: Final[re.Pattern[str]] = re.compile(r"^(?:19|20)\d{2}$")
LOGGER = logging.getLogger(__name__)


class SafetyError(RuntimeError):
    """Raised when a requested operation could affect source data or existing output."""


@dataclass(frozen=True)
class PreparationConfig:
    """Immutable settings for one preparation run.

    Attributes:
        source: Primary root directory containing authoritative photos.
        additional_sources: Additional root directories to scan for photos.
        output: Root directory that will contain ``images`` and ``reports``.
        overwrite_output: Whether existing utility-created output may be rebuilt.
        jpeg_quality: JPEG encoding quality used only for HEIC/HEIF files.
        max_files: Maximum supported images to process, or ``None`` for all.
        dry_run: Whether to produce reports without modifying output images.
        video_output: Optional directory where discovered video files are archived.
    """

    source: Path
    output: Path
    overwrite_output: bool = False
    jpeg_quality: int = 92
    max_files: int | None = None
    dry_run: bool = False
    additional_sources: tuple[Path, ...] = ()
    video_output: Path | None = None

    @property
    def source_roots(self) -> tuple[Path, ...]:
        """Return every configured source root in deterministic order."""
        return (self.source, *self.additional_sources)


@dataclass(frozen=True)
class ErrorRecord:
    """One recoverable failure encountered while processing a source file."""

    source_path: Path
    operation: str
    exception_type: str
    message: str


@dataclass(frozen=True)
class ManifestRecord:
    """Traceability record for one successful or planned output image."""

    output_filename: str
    output_path: Path
    source_path: Path
    source_filename: str
    source_extension: str
    sha256: str
    source_bytes: int
    output_bytes: int | None
    action: str
    status: str
    playback_folder: str
    canonical_date: str
    date_source: str

    @property
    def capture_date(self) -> str:
        """Return the selected capture date using the audit-report terminology."""
        return self.canonical_date


@dataclass(frozen=True)
class DateReshuffleResult:
    """Outcome of regrouping already-prepared photos by corrected capture dates."""

    output: Path
    images_reshuffled: int
    manifest_path: Path

    def summary_text(self) -> str:
        """Return a concise operator-facing result."""
        return (
            "Date reshuffle completed normally\n"
            f"Output: {self.output}\n"
            f"Images reshuffled: {self.images_reshuffled}\n"
            f"Manifest: {self.manifest_path}\n"
        )


@dataclass(frozen=True)
class DuplicateRecord:
    """Record linking an exact duplicate to its retained source."""

    duplicate_path: Path
    retained_path: Path
    sha256: str
    size: int
    duplicate_type: str = "exact_content"


@dataclass(frozen=True)
class VideoRecord:
    """Traceability record for a video moved to the configured archive."""

    source_path: Path
    archived_path: Path
    status: str


@dataclass
class VideoArchiveResult:
    """Outcome of a standalone configured video archival run."""

    sources: tuple[Path, ...]
    video_output: Path
    dry_run: bool
    videos: list[VideoRecord] = field(default_factory=list)
    errors: list[ErrorRecord] = field(default_factory=list)
    elapsed_seconds: float = 0.0

    def summary_text(self) -> str:
        """Return a human-readable summary for standalone video archival."""
        status = "dry run completed" if self.dry_run else "run completed normally"
        return "\n".join(
            [
                "AGPTEK video archival summary",
                f"Status: {status}",
                "Sources:",
                *(f"- {source}" for source in self.sources),
                f"Video archive: {self.video_output}",
                f"Videos archived: {len(self.videos)}",
                f"Errors: {len(self.errors)}",
                f"Elapsed time: {self.elapsed_seconds:.2f} seconds",
            ]
        ) + "\n"


@dataclass
class PreparationResult:
    """Counters and report records generated by a preparation run."""

    sources: tuple[Path, ...]
    output: Path
    dry_run: bool
    max_files: int | None = None
    candidates_discovered: int = 0
    candidates: int = 0
    unique_images: int = 0
    images_written: int = 0
    duplicates_skipped: int = 0
    conversions_completed: int = 0
    unchanged_files_copied: int = 0
    output_bytes: int = 0
    elapsed_seconds: float = 0.0
    errors: list[ErrorRecord] = field(default_factory=list)
    manifest: list[ManifestRecord] = field(default_factory=list)
    duplicates: list[DuplicateRecord] = field(default_factory=list)
    videos: list[VideoRecord] = field(default_factory=list)

    @property
    def source(self) -> Path:
        """Return the primary source root for backward-compatible callers."""
        return self.sources[0]

    def summary_text(self) -> str:
        """Return the human-readable summary written at the end of a run."""
        completion = "dry run completed" if self.dry_run else "run completed normally"
        lines = [
            "AGPTEK media preparation summary",
            f"Status: {completion}",
            "Sources:",
            *(f"- {source}" for source in self.sources),
            f"Output: {self.output}",
            f"Candidate images discovered: {self.candidates_discovered}",
            f"Candidate images selected: {self.candidates}",
            f"Unique images: {self.unique_images}",
            f"Images written: {self.images_written}",
            f"Exact duplicates skipped: {self.duplicates_skipped}",
            f"HEIC/HEIF conversions completed: {self.conversions_completed}",
            f"Unchanged files copied: {self.unchanged_files_copied}",
            f"Videos archived: {len(self.videos)}",
            f"Errors: {len(self.errors)}",
            f"Output size: {self.output_bytes} bytes",
            f"Elapsed time: {self.elapsed_seconds:.2f} seconds",
        ]
        if self.max_files is not None:
            lines.append(f"Processing limit: first {self.max_files} candidate images")
        if self.dry_run:
            lines.append("No output images were created, replaced, or deleted.")
        return "\n".join(lines) + "\n"


def prepare_library(config: PreparationConfig) -> PreparationResult:
    """Prepare a flat, deduplicated image directory and archive configured videos.

    Image sources are only opened for reading. When ``video_output`` is set,
    supported videos are moved there. Per-file failures are recorded in
    ``errors.csv`` and do not prevent remaining files from being processed.
    """
    started_at = time.monotonic()
    sources, output, video_output = _validate_config(config)
    images_dir = output / "photos"
    legacy_images_dir = output / "images"
    reports_dir = output / "reports"
    _validate_existing_output(images_dir, config.overwrite_output)
    _validate_existing_output(legacy_images_dir, config.overwrite_output)
    result = PreparationResult(
        sources=sources, output=output, dry_run=config.dry_run, max_files=config.max_files
    )

    discovered_candidates = list(_discover_candidates(sources, output))
    result.candidates_discovered = len(discovered_candidates)
    candidates = discovered_candidates[: config.max_files]
    result.candidates = len(candidates)
    LOGGER.info(
        "Discovered %d candidate image files; selected %d for this run.",
        result.candidates_discovered,
        result.candidates,
    )

    retained: list[tuple[Path, str, int]] = []
    hashes: dict[str, Path] = {}
    visual_hashes: dict[int, Path] = {}
    cloud_worker = _CloudFingerprintWorker()
    try:
        for index, candidate in enumerate(candidates, start=1):
            digest, size, signature, fingerprint_error = _fingerprint_candidate(candidate, cloud_worker)
            if fingerprint_error is not None:
                _record_error(result, candidate, "deferred_cloud_file", fingerprint_error)
                continue
            if digest is None or size is None:
                _record_error(result, candidate, "deferred_cloud_file", OSError("No file fingerprint produced."))
                continue

            retained_path = hashes.get(digest)
            if retained_path is not None:
                result.duplicates.append(DuplicateRecord(candidate, retained_path, digest, size))
                result.duplicates_skipped += 1
                LOGGER.info("[%d/%d] duplicate skipped: %s", index, result.candidates, candidate)
                continue
            if signature is not None and is_distinctive_signature(signature):
                visual_match = nearest_visual_match(signature, visual_hashes)
                if visual_match is not None:
                    _, visual_retained_path, _ = visual_match
                    result.duplicates.append(
                        DuplicateRecord(candidate, visual_retained_path, digest, size, "visual_content")
                    )
                    result.duplicates_skipped += 1
                    LOGGER.info("[%d/%d] visual duplicate skipped: %s", index, result.candidates, candidate)
                    continue
            hashes[digest] = candidate
            if signature is not None and is_distinctive_signature(signature):
                visual_hashes.setdefault(signature, candidate)
            retained.append((candidate, digest, size))
            LOGGER.info("[%d/%d] queued: %s", index, result.candidates, candidate)
    finally:
        cloud_worker.close()

    result.unique_images = len(retained)
    _prepare_output_directories(images_dir, legacy_images_dir, reports_dir, config)
    file_handler = _configure_file_logging(reports_dir)
    try:
        if video_output is not None:
            _archive_videos(sources, video_output, config.dry_run, result)
        for folder, entries in _playback_groups(
            retained,
            sources,
            date_error_handler=lambda path, error: _record_error(result, path, "capture_date", error),
        ):
            for sequence, (candidate, digest, source_size, date_value, date_source) in enumerate(entries, start=1):
                _process_retained_file(candidate, digest, source_size, sequence, images_dir / folder,
                    folder, date_value, date_source, config, result)

        result.elapsed_seconds = time.monotonic() - started_at
        _write_reports(reports_dir, result)
    finally:
        LOGGER.removeHandler(file_handler)
        file_handler.close()
    return result


def archive_videos(config: PreparationConfig) -> VideoArchiveResult:
    """Archive configured videos without rebuilding the generated image output."""
    started_at = time.monotonic()
    sources, _, video_output = _validate_config(config)
    if video_output is None:
        raise ValueError("video_output must be configured for video archival.")
    result = VideoArchiveResult(sources, video_output, config.dry_run)
    _archive_videos(sources, video_output, config.dry_run, result)
    result.elapsed_seconds = time.monotonic() - started_at
    return result


def reshuffle_output_dates(config: PreparationConfig) -> DateReshuffleResult:
    """Regroup existing USB photos using corrected dates without reprocessing pixels.

    The current manifest is treated as an immutable inventory: every listed
    output must exist under ``photos`` and every physical output must be
    listed. A complete replacement tree is copied beside the existing one
    before the original tree and manifest are atomically replaced.
    """
    sources, output, _ = _validate_config(config)
    images_dir = output / "photos"
    reports_dir = output / "reports"
    manifest_path = reports_dir / "manifest.csv"
    if not images_dir.is_dir() or not manifest_path.is_file():
        raise SafetyError("Date reshuffle requires existing photos and reports/manifest.csv output.")

    rows = _read_reshuffle_manifest(manifest_path, images_dir)
    existing_outputs = {
        Path(root) / filename
        for root, _, filenames in os.walk(images_dir)
        for filename in filenames
    }
    manifest_outputs = {Path(row["output_path"]) for row in rows}
    if existing_outputs != manifest_outputs:
        raise SafetyError("Manifest and photos output differ; run a full rebuild instead of reshuffling.")

    retained: list[tuple[Path, str, int]] = []
    reshuffle_dates: dict[Path, tuple[datetime, str]] = {}
    for row in rows:
        source_path = Path(row["source_path"])
        try:
            retained.append((source_path, row["sha256"], int(row["source_bytes"])))
        except ValueError as error:
            raise SafetyError(f"Manifest has an invalid source size for {source_path}") from error
        reshuffle_dates[source_path] = _reshuffle_date(row, source_path)

    grouped = _playback_groups(retained, sources, lambda path: reshuffle_dates[path])
    rows_by_source = {Path(row["source_path"]): row for row in rows}
    staging_dir = output / f".photos-date-reshuffle-{uuid4().hex}"
    staged_manifest = reports_dir / f".manifest-date-reshuffle-{uuid4().hex}.csv"
    backup_dir = output / f".photos-before-date-reshuffle-{uuid4().hex}"
    reshuffled_records: list[ManifestRecord] = []
    try:
        for folder, entries in grouped:
            for sequence, (source_path, digest, source_size, date_value, date_source) in enumerate(entries, start=1):
                original = rows_by_source[source_path]
                old_output = Path(original["output_path"])
                output_filename = f"{sequence:04d}{old_output.suffix.lower()}"
                staged_output = staging_dir / folder / output_filename
                staged_output.parent.mkdir(parents=True, exist_ok=True)
                _stage_output_file(old_output, staged_output)
                reshuffled_records.append(
                    ManifestRecord(
                        output_filename=output_filename,
                        output_path=images_dir / folder / output_filename,
                        source_path=source_path,
                        source_filename=original["source_filename"],
                        source_extension=original["source_extension"],
                        sha256=digest,
                        source_bytes=source_size,
                        output_bytes=_manifest_output_size(original, old_output),
                        action=original["action"],
                        status=original["status"],
                        playback_folder=folder,
                        canonical_date=date_value.isoformat(),
                        date_source=date_source,
                    )
                )
        _write_manifest(staged_manifest, reshuffled_records)
        images_dir.replace(backup_dir)
        try:
            staging_dir.replace(images_dir)
            staged_manifest.replace(manifest_path)
        except Exception:
            if images_dir.exists():
                images_dir.replace(staging_dir)
            backup_dir.replace(images_dir)
            raise
        shutil.rmtree(backup_dir)
    except Exception:
        if staging_dir.exists():
            shutil.rmtree(staging_dir)
        if staged_manifest.exists():
            staged_manifest.unlink()
        raise
    return DateReshuffleResult(output, len(reshuffled_records), manifest_path)


def _stage_output_file(source: Path, destination: Path) -> None:
    """Stage one existing output using a hard link when the volume supports it."""
    try:
        os.link(source, destination)
    except OSError:
        shutil.copy2(source, destination)


def _manifest_output_size(row: dict[str, str], output_path: Path) -> int:
    """Return the recorded output size without forcing a cloud-backed stat call."""
    value = row.get("output_bytes", "")
    try:
        return int(value)
    except ValueError as error:
        raise SafetyError(f"Manifest has an invalid output size for {output_path}") from error


def _validate_config(config: PreparationConfig) -> tuple[tuple[Path, ...], Path, Path | None]:
    """Validate paths and option values before any output changes occur."""
    if not 1 <= config.jpeg_quality <= 100:
        raise ValueError("jpeg_quality must be between 1 and 100")
    if config.max_files is not None and config.max_files < 1:
        raise ValueError("max_files must be a positive integer or None")
    sources = tuple(source.expanduser().resolve(strict=False) for source in config.source_roots)
    if len(set(sources)) != len(sources):
        raise SafetyError("Source directories must be unique.")
    for source in sources:
        if not source.is_dir():
            raise SafetyError(f"Source directory does not exist or is not a directory: {source}")
    output = config.output.expanduser().resolve(strict=False)
    for source in sources:
        if output == source:
            raise SafetyError("Output directory must not be the same as a source directory.")
        if source.is_relative_to(output):
            raise SafetyError("Output directory must not be a parent of a source directory.")
    for index, source in enumerate(sources):
        if any(
            source.is_relative_to(other) or other.is_relative_to(source)
            for other in sources[:index]
        ):
            raise SafetyError("Source directories must not contain one another.")
    if config.video_output is None:
        return sources, output, None
    video_output = config.video_output.expanduser().resolve(strict=False)
    if video_output.exists() and not video_output.is_dir():
        raise SafetyError(f"Video output path is not a directory: {video_output}")
    if video_output == output:
        raise SafetyError("Video output directory must not be the image output directory.")
    return sources, output, video_output


def _validate_existing_output(images_dir: Path, overwrite_output: bool) -> None:
    """Refuse a non-empty image output folder unless explicit consent was supplied."""
    if images_dir.exists() and not images_dir.is_dir():
        raise SafetyError(f"Output images path is not a directory: {images_dir}")
    if images_dir.is_dir() and any(images_dir.iterdir()) and not overwrite_output:
        raise SafetyError(
            f"Output images folder is non-empty: {images_dir}. "
            "Use --overwrite-output to rebuild it."
        )


def _discover_candidates(sources: Iterable[Path], output: Path) -> Iterable[Path]:
    """Yield supported files from every source in stable, path-based order."""
    candidates: set[Path] = set()
    for source in sources:
        for path in source.rglob("*"):
            if _is_in_output_tree(path, output):
                continue
            if path.is_file() and path.suffix.lower() in SUPPORTED_EXTENSIONS:
                candidates.add(path)
    return iter(sorted(candidates, key=lambda item: str(item).casefold()))


def _discover_videos(sources: Iterable[Path], video_output: Path) -> Iterable[Path]:
    """Yield supported video files outside their configured archive directory."""
    videos: set[Path] = set()
    for source in sources:
        for path in source.rglob("*"):
            if _is_in_output_tree(path, video_output):
                continue
            if path.is_file() and path.suffix.lower() in VIDEO_EXTENSIONS:
                videos.add(path)
    return iter(sorted(videos, key=lambda item: str(item).casefold()))


def _is_in_output_tree(path: Path, output: Path) -> bool:
    """Return whether ``path`` is in an output tree nested in the source."""
    try:
        path.relative_to(output)
    except ValueError:
        return False
    return True


def _hash_file(path: Path) -> tuple[str, int]:
    """Return a streaming SHA-256 digest and source byte count."""
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as source_file:
        for chunk in iter(lambda: source_file.read(HASH_CHUNK_SIZE), b""):
            digest.update(chunk)
            size += len(chunk)
    return digest.hexdigest(), size


def _fingerprint_candidate(
    path: Path, cloud_worker: "_CloudFingerprintWorker"
) -> tuple[str | None, int | None, int | None, Exception | None]:
    """Return fingerprints while isolating potentially stalled WSL cloud-file reads."""
    if not _is_wsl_cloud_path(path):
        try:
            digest, size = _hash_file(path)
            return digest, size, visual_signature(path), None
        except Exception as error:  # Image decoders and mounted files can raise varied errors.
            return None, None, None, error

    return cloud_worker.fingerprint(path)


class _CloudFingerprintWorker:
    """Persistent subprocess that can be replaced after one stalled cloud read."""

    def __init__(self) -> None:
        self._process: subprocess.Popen[str] | None = None

    def fingerprint(self, path: Path) -> tuple[str | None, int | None, int | None, Exception | None]:
        """Read one cloud path, deferring it if its helper does not respond in time."""
        process = self._start()
        if process.stdin is None or process.stdout is None:
            return None, None, None, OSError("Cloud fingerprint worker has no standard streams.")
        try:
            process.stdin.write(f"{path}\n")
            process.stdin.flush()
        except OSError as error:
            self._stop()
            return None, None, None, error
        ready, _, _ = select.select([process.stdout], [], [], WSL_CLOUD_READ_TIMEOUT_SECONDS)
        if not ready:
            self._stop()
            return None, None, None, TimeoutError(
                f"Timed out after {WSL_CLOUD_READ_TIMEOUT_SECONDS} seconds; retry on the next run."
            )
        try:
            payload = json.loads(process.stdout.readline())
            if "error" in payload:
                return None, None, None, OSError(str(payload["error"]))
            return str(payload["sha256"]), int(payload["size"]), int(payload["signature"]), None
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
            self._stop()
            return None, None, None, OSError(f"Invalid fingerprint-worker response: {error}")

    def close(self) -> None:
        """Stop the helper without waiting on a kernel-blocked cloud read."""
        self._stop()

    def _start(self) -> subprocess.Popen[str]:
        if self._process is None or self._process.poll() is not None:
            self._process = subprocess.Popen(
                [sys.executable, "-m", "dpf_files.fingerprint_worker", "--serve"],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1,
            )
        return self._process

    def _stop(self) -> None:
        if self._process is not None and self._process.poll() is None:
            self._process.terminate()
        self._process = None


def _is_wsl_cloud_path(path: Path) -> bool:
    """Return whether a path is in the Windows C drive while running under WSL."""
    return "microsoft" in platform.release().casefold() and path.parts[:3] == ("/", "mnt", "c")


def _archive_videos(
    sources: Iterable[Path],
    video_output: Path,
    dry_run: bool,
    result: PreparationResult | VideoArchiveResult,
) -> None:
    """Move discovered videos to the archive without replacing existing files."""
    for source_path in _discover_videos(sources, video_output):
        destination = _available_video_destination(video_output, source_path.name)
        if dry_run:
            result.videos.append(VideoRecord(source_path, destination, "planned"))
            continue
        try:
            video_output.mkdir(parents=True, exist_ok=True)
            shutil.move(str(source_path), destination)
        except OSError as error:
            _record_error(result, source_path, "archive_video", error)
            continue
        result.videos.append(VideoRecord(source_path, destination, "moved"))
        LOGGER.info("Archived video: %s", source_path)


def _available_video_destination(directory: Path, filename: str) -> Path:
    """Return a collision-free video archive path without overwriting a file."""
    candidate = directory / filename
    if not candidate.exists():
        return candidate
    stem, suffix = candidate.stem, candidate.suffix
    index = 2
    while True:
        candidate = directory / f"{stem} ({index}){suffix}"
        if not candidate.exists():
            return candidate
        index += 1


def _prepare_output_directories(
    images_dir: Path, legacy_images_dir: Path, reports_dir: Path, config: PreparationConfig
) -> None:
    """Safely initialize output locations after validation and source scanning."""
    if config.dry_run:
        reports_dir.mkdir(parents=True, exist_ok=True)
        return
    if config.overwrite_output:
        # Check report cleanup first so a Windows file lock cannot remove photos
        # before the rebuild is able to proceed.
        _clear_output_directory(reports_dir)
        _clear_output_directory(images_dir)
        _clear_output_directory(legacy_images_dir)
    images_dir.mkdir(parents=True, exist_ok=True)
    reports_dir.mkdir(parents=True, exist_ok=True)


def _clear_output_directory(directory: Path) -> None:
    """Remove a verified utility-owned output child directory, if it exists."""
    if directory.exists():
        if not directory.is_dir():
            raise SafetyError(f"Expected output directory but found a file: {directory}")
        shutil.rmtree(directory)


def _playback_groups(
    retained: list[tuple[Path, str, int]],
    sources: tuple[Path, ...],
    date_selector: Callable[[Path], tuple[datetime, str]] | None = None,
    date_error_handler: Callable[[Path, OSError], None] | None = None,
) -> list[tuple[str, list[tuple[Path, str, int, datetime | None, str]]]]:
    """Classify unique sources into family-album and chronological collections."""
    selector = _canonical_date if date_selector is None else date_selector
    family: list[tuple[Path, str, int, datetime | None, str]] = []
    by_year: dict[int, list[tuple[Path, str, int, datetime | None, str]]] = {}
    for path, digest, size in retained:
        try:
            date_value, date_source = selector(path)
        except OSError as error:
            if date_error_handler is None:
                raise
            date_error_handler(path, error)
            continue
        if _is_family_album(path, sources):
            family.append((path, digest, size, date_value, date_source))
            continue
        by_year.setdefault(date_value.year, []).append((path, digest, size, date_value, date_source))
    groups: list[tuple[str, list[tuple[Path, str, int, datetime | None, str]]]] = []
    if family:
        # Player filenames follow capture time; paths make equal timestamps deterministic.
        family.sort(key=_capture_date_order_key)
        groups.append(("family_album", family))
    small: list[tuple[int, list[tuple[Path, str, int, datetime | None, str]]]] = []
    for year in sorted(by_year):
        entries = by_year[year]
        # Sort before splitting or combining so every output folder stays chronological.
        entries.sort(key=_capture_date_order_key)
        if 500 <= len(entries) <= 600:
            groups.append((str(year), entries))
        elif len(entries) > 600:
            groups.extend(_split_year(year, entries))
        else:
            small.append((year, entries))
    groups.extend(_combine_small_years(small))
    return groups


def _capture_date_order_key(
    entry: tuple[Path, str, int, datetime | None, str],
) -> tuple[datetime, str]:
    """Return a stable ascending capture-date key for a playback entry."""
    path, _, _, date_value, _ = entry
    return (date_value or datetime.min, str(path).casefold())


def _is_family_album(path: Path, sources: Iterable[Path]) -> bool:
    """Return whether path belongs to either special family-album source tree."""
    for source in sources:
        try:
            return path.relative_to(source).parts[0].casefold() in FAMILY_ALBUM_DIRECTORIES
        except (ValueError, IndexError):
            continue
    return False


def _canonical_date(path: Path) -> tuple[datetime, str]:
    """Select a credible capture date without trusting recent copy timestamps first."""
    try:
        with Image.open(path) as image:
            exif = image.getexif()
            for tag, label in (
                (36867, "exif_datetime_original"),
                (36868, "exif_datetime_digitized"),
                (306, "exif_datetime"),
            ):
                parsed = _parse_embedded_datetime(exif.get(tag))
                if parsed is not None:
                    return parsed, label
    except Exception:
        pass
    filename_date = _filename_capture_date(path.stem)
    if filename_date is not None:
        return filename_date
    folder_date = _folder_capture_date(path)
    if folder_date is not None:
        return folder_date
    stat = path.stat()
    created = getattr(stat, "st_birthtime", None)
    if created is not None:
        return datetime.fromtimestamp(created), "filesystem_creation"
    return datetime.fromtimestamp(stat.st_mtime), "filesystem_modified"


def _reshuffle_date(row: dict[str, str], source_path: Path) -> tuple[datetime, str]:
    """Re-evaluate dates from manifest and paths without reopening cloud source files."""
    previous_source = row["date_source"]
    previous_date = _parse_manifest_datetime(row["canonical_date"])
    if previous_source.startswith("exif_") and previous_date is not None:
        return previous_date, previous_source
    filename_date = _filename_capture_date(source_path.stem)
    if filename_date is not None:
        return filename_date
    folder_date = _folder_capture_date(source_path)
    if folder_date is not None:
        return folder_date
    if previous_date is None:
        raise SafetyError(f"Manifest has an invalid canonical date for {source_path}")
    return previous_date, previous_source


def _parse_manifest_datetime(value: str) -> datetime | None:
    """Parse the ISO capture date persisted by a prior successful build."""
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _parse_embedded_datetime(value: object) -> datetime | None:
    """Parse a valid EXIF date-time value while rejecting malformed metadata."""
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.strptime(value[:19], "%Y:%m:%d %H:%M:%S")
    except ValueError:
        return None
    return parsed if parsed.year >= MIN_CAPTURE_YEAR else None


def _filename_capture_date(filename: str) -> tuple[datetime, str] | None:
    """Extract a strict machine- or human-readable date from one filename stem."""
    machine_date = _date_from_match(MACHINE_DATE_PATTERN.search(filename), machine=True)
    if machine_date is not None:
        return machine_date, "filename_machine"
    for pattern in (HUMAN_MONTH_FIRST_PATTERN, HUMAN_DAY_FIRST_PATTERN):
        human_date = _date_from_match(pattern.search(filename), machine=False)
        if human_date is not None:
            return human_date, "filename_human"
    return None


def _folder_capture_date(path: Path) -> tuple[datetime, str] | None:
    """Infer a date only from unambiguous date-like ancestor directory names."""
    for directory in path.parents:
        name = directory.name
        machine_date = _date_from_match(MACHINE_DATE_PATTERN.fullmatch(name), machine=True)
        if machine_date is not None:
            return machine_date, "folder_machine"
        for pattern in (HUMAN_MONTH_FIRST_PATTERN, HUMAN_DAY_FIRST_PATTERN):
            human_date = _date_from_match(pattern.fullmatch(name), machine=False)
            if human_date is not None:
                return human_date, "folder_human"
        if FOLDER_YEAR_PATTERN.fullmatch(name):
            return datetime(int(name), 1, 1), "folder_year"
    return None


def _date_from_match(match: re.Match[str] | None, machine: bool) -> datetime | None:
    """Build a calendar-validated datetime from a supported filename-date match."""
    if match is None:
        return None
    groups = match.groupdict()
    try:
        year = int(groups["year"])
        month = int(groups["month"]) if machine else MONTH_NAMES[groups["month"].casefold()]
        day = int(groups["day"])
        hour = int(groups.get("hour") or 0)
        minute = int(groups.get("minute") or 0)
        second = int(groups.get("second") or 0)
        parsed = datetime(year, month, day, hour, minute, second)
    except (KeyError, TypeError, ValueError):
        return None
    return parsed if parsed.year >= MIN_CAPTURE_YEAR else None


def _split_year(year: int, entries: list[tuple[Path, str, int, datetime | None, str]]) -> list[tuple[str, list[tuple[Path, str, int, datetime | None, str]]]]:
    """Split an oversized year into balanced, chronological lettered chunks."""
    chunk_count = (len(entries) + 599) // 600
    base, remainder = divmod(len(entries), chunk_count)
    result = []
    offset = 0
    for index in range(chunk_count):
        size = base + (1 if index < remainder else 0)
        result.append((f"{year}-{chr(ord('A') + index)}", entries[offset:offset + size]))
        offset += size
    return result


def _combine_small_years(small: list[tuple[int, list[tuple[Path, str, int, datetime | None, str]]]]) -> list[tuple[str, list[tuple[Path, str, int, datetime | None, str]]]]:
    """Combine adjacent undersized years while preserving chronological continuity."""
    groups = []
    current: list[tuple[Path, str, int, datetime | None, str]] = []
    start = end = None
    for year, entries in small:
        if current and year != end + 1:
            groups.append((_range_name(start, end), current))
            current = []
        current.extend(entries); start = year if start is None else start; end = year
        if len(current) >= 500:
            groups.append((_range_name(start, end), current)); current = []; start = end = None
    if current:
        groups.append((_range_name(start, end), current))
    return groups


def _range_name(start: int | None, end: int | None) -> str:
    return str(start) if start == end else f"{start}-{end}"


def _process_retained_file(
    source_path: Path,
    digest: str,
    source_size: int,
    sequence: int,
    images_dir: Path,
    folder: str,
    date_value: datetime | None,
    date_source: str,
    config: PreparationConfig,
    result: PreparationResult,
) -> None:
    """Copy or convert one unique source, recording an error without stopping the run."""
    extension = source_path.suffix.lower()
    converted = extension in HEIF_EXTENSIONS
    output_extension = ".jpg" if converted else extension
    output_filename = f"{sequence:04d}{output_extension}"
    output_path = images_dir / output_filename
    temporary_path = images_dir / f".{output_filename}.tmp"
    action = "converted" if converted else "copied"
    try:
        if config.dry_run:
            _validate_readable_image(source_path, converted)
            result.manifest.append(
                ManifestRecord(
                    output_filename, output_path, source_path, source_path.name, extension, digest,
                    source_size, None, f"would_{action}", "planned", folder,
                    "" if date_value is None else date_value.isoformat(), date_source
                )
            )
            return
        images_dir.mkdir(parents=True, exist_ok=True)
        if converted:
            _convert_heif(source_path, temporary_path, config.jpeg_quality)
        else:
            _validate_readable_image(source_path, False)
            shutil.copy2(source_path, temporary_path)
        temporary_path.replace(output_path)
        output_size = output_path.stat().st_size
    except Exception as error:  # Individual image decoders may raise library-specific errors.
        if temporary_path.exists():
            temporary_path.unlink()
        _record_error(result, source_path, action, error)
        return

    result.images_written += 1
    result.output_bytes += output_size
    if converted:
        result.conversions_completed += 1
    else:
        result.unchanged_files_copied += 1
    result.manifest.append(
        ManifestRecord(
            output_filename, output_path, source_path, source_path.name, extension, digest, source_size,
            output_size, action, "written", folder,
            "" if date_value is None else date_value.isoformat(), date_source
        )
    )
    LOGGER.info("Wrote %s (%d unique images completed).", output_filename, result.images_written)


def _validate_readable_image(path: Path, is_heif: bool) -> None:
    """Decode an image enough to identify corrupt ordinary inputs before copying."""
    if is_heif:
        register_heif_opener()
    with Image.open(path) as image:
        image.verify()


def _convert_heif(source_path: Path, output_path: Path, quality: int) -> None:
    """Convert HEIC/HEIF to orientation-correct RGB JPEG."""
    register_heif_opener()
    with Image.open(source_path) as image:
        _restore_heif_orientation_metadata(image)
        oriented = ImageOps.exif_transpose(image)
        if oriented.mode != "RGB":
            oriented = oriented.convert("RGB")
        oriented.save(output_path, format="JPEG", quality=quality, optimize=True)


def _restore_heif_orientation_metadata(image: Image.Image) -> None:
    """Expose HEIF's original orientation to Pillow before transposing it.

    ``pillow-heif`` preserves the container orientation in
    ``original_orientation`` while presenting a normalized EXIF orientation.  A
    conversion needs the original value so the JPEG pixel data is oriented for
    viewers that do not interpret HEIF metadata.
    """
    original_orientation = image.info.get("original_orientation")
    current_orientation = image.getexif().get(274, 1)
    if isinstance(original_orientation, int) and current_orientation == 1:
        image.getexif()[274] = original_orientation


def _record_error(
    result: PreparationResult, source_path: Path, operation: str, error: Exception
) -> None:
    """Store and log a recoverable per-file error."""
    record = ErrorRecord(source_path, operation, type(error).__name__, str(error))
    result.errors.append(record)
    LOGGER.error("%s failed for %s: %s", operation, source_path, error)


def _read_reshuffle_manifest(manifest_path: Path, images_dir: Path) -> list[dict[str, str]]:
    """Read and validate the existing manifest before any output is changed."""
    required_fields = {
        "output_path", "source_path", "source_filename", "source_extension", "sha256",
        "source_bytes", "action", "status", "canonical_date", "date_source",
    }
    with manifest_path.open(newline="", encoding="utf-8") as manifest_file:
        reader = csv.DictReader(manifest_file)
        if reader.fieldnames is None or not required_fields.issubset(reader.fieldnames):
            raise SafetyError("Manifest is missing fields required for date reshuffle.")
        rows = list(reader)
    if not rows:
        raise SafetyError("Manifest contains no prepared photos to reshuffle.")

    validated_rows: list[dict[str, str]] = []
    seen_outputs: set[Path] = set()
    seen_sources: set[Path] = set()
    for row in rows:
        output_path = _manifest_path(row["output_path"])
        source_path = _manifest_path(row["source_path"])
        try:
            output_path.relative_to(images_dir)
        except ValueError as error:
            raise SafetyError(f"Manifest output is outside photos: {output_path}") from error
        if output_path in seen_outputs:
            raise SafetyError(f"Manifest lists output more than once: {output_path}")
        if source_path in seen_sources:
            raise SafetyError(f"Manifest lists source more than once: {source_path}")
        seen_outputs.add(output_path)
        seen_sources.add(source_path)
        validated_rows.append({**row, "output_path": str(output_path), "source_path": str(source_path)})
    return validated_rows


def _manifest_path(value: str) -> Path:
    """Convert a Windows-formatted report path to the current platform path."""
    windows_path = PureWindowsPath(value)
    if "microsoft" in platform.release().casefold() and windows_path.drive and windows_path.root:
        return Path("/mnt", windows_path.drive.removesuffix(":").lower(), *windows_path.parts[1:])
    return Path(value).expanduser()


def _configure_file_logging(reports_dir: Path) -> logging.FileHandler:
    """Attach a per-run diagnostic log in the reports directory."""
    handler = logging.FileHandler(reports_dir / "processing.log", encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    LOGGER.addHandler(handler)
    LOGGER.setLevel(logging.INFO)
    return handler


def _write_reports(reports_dir: Path, result: PreparationResult) -> None:
    """Write all CSV and text reports for a completed or dry run."""
    _write_manifest(reports_dir / "manifest.csv", result.manifest)
    _write_csv(
        reports_dir / "videos.csv",
        ["source_path", "archived_path", "status"],
        ((str(item.source_path), str(item.archived_path), item.status) for item in result.videos),
    )
    _write_csv(
        reports_dir / "duplicates.csv",
        ["duplicate_source_path", "retained_source_path", "sha256", "size", "duplicate_type"],
        (
            (str(item.duplicate_path), str(item.retained_path), item.sha256, item.size, item.duplicate_type)
            for item in result.duplicates
        ),
    )
    _write_csv(
        reports_dir / "errors.csv",
        ["source_path", "operation", "exception_type", "message"],
        ((str(item.source_path), item.operation, item.exception_type, item.message) for item in result.errors),
    )
    (reports_dir / "summary.txt").write_text(result.summary_text(), encoding="utf-8")


def _write_manifest(path: Path, records: Iterable[ManifestRecord]) -> None:
    """Write traceability records in the shared manifest CSV schema."""
    _write_csv(
        path,
        [
            "output_filename", "output_path", "source_path", "source_folder", "source_filename", "source_extension", "sha256",
            "source_bytes", "output_bytes", "action", "status", "playback_folder", "canonical_date", "capture_date", "date_source",
        ],
        (
            (
                item.output_filename, format_report_path(item.output_path), format_report_path(item.source_path),
                format_report_path(item.source_path.parent), item.source_filename,
                item.source_extension, item.sha256, item.source_bytes,
                "" if item.output_bytes is None else item.output_bytes, item.action, item.status,
                item.playback_folder, item.canonical_date, item.capture_date, item.date_source,
            )
            for item in records
        ),
    )


def _write_csv(path: Path, headers: list[str], rows: Iterable[tuple[object, ...]]) -> None:
    """Write a UTF-8 CSV file with a deterministic header row."""
    with path.open("w", newline="", encoding="utf-8") as report_file:
        writer = csv.writer(report_file)
        writer.writerow(headers)
        writer.writerows(rows)


def format_report_path(path: Path) -> str:
    """Return a report path usable in the operating system running the report viewer."""
    expanded_path = path.expanduser()
    parts = expanded_path.parts
    if len(parts) >= 4 and parts[:2] == ("/", "mnt") and len(parts[2]) == 1:
        separator = chr(92)
        windows_suffix = separator.join(parts[3:])
        return f"{parts[2].upper()}:{separator}{windows_suffix}"
    return str(expanded_path.resolve(strict=False))


def _report_path(path: Path) -> str:
    """Backward-compatible private alias for :func:`format_report_path`."""
    return format_report_path(path)
