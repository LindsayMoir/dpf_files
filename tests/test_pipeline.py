"""Tests for the source-safe AGPTEK preparation pipeline."""

from __future__ import annotations

import csv
from pathlib import Path

import pytest
from PIL import Image

from dpf_files.config import load_config
from dpf_files.pipeline import PreparationConfig, SafetyError, prepare_library


def _write_jpeg(path: Path, color: tuple[int, int, int]) -> None:
    """Create a small valid JPEG fixture."""
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (8, 6), color).save(path, "JPEG")


def test_recursive_discovery_and_case_insensitive_extensions(tmp_path: Path) -> None:
    """Supported image extensions are found recursively, regardless of case."""
    source = tmp_path / "source"
    _write_jpeg(source / "nested" / "one.JPG", (255, 0, 0))
    Image.new("RGB", (5, 5), (0, 255, 0)).save(source / "nested" / "two.PnG")
    (source / "nested" / "notes.txt").write_text("not an image", encoding="utf-8")

    result = prepare_library(PreparationConfig(source, tmp_path / "output"))

    assert result.candidates == 2
    assert result.images_written == 2
    assert sorted(path.suffix for path in (tmp_path / "output" / "images").iterdir()) == [".jpg", ".png"]


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
    assert [path.name for path in sorted((tmp_path / "output" / "images").iterdir())] == [
        "000001.jpg",
        "000002.jpg",
    ]
    with (tmp_path / "output" / "reports" / "duplicates.csv").open(encoding="utf-8") as report:
        rows = list(csv.DictReader(report))
    assert rows[0]["duplicate_source_path"] == str(duplicate)
    assert rows[0]["retained_source_path"] == str(first)


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
    assert not (output / "images").exists()
    assert (output / "reports" / "manifest.csv").exists()
    assert result.manifest[0].status == "planned"


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
    existing = output / "images" / "keep.jpg"
    _write_jpeg(existing, (40, 50, 60))

    with pytest.raises(SafetyError, match="non-empty"):
        prepare_library(PreparationConfig(source, output))
    assert existing.exists()

    result = prepare_library(PreparationConfig(source, output, overwrite_output=True))
    assert result.images_written == 1
    assert not existing.exists()


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

    output_path = tmp_path / "output" / "images" / "000001.jpg"
    assert result.conversions_completed == 1
    with Image.open(output_path) as converted:
        converted.load()
        assert converted.format == "JPEG"
        assert converted.size == (20, 10)
