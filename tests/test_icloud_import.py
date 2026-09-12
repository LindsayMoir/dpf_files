"""Tests for manifest-driven, non-destructive iCloud photo imports."""

from __future__ import annotations

import csv
import hashlib
import os
from pathlib import Path

import pytest
from PIL import Image

from dpf_files.icloud_import import (
    ICloudImportError,
    delete_visual_duplicates,
    import_manifest_to_icloud,
)


def _sha256(path: Path) -> str:
    """Return a fixture file's SHA-256 digest."""
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write_manifest(path: Path, sources: list[Path]) -> None:
    """Create the subset of a preparation manifest needed by the importer."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as manifest_file:
        writer = csv.DictWriter(
            manifest_file,
            fieldnames=["source_path", "source_filename", "sha256", "status"],
        )
        writer.writeheader()
        for source in sources:
            writer.writerow(
                {
                    "source_path": str(source),
                    "source_filename": source.name,
                    "sha256": _sha256(source),
                    "status": "written",
                }
            )


def test_dry_run_preserves_original_name_and_writes_audit_report(tmp_path: Path) -> None:
    """Dry runs plan a flattened import without changing iCloud files."""
    source = tmp_path / "USB" / "album" / "IMG_0001.JPG"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"original photo")
    manifest = tmp_path / "output" / "reports" / "manifest.csv"
    _write_manifest(manifest, [source])
    destination = tmp_path / "iCloud Photos" / "Photos"
    destination.mkdir(parents=True)

    result = import_manifest_to_icloud(manifest, destination)

    assert not (destination / source.name).exists()
    assert [(record.status, record.destination_path) for record in result.records] == [
        ("would_copy", destination / source.name)
    ]
    with result.report_path.open(newline="", encoding="utf-8") as report_file:
        rows = list(csv.DictReader(report_file))
    assert rows[0]["status"] == "would_copy"
    assert rows[0]["destination_path"] == str(destination / source.name)


def test_execute_skips_existing_content_and_is_safe_to_rerun(tmp_path: Path) -> None:
    """Content hashes prevent duplicates before and after an import."""
    source = tmp_path / "USB" / "IMG_0002.JPG"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"unique source")
    manifest = tmp_path / "output" / "reports" / "manifest.csv"
    _write_manifest(manifest, [source])
    destination = tmp_path / "iCloud" / "Photos"
    destination.mkdir(parents=True)
    (destination / "already there.jpg").write_bytes(source.read_bytes())

    duplicate_result = import_manifest_to_icloud(manifest, destination, dry_run=False)
    assert duplicate_result.records[0].status == "duplicate_content"
    assert not (destination / source.name).exists()

    (destination / "already there.jpg").unlink()
    copied_result = import_manifest_to_icloud(manifest, destination, dry_run=False)
    assert copied_result.records[0].status == "copied"
    assert (destination / source.name).read_bytes() == source.read_bytes()

    rerun_result = import_manifest_to_icloud(manifest, destination, dry_run=False)
    assert rerun_result.records[0].status == "duplicate_content"


def test_distinct_name_collision_receives_stable_hash_suffix(tmp_path: Path) -> None:
    """Different photos with one filename are retained without overwriting."""
    source = tmp_path / "USB" / "IMG_0003.JPG"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"new photo")
    manifest = tmp_path / "output" / "reports" / "manifest.csv"
    _write_manifest(manifest, [source])
    destination = tmp_path / "iCloud" / "Photos"
    destination.mkdir(parents=True)
    existing = destination / source.name
    existing.write_bytes(b"old photo")

    result = import_manifest_to_icloud(manifest, destination, dry_run=False)

    expected = destination / f"IMG_0003--{_sha256(source)[:8]}.JPG"
    assert result.records[0].destination_path == expected
    assert result.records[0].status == "copied_renamed"
    assert existing.read_bytes() == b"old photo"
    assert expected.read_bytes() == b"new photo"


def test_sources_already_in_icloud_are_not_reimported(tmp_path: Path) -> None:
    """An iCloud source is never copied onto itself or renamed."""
    destination = tmp_path / "iCloud" / "Photos"
    destination.mkdir(parents=True)
    source = destination / "IMG_0004.JPG"
    source.write_bytes(b"already in iCloud")
    manifest = tmp_path / "output" / "reports" / "manifest.csv"
    _write_manifest(manifest, [source])

    result = import_manifest_to_icloud(manifest, destination, dry_run=False)

    assert result.records[0].status == "already_in_destination"
    assert source.read_bytes() == b"already in iCloud"


def test_changed_or_malformed_manifest_sources_fail_safely(tmp_path: Path) -> None:
    """A stale or malformed manifest cannot silently import the wrong bytes."""
    source = tmp_path / "USB" / "IMG_0005.JPG"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"initial bytes")
    manifest = tmp_path / "output" / "reports" / "manifest.csv"
    _write_manifest(manifest, [source])
    source.write_bytes(b"changed bytes")
    destination = tmp_path / "iCloud" / "Photos"
    destination.mkdir(parents=True)

    result = import_manifest_to_icloud(manifest, destination, dry_run=False)
    assert result.records[0].status == "source_changed"
    assert not list(destination.iterdir())

    manifest.write_text("source_path,sha256,status\n", encoding="utf-8")
    with pytest.raises(ICloudImportError, match="source_filename"):
        import_manifest_to_icloud(manifest, destination)


def test_visual_dedup_audits_reencoded_images_without_suppressing_copy(tmp_path: Path) -> None:
    """A visual match is reported but remains strictly non-destructive."""
    source = tmp_path / "USB" / "IMG_0006.JPG"
    source.parent.mkdir(parents=True)
    image = Image.new("RGB", (64, 48))
    image.putdata([(x * 4, y * 5, (x + y) * 2) for y in range(48) for x in range(64)])
    image.save(source, "JPEG", quality=70)
    manifest = tmp_path / "output" / "reports" / "manifest.csv"
    _write_manifest(manifest, [source])
    destination = tmp_path / "iCloud" / "Photos"
    destination.mkdir(parents=True)
    image.save(destination / "already-there.png", "PNG")

    result = import_manifest_to_icloud(manifest, destination, visual_dedup=True)

    assert result.records[0].status == "would_copy"
    assert not (destination / source.name).exists()
    assert result.visual_report_path == manifest.parent / "possible_visual_duplicates.csv"
    assert result.visual_duplicates[-1].status == "possible_visual_duplicate"
    assert result.visual_duplicates[-1].destination_path == destination / "already-there.png"
    assert result.visual_duplicates[-1].hamming_distance is not None
    assert result.visual_duplicates[-1].source_thumbnail is not None
    assert result.visual_duplicates[-1].destination_thumbnail is not None
    assert (manifest.parent / result.visual_duplicates[-1].source_thumbnail).is_file()
    assert (manifest.parent / result.visual_duplicates[-1].destination_thumbnail).is_file()
    assert result.visual_html_report_path is not None
    assert result.visual_html_report_path.is_file()
    with result.visual_report_path.open(newline="", encoding="utf-8") as report_file:
        rows = list(csv.DictReader(report_file))
    assert rows[-1]["status"] == "possible_visual_duplicate"
    assert rows[-1]["source_thumbnail"]
    assert "Possible visual duplicates (1)" in result.visual_html_report_path.read_text(encoding="utf-8")


def test_visual_dedup_prevents_execute_mode_from_copying_a_visual_duplicate(tmp_path: Path) -> None:
    """Visual matching prevents a re-encoded image from entering iCloud again."""
    source = tmp_path / "USB" / "IMG_0007.JPG"
    source.parent.mkdir(parents=True)
    image = Image.new("RGB", (64, 48))
    image.putdata([(x * 4, y * 5, (x + y) * 2) for y in range(48) for x in range(64)])
    image.save(source, "JPEG", quality=70)
    manifest = tmp_path / "output" / "reports" / "manifest.csv"
    _write_manifest(manifest, [source])
    destination = tmp_path / "iCloud" / "Photos"
    destination.mkdir(parents=True)
    image.save(destination / "already-there.png", "PNG")

    result = import_manifest_to_icloud(manifest, destination, dry_run=False, visual_dedup=True)

    assert result.records[0].status == "duplicate_visual_content"
    assert not (destination / source.name).exists()


def test_visual_dedup_audits_duplicates_already_in_destination(tmp_path: Path) -> None:
    """An audit finds re-encoded visual matches within the iCloud folder itself."""
    destination = tmp_path / "iCloud" / "Photos"
    destination.mkdir(parents=True)
    image = Image.new("RGB", (64, 48))
    image.putdata([(x * 4, y * 5, (x + y) * 2) for y in range(48) for x in range(64)])
    first = destination / "first.png"
    second = destination / "second.jpg"
    image.save(first, "PNG")
    image.save(second, "JPEG", quality=70)
    manifest = tmp_path / "output" / "reports" / "manifest.csv"
    _write_manifest(manifest, [first])

    result = import_manifest_to_icloud(manifest, destination, visual_dedup=True)

    matches = [record for record in result.visual_duplicates if record.status == "possible_visual_duplicate"]
    assert len(matches) == 1
    assert matches[0].source_path == second
    assert matches[0].destination_path == first
    assert result.records[0].status == "already_in_destination"


def test_visual_audit_only_preserves_the_import_report(tmp_path: Path) -> None:
    """A destination-only visual scan bypasses normal import planning output."""
    destination = tmp_path / "iCloud" / "Photos"
    destination.mkdir(parents=True)
    image = Image.new("RGB", (8, 8), (12, 34, 56))
    source = destination / "photo.png"
    image.save(source)
    manifest = tmp_path / "output" / "reports" / "manifest.csv"
    _write_manifest(manifest, [source])
    import_report = manifest.parent / "icloud_import.csv"
    import_report.write_text("preserve me\n", encoding="utf-8")

    result = import_manifest_to_icloud(
        manifest,
        destination,
        visual_dedup=True,
        visual_audit_only=True,
    )

    assert result.records == []
    assert import_report.read_text(encoding="utf-8") == "preserve me\n"
    assert result.visual_report_path is not None
    assert result.visual_report_path.is_file()


def test_visual_cleanup_deletes_newest_file_from_a_connected_group(tmp_path: Path) -> None:
    """Cleanup keeps the oldest version and removes every newer visual duplicate."""
    destination = tmp_path / "iCloud" / "Photos"
    destination.mkdir(parents=True)
    first = destination / "first.jpg"
    second = destination / "second.jpg"
    third = destination / "third.jpg"
    for path in (first, second, third):
        path.write_bytes(b"photo")
    os.utime(first, ns=(1_000_000_000, 1_000_000_000))
    os.utime(second, ns=(2_000_000_000, 2_000_000_000))
    os.utime(third, ns=(3_000_000_000, 3_000_000_000))
    report = tmp_path / "reports" / "possible_visual_duplicates.csv"
    report.parent.mkdir()
    with report.open("w", newline="", encoding="utf-8") as report_file:
        writer = csv.DictWriter(report_file, fieldnames=["source_path", "destination_path", "status"])
        writer.writeheader()
        writer.writerows(
            [
                {"source_path": str(second), "destination_path": str(first), "status": "possible_visual_duplicate"},
                {"source_path": str(third), "destination_path": str(second), "status": "possible_visual_duplicate"},
            ]
        )

    records = delete_visual_duplicates(report, destination)

    assert first.exists()
    assert not second.exists()
    assert not third.exists()
    assert [record.status for record in records] == [
        "deleted_newest_visual_duplicate",
        "deleted_newest_visual_duplicate",
    ]
    assert (report.parent / "visual_duplicate_cleanup.csv").is_file()
