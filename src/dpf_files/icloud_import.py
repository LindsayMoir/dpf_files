"""Safely import original photos referenced by a preparation manifest into iCloud."""

from __future__ import annotations

import csv
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field, replace
from datetime import datetime
from html import escape
import hashlib
import os
from pathlib import Path
import shutil
from typing import Final, Iterable
from uuid import uuid4

from PIL import Image, ImageOps
from pillow_heif import register_heif_opener

from dpf_files.config import normalize_path
from dpf_files.pipeline import format_report_path
from dpf_files.visual import is_distinctive_signature
from dpf_files.visual import nearest_visual_match as _nearest_visual_match
from dpf_files.visual import visual_signature as _visual_signature

MANIFEST_REQUIRED_FIELDS: Final[frozenset[str]] = frozenset(
    {"source_path", "source_filename", "sha256", "status"}
)
COPY_BUFFER_SIZE: Final[int] = 1024 * 1024
VISUAL_THUMBNAIL_SIZE: Final[tuple[int, int]] = (240, 240)
VISUAL_HASH_WORKERS: Final[int] = 4
VISUAL_DUPLICATE_REPORT_NAME: Final[str] = "possible_visual_duplicates.csv"
VISUAL_CLEANUP_REPORT_NAME: Final[str] = "visual_duplicate_cleanup.csv"


class ICloudImportError(ValueError):
    """Raised when an iCloud import cannot be planned safely."""


@dataclass(frozen=True)
class ImportRecord:
    """One source-photo decision made by an iCloud import run."""

    source_path: Path
    destination_path: Path | None
    sha256: str
    status: str
    detail: str = ""


@dataclass(frozen=True)
class VisualDuplicateRecord:
    """One non-destructive visual-duplicate audit decision."""

    source_path: Path
    destination_path: Path | None
    source_signature: str
    destination_signature: str
    hamming_distance: int | None
    status: str
    detail: str = ""
    source_thumbnail: Path | None = None
    destination_thumbnail: Path | None = None


@dataclass(frozen=True)
class VisualCleanupRecord:
    """One deletion decision for a connected visual-duplicate group."""

    deleted_path: Path | None
    retained_path: Path | None
    status: str
    modified_at: str
    detail: str = ""


@dataclass
class ICloudImportResult:
    """Outcome and audit records for one iCloud import run."""

    manifest_path: Path
    destination: Path
    report_path: Path
    dry_run: bool
    records: list[ImportRecord] = field(default_factory=list)
    visual_duplicates: list[VisualDuplicateRecord] = field(default_factory=list)
    visual_report_path: Path | None = None
    visual_html_report_path: Path | None = None

    def summary_text(self) -> str:
        """Return a concise, human-readable import summary."""
        counts: dict[str, int] = {}
        for record in self.records:
            counts[record.status] = counts.get(record.status, 0) + 1
        lines = [
            f"Manifest: {format_report_path(self.manifest_path)}",
            f"iCloud destination: {format_report_path(self.destination)}",
            f"Mode: {'dry run' if self.dry_run else 'execute'}",
            f"Records: {len(self.records)}",
        ]
        lines.extend(f"{status}: {count}" for status, count in sorted(counts.items()))
        lines.append(f"Report: {format_report_path(self.report_path)}")
        if self.visual_report_path is not None:
            matches = sum(record.status == "possible_visual_duplicate" for record in self.visual_duplicates)
            lines.append(f"Possible visual duplicates: {matches}")
            lines.append(f"Visual-duplicate report: {format_report_path(self.visual_report_path)}")
            if self.visual_html_report_path is not None:
                lines.append(f"Visual-duplicate thumbnails: {format_report_path(self.visual_html_report_path)}")
        return "\n".join(lines) + "\n"


