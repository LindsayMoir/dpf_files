"""Tests for the source-safe AGPTEK preparation pipeline."""

from __future__ import annotations

import csv
from pathlib import Path
import shutil

import pytest
from PIL import Image

from dpf_files.config import load_config
from dpf_files.pipeline import (
    PreparationConfig,
    SafetyError,
    VideoRecord,
    _report_path,
    archive_videos,
    prepare_library,
    reshuffle_output_dates,
)


def _write_jpeg(
    path: Path, color: tuple[int, int, int], captured_at: str | None = None
) -> None:
    """Create a small valid JPEG fixture."""
    path.parent.mkdir(parents=True, exist_ok=True)
    image = Image.new("RGB", (8, 6), color)
    if captured_at is not None:
        image.getexif()[36867] = captured_at
    image.save(path, "JPEG", exif=image.getexif())


def test_recursive_discovery_and_case_insensitive_extensions(tmp_path: Path) -> None:
    """Supported image extensions are found recursively, regardless of case."""
    source = tmp_path / "source"
    _write_jpeg(source / "nested" / "one.JPG", (255, 0, 0))
    Image.new("RGB", (5, 5), (0, 255, 0)).save(source / "nested" / "two.PnG")
    (source / "nested" / "notes.txt").write_text("not an image", encoding="utf-8")

    result = prepare_library(PreparationConfig(source, tmp_path / "output"))

    assert result.candidates == 2
    assert result.images_written == 2
    assert sorted(path.suffix for path in (tmp_path / "output" / "photos").rglob("*") if path.is_file()) == [".jpg", ".png"]


def test_videos_are_archived_without_overwriting_existing_files(tmp_path: Path) -> None:
    """Configured video archival moves videos and preserves name collisions."""
    source = tmp_path / "source"
    video = source / "nested" / "clip.MOV"
    video.parent.mkdir(parents=True)
    video.write_bytes(b"video")
    archive = tmp_path / "videos"
    archive.mkdir()
    (archive / "clip.MOV").write_bytes(b"existing video")

    result = prepare_library(
        PreparationConfig(source, tmp_path / "output", video_output=archive)
    )

    assert not video.exists()
    assert (archive / "clip.MOV").read_bytes() == b"existing video"
    assert (archive / "clip (2).MOV").read_bytes() == b"video"
    assert result.videos == [VideoRecord(video, archive / "clip (2).MOV", "moved")]


def test_video_only_archival_leaves_existing_image_output_untouched(tmp_path: Path) -> None:
    """Standalone archival moves videos without clearing generated image files."""
    source = tmp_path / "source"
    video = source / "clip.mp4"
    video.parent.mkdir(parents=True)
    video.write_bytes(b"video")
    archive = tmp_path / "videos"
    output = tmp_path / "output"
    existing_image = output / "photos" / "keep.jpg"
    _write_jpeg(existing_image, (1, 2, 3))

    result = archive_videos(
        PreparationConfig(source, output, video_output=archive)
    )

    assert not video.exists()
    assert (archive / "clip.mp4").exists()
    assert existing_image.exists()
    assert result.errors == []


def test_duplicate_and_same_filename_sources_are_safe(tmp_path: Path) -> None:
    """Exact duplicates are skipped while distinct same-name files receive unique names."""
    source = tmp_path / "source"
    first = source / "a" / "photo.jpg"
    duplicate = source / "b" / "duplicate.jpg"
    second = source / "c" / "photo.jpg"
    _write_jpeg(first, (255, 0, 0))
    duplicate.parent.mkdir(parents=True, exist_ok=True)
    duplicate.write_bytes(first.read_bytes())
    _write_jpeg(second, (0, 0, 255))

    result = prepare_library(PreparationConfig(source, tmp_path / "output"))

    assert result.images_written == 2
    assert result.duplicates_skipped == 1
    assert [path.name for path in sorted((tmp_path / "output" / "photos").rglob("*.jpg"))] == [
        "0001.jpg",
        "0002.jpg",
    ]
    with (tmp_path / "output" / "reports" / "duplicates.csv").open(encoding="utf-8") as report:
        rows = list(csv.DictReader(report))
    assert rows[0]["duplicate_source_path"] == str(duplicate)
    assert rows[0]["retained_source_path"] == str(first)


