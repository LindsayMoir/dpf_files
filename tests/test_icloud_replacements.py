"""Tests for explicit, recoverable iCloud photo replacements."""

from __future__ import annotations

import csv
from datetime import date
from pathlib import Path

from PIL import Image

from dpf_files.icloud_replacements import (
    ICloudReplacementConfig,
    load_icloud_replacement_config,
    prepare_native_windows_replacements,
    replace_icloud_photos,
)
from dpf_files.pipeline import format_report_path


def _write_image(path: Path, image_format: str, color: tuple[int, int, int] = (20, 30, 40)) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (40, 30), color).save(path, image_format)


def _config(tmp_path: Path) -> ICloudReplacementConfig:
    return ICloudReplacementConfig(
        updates_directory=tmp_path / "updates",
        icloud_directory=tmp_path / "icloud",
        backup_directory=tmp_path / "backups",
        report_path=tmp_path / "reports" / "replacements.csv",
        replacement_date=date(1955, 4, 27),
        icloud_delete_settle_seconds=0,
        superseded_sources_report=tmp_path / "reports" / "superseded.csv",
    )


def test_dry_run_never_copies_or_deletes(tmp_path: Path) -> None:
    config = _config(tmp_path)
    original = config.icloud_directory / "photo_1174.jpg"
    update = config.updates_directory / "photo_1174_restored.png"
    _write_image(original, "JPEG")
    _write_image(update, "PNG", (70, 80, 90))
    original_bytes = original.read_bytes()

    result = replace_icloud_photos(config)

    assert result.records[0].status == "would_replace"
    assert original.read_bytes() == original_bytes
    assert not (config.icloud_directory / "photo_1174_restored.jpg").exists()
    assert not config.backup_directory.exists()
    with config.report_path.open(newline="", encoding="utf-8") as file:
        assert next(csv.DictReader(file))["status"] == "would_replace"


def test_execute_creates_dated_jpeg_backup_then_deletes_exact_original(tmp_path: Path) -> None:
    config = _config(tmp_path)
    original = config.icloud_directory / "photo_1174.jpg"
    update = config.updates_directory / "photo_1174_restored.png"
    _write_image(original, "JPEG")
    _write_image(update, "PNG", (70, 80, 90))
    original_bytes = original.read_bytes()

    result = replace_icloud_photos(config, dry_run=False)

    replacement = config.icloud_directory / "photo_1174_restored.jpg"
    backup = config.backup_directory / "originals" / "photo_1174.jpg"
    assert result.records[0].status == "replaced"
    assert not original.exists()
    assert backup.read_bytes() == original_bytes
    with Image.open(replacement) as image:
        assert image.format == "JPEG"
        assert image.getexif().get(306) == "1955:04:27 12:00:00"
        assert image.getexif().get(36867) == "1955:04:27 12:00:00"
        assert image.getexif().get(36868) == "1955:04:27 12:00:00"
    with config.superseded_sources_report.open(newline="", encoding="utf-8") as file:
        ledger_row = next(csv.DictReader(file))
    assert ledger_row["original_path"] == str(original)
    assert ledger_row["replacement_path"] == str(replacement)
    assert ledger_row["status"] == "superseded"

    rerun = replace_icloud_photos(config, dry_run=False)
    assert rerun.records[0].status == "already_replaced"


def test_invalid_duplicate_and_ambiguous_updates_are_not_executed(tmp_path: Path) -> None:
    config = _config(tmp_path)
    _write_image(config.icloud_directory / "photo_1174.jpg", "JPEG")
    _write_image(config.icloud_directory / "photo_1174.jpeg", "JPEG")
    _write_image(config.updates_directory / "photo_1174_restored.png", "PNG")
    _write_image(config.updates_directory / "photo_1174_restored..png", "PNG")
    _write_image(config.updates_directory / "not_a_restoration.png", "PNG")

    result = replace_icloud_photos(config, dry_run=False)

    statuses = {record.status for record in result.records}
    assert "ambiguous_original" in statuses
    assert "duplicate_update_identifier" in statuses
    assert "invalid_update_filename" in statuses
    assert (config.icloud_directory / "photo_1174.jpg").exists()
    assert not (config.icloud_directory / "photo_1174_restored.jpg").exists()


def test_yaml_configuration_accepts_yaml_date_values(tmp_path: Path) -> None:
    config = _config(tmp_path)
    config.updates_directory.mkdir()
    config.icloud_directory.mkdir()
    yaml_path = tmp_path / "replacements.yaml"
    yaml_path.write_text(
        "\n".join(
            [
                f'updates_directory: "{config.updates_directory}"',
                f'icloud_directory: "{config.icloud_directory}"',
                f'backup_directory: "{config.backup_directory}"',
                f'report_path: "{config.report_path}"',
                "replacement_date: 1955-04-27",
                "icloud_delete_settle_seconds: 0",
                "icloud_delete_attempts: 3",
            ]
        ),
        encoding="utf-8",
    )

    loaded = load_icloud_replacement_config(yaml_path)

    assert loaded.replacement_date == date(1955, 4, 27)


def test_native_windows_preparation_never_requires_icloud_access(tmp_path: Path) -> None:
    config = _config(tmp_path)
    update = config.updates_directory / "photo_1174_restored.png"
    _write_image(update, "PNG", (70, 80, 90))

    result = prepare_native_windows_replacements(config)

    prepared = config.updates_directory / "ready_for_icloud" / "photo_1174_restored.jpg"
    manifest = config.updates_directory / "reports" / "native_windows_replacements.csv"
    script = config.updates_directory / "reports" / "apply_icloud_replacements.ps1"
    assert result.records[0].status == "prepared"
    assert prepared.is_file()
    with Image.open(prepared) as image:
        assert image.format == "JPEG"
        assert image.getexif().get(36867) == "1955:04:27 12:00:00"
    with manifest.open(newline="", encoding="utf-8") as file:
        row = next(csv.DictReader(file))
    assert row["original_filename"] == "photo_1174.jpg"
    assert row["prepared_path"] == format_report_path(prepared)
    contents = script.read_text(encoding="utf-8")
    assert "param([switch]$WhatIf, [switch]$SkipBackup, [switch]$SkipICloudHashVerification)" in contents
    assert "Copy-Item" in contents
    assert "Remove-Item" in contents
    assert "Get-FileHash" in contents