def import_manifest_to_icloud(
    manifest_path: Path,
    destination: Path,
    *,
    dry_run: bool = True,
    report_path: Path | None = None,
    visual_dedup: bool = False,
    visual_audit_only: bool = False,
) -> ICloudImportResult:
    """Import missing original photos named in a preparation manifest.

    Sources are never modified. Destination files are compared by SHA-256, and
    an existing destination filename is never overwritten. A CSV audit report
    is written for both dry runs and executed imports. When ``visual_dedup``
    is enabled, an additional dry-run-only report identifies conservative
    perceptual-hash matches but never changes the copy decision.
    """
    manifest = normalize_path(manifest_path).resolve(strict=False)
    target_directory = normalize_path(destination).resolve(strict=False)
    report = normalize_path(report_path or manifest.parent / "icloud_import.csv").resolve(strict=False)
    _validate_inputs(manifest, target_directory, report)
    if visual_audit_only and not visual_dedup:
        raise ICloudImportError("Visual-audit-only mode requires visual duplicate detection.")

    visual_report = report.parent / "possible_visual_duplicates.csv" if visual_dedup else None
    visual_html_report = report.parent / "possible_visual_duplicates.html" if visual_dedup else None
    result = ICloudImportResult(
        manifest_path=manifest,
        destination=target_directory,
        report_path=report,
        dry_run=dry_run,
        visual_report_path=visual_report,
        visual_html_report_path=visual_html_report,
    )
    if visual_audit_only:
        visual_index, visual_inventory_records = _visual_destination_inventory(target_directory)
        del visual_index
        result.visual_duplicates.extend(visual_inventory_records)
        _write_visual_reports(result)
        return result

    existing_hashes, existing_names, inventory_errors = _destination_inventory(target_directory)
    result.records.extend(inventory_errors)
    if visual_dedup:
        visual_index, visual_inventory_records = _visual_destination_inventory(target_directory)
    else:
        visual_index, visual_inventory_records = {}, []
    result.visual_duplicates.extend(visual_inventory_records)
    reserved_names = set(existing_names)
    seen_sources: set[Path] = set()

    for row in _read_manifest(manifest):
        source = normalize_path(row["source_path"]).resolve(strict=False)
        manifest_hash = row["sha256"].casefold()
        if source in seen_sources:
            result.records.append(ImportRecord(source, None, manifest_hash, "duplicate_manifest_entry"))
            continue
        seen_sources.add(source)
        if row["status"].casefold() not in {"written", "planned"}:
            result.records.append(ImportRecord(source, None, manifest_hash, "manifest_not_importable"))
            continue
        if _is_within(source, target_directory):
            result.records.append(ImportRecord(source, source, manifest_hash, "already_in_destination"))
            continue
        if not source.is_file():
            result.records.append(ImportRecord(source, None, manifest_hash, "missing_source"))
            continue
        if manifest_hash in existing_hashes:
            result.records.append(ImportRecord(source, existing_hashes[manifest_hash], manifest_hash, "duplicate_content"))
            continue

        visual_record: VisualDuplicateRecord | None = None
        if visual_dedup:
            visual_record = _visual_duplicate_record(source, visual_index)
            result.visual_duplicates.append(visual_record)
            if not dry_run and visual_record.status == "possible_visual_duplicate":
                result.records.append(
                    ImportRecord(
                        source,
                        visual_record.destination_path,
                        manifest_hash,
                        "duplicate_visual_content",
                        f"dHash distance: {visual_record.hamming_distance}",
                    )
                )
                continue

        target = _available_destination(target_directory, source.name, manifest_hash, reserved_names)
        reserved_names.add(target.name.casefold())
        renamed = target.name != source.name
        if dry_run:
            status = "would_copy_renamed" if renamed else "would_copy"
            result.records.append(ImportRecord(source, target, manifest_hash, status))
            continue
        try:
            current_hash = _sha256(source)
            if current_hash != manifest_hash:
                result.records.append(ImportRecord(source, target, manifest_hash, "source_changed"))
                continue
            _copy_without_overwrite(source, target, manifest_hash)
        except OSError as error:
            result.records.append(ImportRecord(source, target, manifest_hash, "copy_failed", str(error)))
            continue
        existing_hashes[manifest_hash] = target
        if visual_record is not None and visual_record.source_signature:
            visual_index.setdefault(int(visual_record.source_signature, 16), target)
        status = "copied_renamed" if renamed else "copied"
        result.records.append(ImportRecord(source, target, manifest_hash, status))

    _write_report(report, result.records)
    _write_visual_reports(result)
    return result