def test_visual_duplicates_are_excluded_from_usb_output(tmp_path: Path) -> None:
    """Re-encoded versions of one image produce only one USB output file."""
    source = tmp_path / "source"
    source.mkdir()
    image = Image.new("RGB", (64, 48))
    image.putdata([(x * 4, y * 5, (x + y) * 2) for y in range(48) for x in range(64)])
    image.save(source / "original.png", "PNG")
    image.save(source / "reencoded.jpg", "JPEG", quality=70)

    result = prepare_library(PreparationConfig(source=source, output=tmp_path / "output"))

    assert result.images_written == 1
    assert result.duplicates_skipped == 1
    assert result.duplicates[0].duplicate_type == "visual_content"


def test_corrupt_image_is_logged_and_source_is_unchanged(tmp_path: Path) -> None:
    """A corrupt image does not stop valid inputs or modify any source bytes."""
    source = tmp_path / "source with ünicode"
    valid = source / "valid image.jpg"
    corrupt = source / "corrupt.JPEG"
    _write_jpeg(valid, (1, 2, 3))
    corrupt.write_bytes(b"this is not a JPEG")
    original_bytes = {path: path.read_bytes() for path in (valid, corrupt)}

    result = prepare_library(PreparationConfig(source, tmp_path / "output directory"))

    assert result.images_written == 1
    assert len(result.errors) == 1
    assert result.errors[0].source_path == corrupt
    assert {path: path.read_bytes() for path in (valid, corrupt)} == original_bytes


def test_dry_run_writes_reports_but_no_images(tmp_path: Path) -> None:
    """Dry runs report planned work without creating an image output directory."""
    source = tmp_path / "source"
    _write_jpeg(source / "photo.jpg", (10, 20, 30))
    output = tmp_path / "output"

    result = prepare_library(PreparationConfig(source, output, dry_run=True))

    assert result.unique_images == 1
    assert result.images_written == 0
    assert not (output / "photos").exists()
    assert (output / "reports" / "manifest.csv").exists()
    assert result.manifest[0].status == "planned"


def test_manifest_maps_each_output_path_to_its_original_folder(tmp_path: Path) -> None:
    """The manifest supplies complete paths for both the generated and source files."""
    source = tmp_path / "source" / "original album"
    original = source / "photo.jpg"
    _write_jpeg(original, (10, 20, 30))
    output = tmp_path / "output"

    prepare_library(PreparationConfig(source, output))

    with (output / "reports" / "manifest.csv").open(newline="", encoding="utf-8") as report_file:
        row = next(csv.DictReader(report_file))

    expected_output = output / "photos" / row["playback_folder"] / row["output_filename"]
    assert row["output_path"] == str(expected_output.resolve())
    assert row["source_path"] == str(original.resolve())
    assert row["source_folder"] == str(source.resolve())


def test_report_paths_convert_wsl_windows_drives_for_file_explorer() -> None:
    """WSL-backed source paths are written in Windows Explorer form."""
    assert _report_path(Path("/mnt/d/OneDrive/USB/photo.jpg")) == "D:\\OneDrive\\USB\\photo.jpg"


def test_max_files_selects_a_deterministic_small_trial(tmp_path: Path) -> None:
    """A trial limit processes only the first sorted supported image files."""
    source = tmp_path / "source"
    _write_jpeg(source / "c.jpg", (1, 2, 3))
    _write_jpeg(source / "a.jpg", (4, 5, 6))
    _write_jpeg(source / "b.jpg", (7, 8, 9))

    result = prepare_library(PreparationConfig(source, tmp_path / "output", max_files=2))

    assert result.candidates_discovered == 3
    assert result.candidates == 2
    assert [record.source_filename for record in result.manifest] == ["a.jpg", "b.jpg"]
    assert result.images_written == 2