def delete_visual_duplicates(
    visual_report_path: Path,
    destination: Path,
    *,
    cleanup_report_path: Path | None = None,
) -> list[VisualCleanupRecord]:
    """Delete the newest file from each visual-duplicate group in an iCloud folder.

    The input must be this utility's visual audit CSV. Every referenced path is
    validated to be an existing file within ``destination`` before any deletion
    decision is made. A separate audit report records every retained and deleted
    path so the iCloud cleanup remains traceable.
    """
    visual_report = normalize_path(visual_report_path).resolve(strict=False)
    target_directory = normalize_path(destination).resolve(strict=False)
    cleanup_report = normalize_path(
        cleanup_report_path or visual_report.parent / VISUAL_CLEANUP_REPORT_NAME
    ).resolve(strict=False)
    if not visual_report.is_file():
        raise ICloudImportError(f"Visual duplicate report does not exist: {format_report_path(visual_report)}")
    if not target_directory.is_dir():
        raise ICloudImportError(f"iCloud destination is not a directory: {format_report_path(target_directory)}")
    if not cleanup_report.parent.is_dir():
        raise ICloudImportError(f"Cleanup report directory does not exist: {format_report_path(cleanup_report.parent)}")
    if cleanup_report == visual_report:
        raise ICloudImportError("Cleanup report path must not replace the visual duplicate report.")

    groups, records = _visual_duplicate_groups(visual_report, target_directory)
    for group in groups:
        available: list[Path] = []
        for candidate in group:
            try:
                _image_version_key(candidate)
            except OSError as error:
                records.append(VisualCleanupRecord(candidate, None, "missing_candidate", "", str(error)))
            else:
                available.append(candidate)
        if len(available) < 2:
            continue
        retained = min(available, key=_image_version_key)
        for candidate in sorted(set(available) - {retained}, key=_image_version_key, reverse=True):
            modified_at = _modified_timestamp(candidate)
            try:
                candidate.unlink()
            except OSError as error:
                records.append(VisualCleanupRecord(candidate, retained, "delete_failed", modified_at, str(error)))
                continue
            records.append(VisualCleanupRecord(candidate, retained, "deleted_newest_visual_duplicate", modified_at))
    _write_visual_cleanup_report(cleanup_report, records)
    return records


def _write_visual_reports(result: ICloudImportResult) -> None:
    """Write visual audit assets when visual duplicate detection was requested."""
    if result.visual_report_path is None or result.visual_html_report_path is None:
        return
    thumbnail_directory = result.visual_report_path.parent / "possible_visual_duplicates"
    result.visual_duplicates = _write_visual_thumbnails(result.visual_duplicates, thumbnail_directory)
    _write_visual_report(result.visual_report_path, result.visual_duplicates)
    _write_visual_html_report(result.visual_html_report_path, result.visual_duplicates)


def _visual_duplicate_groups(
    visual_report: Path, destination: Path
) -> tuple[list[set[Path]], list[VisualCleanupRecord]]:
    """Read safe visual-match edges and return their connected file groups."""
    parents: dict[Path, Path] = {}
    records: list[VisualCleanupRecord] = []
    with visual_report.open(newline="", encoding="utf-8-sig") as report_file:
        reader = csv.DictReader(report_file)
        required_fields = {"source_path", "destination_path", "status"}
        missing = required_fields - set(reader.fieldnames or ())
        if missing:
            raise ICloudImportError(f"Visual duplicate report is missing column(s): {', '.join(sorted(missing))}")
        for line_number, row in enumerate(reader, start=2):
            if row.get("status") != "possible_visual_duplicate":
                continue
            source = _report_image_path(row.get("source_path"), destination, line_number, records)
            retained = _report_image_path(row.get("destination_path"), destination, line_number, records)
            if source is None or retained is None:
                continue
            _union_visual_paths(parents, source, retained)

    grouped_paths: dict[Path, set[Path]] = {}
    for path in parents:
        grouped_paths.setdefault(_find_visual_path(parents, path), set()).add(path)
    return list(grouped_paths.values()), records


def _report_image_path(
    value: str | None,
    destination: Path,
    line_number: int,
    records: list[VisualCleanupRecord],
) -> Path | None:
    """Validate one visual-report path before allowing it into a deletion group."""
    if not value:
        records.append(VisualCleanupRecord(None, None, "invalid_report_row", "", f"Line {line_number} has a blank path."))
        return None
    path = normalize_path(Path(value)).resolve(strict=False)
    if not _is_within(path, destination):
        records.append(VisualCleanupRecord(path, None, "unsafe_report_path", "", f"Line {line_number} is outside destination."))
        return None
    if not path.is_file():
        records.append(VisualCleanupRecord(path, None, "missing_candidate", "", f"Line {line_number} no longer exists."))
        return None
    return path


def _union_visual_paths(parents: dict[Path, Path], first: Path, second: Path) -> None:
    """Join two duplicate paths in a deterministic union-find structure."""
    first_root = _find_visual_path(parents, first)
    second_root = _find_visual_path(parents, second)
    if first_root != second_root:
        parents[second_root] = first_root


def _find_visual_path(parents: dict[Path, Path], path: Path) -> Path:
    """Return a path group's representative, adding unseen paths as roots."""
    parent = parents.setdefault(path, path)
    if parent == path:
        return path
    root = _find_visual_path(parents, parent)
    parents[path] = root
    return root


def _image_version_key(path: Path) -> tuple[int, str]:
    """Sort image versions from oldest to newest by durable modification time."""
    return path.stat().st_mtime_ns, str(path).casefold()


def _modified_timestamp(path: Path) -> str:
    """Return a report-friendly local modification timestamp for one image."""
    return datetime.fromtimestamp(path.stat().st_mtime).isoformat(timespec="seconds")


def _write_visual_cleanup_report(path: Path, records: Iterable[VisualCleanupRecord]) -> None:
    """Write an audit log for visual-duplicate cleanup decisions."""
    with path.open("w", newline="", encoding="utf-8") as report_file:
        writer = csv.writer(report_file)
        writer.writerow(["deleted_path", "retained_path", "status", "modified_at", "detail"])
        writer.writerows(
            (
                "" if record.deleted_path is None else format_report_path(record.deleted_path),
                "" if record.retained_path is None else format_report_path(record.retained_path),
                record.status,
                record.modified_at,
                record.detail,
            )
            for record in records
        )


def _validate_inputs(manifest: Path, destination: Path, report: Path) -> None:
    """Validate paths before inspecting or changing the destination."""
    if not manifest.is_file():
        raise ICloudImportError(f"Manifest does not exist: {format_report_path(manifest)}")
    if not destination.is_dir():
        raise ICloudImportError(f"iCloud destination is not a directory: {format_report_path(destination)}")
    if not report.parent.is_dir():
        raise ICloudImportError(f"Report directory does not exist: {format_report_path(report.parent)}")
    if report == manifest:
        raise ICloudImportError("Report path must not replace the manifest.")


def _read_manifest(path: Path) -> Iterable[dict[str, str]]:
    """Yield validated manifest rows, rejecting malformed input before copying."""
    with path.open(newline="", encoding="utf-8-sig") as manifest_file:
        reader = csv.DictReader(manifest_file)
        fields = set(reader.fieldnames or ())
        missing = MANIFEST_REQUIRED_FIELDS - fields
        if missing:
            raise ICloudImportError(f"Manifest is missing required column(s): {', '.join(sorted(missing))}")
        for line_number, row in enumerate(reader, start=2):
            if not all(isinstance(row.get(field), str) and row[field].strip() for field in MANIFEST_REQUIRED_FIELDS):
                raise ICloudImportError(f"Manifest row {line_number} has a blank required value.")
            digest = row["sha256"].casefold()
            if len(digest) != 64 or any(character not in "0123456789abcdef" for character in digest):
                raise ICloudImportError(f"Manifest row {line_number} has an invalid SHA-256 value.")
            yield row


def _destination_inventory(directory: Path) -> tuple[dict[str, Path], set[str], list[ImportRecord]]:
    """Hash destination files once to make duplicate checks linear in library size."""
    hashes: dict[str, Path] = {}
    names: set[str] = set()
    errors: list[ImportRecord] = []
    for path in sorted((item for item in directory.rglob("*") if item.is_file()), key=lambda item: str(item).casefold()):
        names.add(path.name.casefold())
        try:
            hashes.setdefault(_sha256(path), path)
        except OSError as error:
            errors.append(ImportRecord(path, None, "", "destination_unreadable", str(error)))
    return hashes, names, errors