def test_yaml_config_resolves_relative_paths_and_controls_trial(tmp_path: Path) -> None:
    """The YAML file, rather than application code, supplies machine-specific paths."""
    config_path = tmp_path / "settings.yaml"
    config_path.write_text(
        "\n".join(
            [
                "source: source library",
                "output: prepared output",
                "max_files: 3",
                "dry_run: true",
                "overwrite_output: false",
                "jpeg_quality: 88",
            ]
        ),
        encoding="utf-8",
    )

    config = load_config(config_path)

    assert config.source == tmp_path / "source library"
    assert config.output == tmp_path / "prepared output"
    assert config.max_files == 3
    assert config.dry_run is True
    assert config.jpeg_quality == 88


def test_multiple_source_roots_are_scanned_and_exact_duplicates_are_skipped(
    tmp_path: Path,
) -> None:
    """Multiple source roots feed the existing duplicate-safe processing path."""
    primary_source = tmp_path / "primary"
    iphone_imports = tmp_path / "iPhone imports"
    original = primary_source / "original.jpg"
    duplicate = iphone_imports / "duplicate.jpg"
    unique = iphone_imports / "unique.jpg"
    _write_jpeg(original, (1, 2, 3))
    duplicate.parent.mkdir(parents=True, exist_ok=True)
    duplicate.write_bytes(original.read_bytes())
    _write_jpeg(unique, (4, 5, 6))

    result = prepare_library(
        PreparationConfig(
            primary_source,
            tmp_path / "output",
            additional_sources=(iphone_imports,),
        )
    )

    assert result.candidates_discovered == 3
    assert result.images_written == 2
    assert result.duplicates_skipped == 1
    assert result.sources == (primary_source.resolve(), iphone_imports.resolve())


def test_yaml_config_accepts_multiple_source_paths(tmp_path: Path) -> None:
    """A sources list resolves every relative path from the YAML location."""
    config_path = tmp_path / "settings.yaml"
    config_path.write_text(
        "sources:\n  - original library\n  - iPhone imports\noutput: output\n",
        encoding="utf-8",
    )

    config = load_config(config_path)

    assert config.source_roots == (
        tmp_path / "original library",
        tmp_path / "iPhone imports",
    )


def test_nested_source_roots_are_rejected(tmp_path: Path) -> None:
    """Nested roots are refused because they would scan the same files twice."""
    primary_source = tmp_path / "primary"
    nested_source = primary_source / "nested"
    nested_source.mkdir(parents=True)

    with pytest.raises(SafetyError, match="must not contain one another"):
        prepare_library(
            PreparationConfig(
                primary_source,
                tmp_path / "output",
                additional_sources=(nested_source,),
            )
        )


def test_output_order_is_ascending_by_capture_date(tmp_path: Path) -> None:
    """Sequential filenames within a folder follow ascending EXIF capture dates."""
    source = tmp_path / "source"
    _write_jpeg(source / "march.jpg", (1, 2, 3), "2024:03:01 12:00:00")
    _write_jpeg(source / "january.jpg", (4, 5, 6), "2024:01:01 12:00:00")
    _write_jpeg(source / "february.jpg", (7, 8, 9), "2024:02:01 12:00:00")

    result = prepare_library(PreparationConfig(source, tmp_path / "output"))

    assert [record.source_filename for record in result.manifest] == [
        "january.jpg",
        "february.jpg",
        "march.jpg",
    ]
    assert [record.output_filename for record in result.manifest] == [
        "0001.jpg",
        "0002.jpg",
        "0003.jpg",
    ]