def _visual_destination_inventory(directory: Path) -> tuple[dict[int, Path], list[VisualDuplicateRecord]]:
    """Build a signature index and audit visually matching destination images."""
    signatures: dict[int, Path] = {}
    records: list[VisualDuplicateRecord] = []
    paths = list(_image_files(directory))
    register_heif_opener()
    worker_count = min(VISUAL_HASH_WORKERS, os.cpu_count() or 1)
    with ThreadPoolExecutor(max_workers=worker_count) as executor:
        signature_results = executor.map(_visual_signature_result, paths)
        for path, signature, error in signature_results:
            if error is not None:
                records.append(
                    VisualDuplicateRecord(path, None, "", "", None, "destination_signature_failed", str(error))
                )
                continue
            if signature is None:
                records.append(
                    VisualDuplicateRecord(path, None, "", "", None, "destination_signature_failed", "No signature produced."))
                continue
            match = _nearest_visual_match(signature, signatures) if is_distinctive_signature(signature) else None
            if match is not None:
                destination_signature, destination_path, distance = match
                records.append(
                    VisualDuplicateRecord(
                        path,
                        destination_path,
                        f"{signature:016x}",
                        f"{destination_signature:016x}",
                        distance,
                        "possible_visual_duplicate",
                    )
                )
            if is_distinctive_signature(signature):
                signatures.setdefault(signature, path)
    return signatures, records


def _visual_signature_result(path: Path) -> tuple[Path, int | None, Exception | None]:
    """Calculate one signature while retaining decode errors for the audit report."""
    try:
        return path, _visual_signature(path), None
    except Exception as error:  # Image decoders can raise library-specific exceptions.
        return path, None, error


def _visual_duplicate_record(source: Path, destination_signatures: dict[int, Path]) -> VisualDuplicateRecord:
    """Return an audit record for a source image without affecting import behavior."""
    try:
        source_signature = _visual_signature(source)
    except Exception as error:  # Image decoders can raise library-specific exceptions.
        return VisualDuplicateRecord(source, None, "", "", None, "source_signature_failed", str(error))

    match = _nearest_visual_match(source_signature, destination_signatures) if is_distinctive_signature(source_signature) else None
    if match is None:
        return VisualDuplicateRecord(source, None, f"{source_signature:016x}", "", None, "no_visual_match")
    destination_signature, destination_path, distance = match
    return VisualDuplicateRecord(
        source,
        destination_path,
        f"{source_signature:016x}",
        f"{destination_signature:016x}",
        distance,
        "possible_visual_duplicate",
    )


def _image_files(directory: Path) -> Iterable[Path]:
    """Yield destination images in a deterministic order suitable for auditing."""
    extensions = frozenset({".jpg", ".jpeg", ".png", ".bmp", ".heic", ".heif"})
    return iter(
        sorted(
            (path for path in directory.rglob("*") if path.is_file() and path.suffix.casefold() in extensions),
            key=lambda path: str(path).casefold(),
        )
    )


def _available_destination(directory: Path, filename: str, digest: str, reserved_names: set[str]) -> Path:
    """Return a flattened, collision-safe destination name without overwriting."""
    candidate = directory / filename
    if candidate.name.casefold() not in reserved_names:
        return candidate
    suffix = candidate.suffix
    stem = candidate.stem
    index = 1
    while True:
        numbered_suffix = "" if index == 1 else f"-{index}"
        candidate = directory / f"{stem}--{digest[:8]}{numbered_suffix}{suffix}"
        if candidate.name.casefold() not in reserved_names:
            return candidate
        index += 1


def _copy_without_overwrite(source: Path, destination: Path, expected_hash: str) -> None:
    """Copy a source atomically into one new destination path without replacement."""
    temporary = destination.parent / f".{destination.name}.{uuid4().hex}.tmp"
    try:
        shutil.copy2(source, temporary)
        if _sha256(temporary) != expected_hash:
            raise OSError("Copied file hash does not match its source hash.")
        os.link(temporary, destination)
    finally:
        if temporary.exists():
            temporary.unlink()


def _sha256(path: Path) -> str:
    """Calculate a file's SHA-256 without loading it all into memory."""
    digest = hashlib.sha256()
    with path.open("rb") as source_file:
        for chunk in iter(lambda: source_file.read(COPY_BUFFER_SIZE), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _is_within(path: Path, directory: Path) -> bool:
    """Return whether a path is the destination directory or one of its children."""
    try:
        path.relative_to(directory)
    except ValueError:
        return False
    return True


def _write_report(path: Path, records: Iterable[ImportRecord]) -> None:
    """Write an auditable CSV report after all decisions have been made."""
    with path.open("w", newline="", encoding="utf-8") as report_file:
        writer = csv.writer(report_file)
        writer.writerow(["source_path", "destination_path", "sha256", "status", "detail"])
        writer.writerows(
            (
                format_report_path(record.source_path),
                "" if record.destination_path is None else format_report_path(record.destination_path),
                record.sha256,
                record.status,
                record.detail,
            )
            for record in records
        )


def _write_visual_report(path: Path, records: Iterable[VisualDuplicateRecord]) -> None:
    """Write the non-destructive perceptual-hash audit report."""
    with path.open("w", newline="", encoding="utf-8") as report_file:
        writer = csv.writer(report_file)
        writer.writerow(
            [
                "source_path",
                "destination_path",
                "source_visual_signature",
                "destination_visual_signature",
                "hamming_distance",
                "status",
                "detail",
                "source_thumbnail",
                "destination_thumbnail",
            ]
        )
        writer.writerows(
            (
                format_report_path(record.source_path),
                "" if record.destination_path is None else format_report_path(record.destination_path),
                record.source_signature,
                record.destination_signature,
                "" if record.hamming_distance is None else record.hamming_distance,
                record.status,
                record.detail,
                "" if record.source_thumbnail is None else record.source_thumbnail.as_posix(),
                "" if record.destination_thumbnail is None else record.destination_thumbnail.as_posix(),
            )
            for record in records
        )


def _write_visual_thumbnails(
    records: list[VisualDuplicateRecord], thumbnail_directory: Path
) -> list[VisualDuplicateRecord]:
    """Create paired thumbnails for visual matches and return updated audit records."""
    thumbnail_directory.mkdir(exist_ok=True)
    updated_records: list[VisualDuplicateRecord] = []
    for index, record in enumerate(records, start=1):
        if record.status != "possible_visual_duplicate" or record.destination_path is None:
            updated_records.append(record)
            continue
        source_thumbnail = thumbnail_directory / f"{index:05d}-source.jpg"
        destination_thumbnail = thumbnail_directory / f"{index:05d}-destination.jpg"
        try:
            _write_thumbnail(record.source_path, source_thumbnail)
            _write_thumbnail(record.destination_path, destination_thumbnail)
        except Exception as error:  # Image decoders can raise library-specific exceptions.
            updated_records.append(
                replace(
                    record,
                    status="possible_visual_duplicate_thumbnail_failed",
                    detail=str(error),
                )
            )
            continue
        updated_records.append(
            replace(
                record,
                source_thumbnail=source_thumbnail.relative_to(thumbnail_directory.parent),
                destination_thumbnail=destination_thumbnail.relative_to(thumbnail_directory.parent),
            )
        )
    return updated_records


def _write_thumbnail(source: Path, destination: Path) -> None:
    """Write one orientation-normalized JPEG thumbnail for visual review."""
    register_heif_opener()
    with Image.open(source) as image:
        oriented = ImageOps.exif_transpose(image).convert("RGB")
        thumbnail = ImageOps.contain(oriented, VISUAL_THUMBNAIL_SIZE, Image.Resampling.LANCZOS)
        thumbnail.save(destination, "JPEG", quality=85, optimize=True)


def _write_visual_html_report(path: Path, records: Iterable[VisualDuplicateRecord]) -> None:
    """Write a browsable paired-thumbnail report for proposed visual duplicates."""
    matches = [record for record in records if record.source_thumbnail is not None]
    rows = "\n".join(
        "<article>"
        f"<p>Distance: {record.hamming_distance}</p>"
        f"<figure><img src=\"{escape(record.source_thumbnail.as_posix())}\"><figcaption>{escape(format_report_path(record.source_path))}</figcaption></figure>"
        f"<figure><img src=\"{escape(record.destination_thumbnail.as_posix())}\"><figcaption>{escape(format_report_path(record.destination_path))}</figcaption></figure>"
        "</article>"
        for record in matches
        if record.destination_thumbnail is not None and record.destination_path is not None
    )
    document = (
        "<!doctype html><html><head><meta charset=\"utf-8\"><title>Possible visual duplicates</title>"
        "<style>body{font-family:system-ui,sans-serif;margin:2rem}article{display:flex;gap:1rem;align-items:start;"
        "border-top:1px solid #ccc;padding:1rem 0}figure{margin:0;max-width:280px}img{max-width:240px;max-height:240px}"
        "figcaption{font-size:.8rem;overflow-wrap:anywhere}p{min-width:7rem}</style></head><body>"
        f"<h1>Possible visual duplicates ({len(matches)})</h1>"
        "<p>Review before any cleanup. A lower dHash distance is more similar; this report makes no changes.</p>"
        f"{rows or '<p>No proposed visual duplicates.</p>'}</body></html>"
    )
    path.write_text(document, encoding="utf-8")