@pytest.mark.parametrize(
    ("filename", "expected", "date_source"),
    [
        ("20160718_120537-legacy.jpg", "2016-07-18T12:05:37", "filename_machine"),
        ("2016-08-03-holiday.jpg", "2016-08-03T00:00:00", "filename_machine"),
        ("2016_04_30_130509.jpg", "2016-04-30T13:05:09", "filename_machine"),
        ("Sherlayn Beach Windemere Jun 22 2008.jpg", "2008-06-22T00:00:00", "filename_human"),
        ("Edited Ivan's Boat 2 Feb 24 2009.jpg", "2009-02-24T00:00:00", "filename_human"),
    ],
)
def test_legacy_filename_dates_override_recent_filesystem_dates(
    tmp_path: Path, filename: str, expected: str, date_source: str
) -> None:
    """Credible filename dates win over recent iCloud copy timestamps."""
    source = tmp_path / "source"
    _write_jpeg(source / filename, (1, 2, 3))

    result = prepare_library(PreparationConfig(source, tmp_path / "output"))

    record = result.manifest[0]
    assert record.canonical_date == expected
    assert record.capture_date == expected
    assert record.date_source == date_source
    with (tmp_path / "output" / "reports" / "manifest.csv").open(newline="", encoding="utf-8") as report:
        row = next(csv.DictReader(report))
    assert row["capture_date"] == expected
    assert row["date_source"] == date_source


def test_embedded_original_date_overrides_legacy_filename_date(tmp_path: Path) -> None:
    """Valid EXIF DateTimeOriginal remains the highest-priority capture date."""
    source = tmp_path / "source"
    _write_jpeg(source / "20160718_120537.jpg", (1, 2, 3), "2005:04:03 02:01:00")

    result = prepare_library(PreparationConfig(source, tmp_path / "output"))

    assert result.manifest[0].canonical_date == "2005-04-03T02:01:00"
    assert result.manifest[0].date_source == "exif_datetime_original"


def test_unambiguous_folder_date_precedes_filesystem_fallback(tmp_path: Path) -> None:
    """Date-like folders supply a date only when a filename has none."""
    source = tmp_path / "source" / "2008-06-22"
    _write_jpeg(source / "photo.jpg", (1, 2, 3))

    result = prepare_library(PreparationConfig(tmp_path / "source", tmp_path / "output"))

    assert result.manifest[0].canonical_date == "2008-06-22T00:00:00"
    assert result.manifest[0].date_source == "folder_machine"


def test_invalid_filename_calendar_date_falls_back_to_filesystem(tmp_path: Path) -> None:
    """Date-looking but impossible filenames never create false capture dates."""
    source = tmp_path / "source"
    _write_jpeg(source / "2016-02-31.jpg", (1, 2, 3))

    result = prepare_library(PreparationConfig(source, tmp_path / "output"))

    assert result.manifest[0].date_source.startswith("filesystem_")


def test_disappearing_source_during_date_grouping_is_logged_and_skipped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A transient iCloud removal after hashing does not stop the full rebuild."""
    source = tmp_path / "source"
    unavailable = source / "unavailable.jpg"
    _write_jpeg(unavailable, (1, 2, 3))

    def raise_unavailable(path: Path) -> tuple[object, str]:
        raise FileNotFoundError(f"No longer available: {path}")

    monkeypatch.setattr("dpf_files.pipeline._canonical_date", raise_unavailable)
    result = prepare_library(PreparationConfig(source, tmp_path / "output"))

    assert result.images_written == 0
    assert result.errors[0].operation == "capture_date"
    assert result.errors[0].source_path == unavailable


def test_date_reshuffle_regroups_existing_output_without_reprocessing_sources(tmp_path: Path) -> None:
    """Existing USB files are copied into corrected groups using manifest source paths."""
    source = tmp_path / "source"
    _write_jpeg(source / "20160718_120537.jpg", (1, 2, 3))
    _write_jpeg(source / "Beach Jun 22 2008.jpg", (4, 5, 6))
    output = tmp_path / "output"
    prepare_library(PreparationConfig(source, output))
    before = {path.read_bytes() for path in (output / "photos").rglob("*.jpg")}
    for source_file in source.iterdir():
        source_file.unlink()

    reshuffled = reshuffle_output_dates(PreparationConfig(source, output))

    assert reshuffled.images_reshuffled == 2
    assert (output / "photos" / "2008" / "0001.jpg").exists()
    assert (output / "photos" / "2008-2016" / "0001.jpg").exists()
    assert {path.read_bytes() for path in (output / "photos").rglob("*.jpg")} == before
    with (output / "reports" / "manifest.csv").open(newline="", encoding="utf-8") as report:
        rows = list(csv.DictReader(report))
    assert [row["date_source"] for row in rows] == ["filename_human", "filename_machine"]


def test_date_reshuffle_refuses_unlisted_output_files(tmp_path: Path) -> None:
    """An unexpected USB file prevents a reshuffle from losing user data."""
    source = tmp_path / "source"
    _write_jpeg(source / "20160718_120537.jpg", (1, 2, 3))
    output = tmp_path / "output"
    prepare_library(PreparationConfig(source, output))
    _write_jpeg(output / "photos" / "user-file.jpg", (4, 5, 6))

    with pytest.raises(SafetyError, match="Manifest and photos output differ"):
        reshuffle_output_dates(PreparationConfig(source, output))


def test_yaml_config_translates_windows_paths_when_running_in_wsl(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Windows drive paths remain usable from a WSL terminal."""
    config_path = tmp_path / "settings.yaml"
    config_path.write_text(
        "source: 'D:/OneDrive/USB'\noutput: 'D:/OneDrive/USB_OUTPUT'\n",
        encoding="utf-8",
    )
    monkeypatch.setattr("dpf_files.config._is_wsl", lambda: True)

    config = load_config(config_path)

    assert config.source == Path("/mnt/d/OneDrive/USB")
    assert config.output == Path("/mnt/d/OneDrive/USB_OUTPUT")


def test_non_empty_output_requires_explicit_overwrite(tmp_path: Path) -> None:
    """Existing output images cannot be replaced without an explicit flag."""
    source = tmp_path / "source"
    _write_jpeg(source / "photo.jpg", (10, 20, 30))
    output = tmp_path / "output"
    existing = output / "photos" / "keep.jpg"
    _write_jpeg(existing, (40, 50, 60))

    with pytest.raises(SafetyError, match="non-empty"):
        prepare_library(PreparationConfig(source, output))
    assert existing.exists()

    result = prepare_library(PreparationConfig(source, output, overwrite_output=True))
    assert result.images_written == 1
    assert not existing.exists()


def test_locked_report_does_not_clear_existing_images(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A report cleanup failure leaves the existing image output intact."""
    source = tmp_path / "source"
    _write_jpeg(source / "photo.jpg", (10, 20, 30))
    output = tmp_path / "output"
    existing_image = output / "photos" / "keep.jpg"
    locked_report = output / "reports" / "manifest.csv"
    _write_jpeg(existing_image, (40, 50, 60))
    locked_report.parent.mkdir(parents=True)
    locked_report.write_text("locked", encoding="utf-8")
    original_rmtree = shutil.rmtree

    def reject_locked_report(directory: Path) -> None:
        if directory == locked_report.parent:
            raise PermissionError("manifest.csv is open")
        original_rmtree(directory)

    monkeypatch.setattr("dpf_files.pipeline.shutil.rmtree", reject_locked_report)

    with pytest.raises(PermissionError, match="manifest.csv is open"):
        prepare_library(PreparationConfig(source, output, overwrite_output=True))

    assert existing_image.exists()


def test_heif_is_converted_to_oriented_readable_jpeg(tmp_path: Path) -> None:
    """HEIF sources become readable, orientation-correct JPEG output files."""
    pillow_heif = pytest.importorskip("pillow_heif")
    source = tmp_path / "source"
    source.mkdir()
    heif_path = source / "portrait.HeIc"
    image = Image.new("RGB", (10, 20), (100, 110, 120))
    image.getexif()[274] = 6
    pillow_heif.from_pillow(image).save(heif_path)

    result = prepare_library(PreparationConfig(source, tmp_path / "output"))

    output_path = next((tmp_path / "output" / "photos").rglob("0001.jpg"))
    assert result.conversions_completed == 1
    with Image.open(output_path) as converted:
        converted.load()
        assert converted.format == "JPEG"
        assert converted.size == (20, 10)
