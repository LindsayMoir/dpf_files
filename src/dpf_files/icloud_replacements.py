"""Safely replace named iCloud originals with user-created restored images."""

from __future__ import annotations

import csv
from dataclasses import dataclass, field
from datetime import date
import hashlib
import os
from pathlib import Path
import re
import shutil
import time
from typing import Final
from uuid import uuid4

import yaml
from PIL import Image

from dpf_files.config import normalize_path
from dpf_files.pipeline import format_report_path

SUPPORTED_UPDATE_EXTENSIONS: Final[frozenset[str]] = frozenset({".png", ".jpg", ".jpeg"})
ORIGINAL_EXTENSIONS: Final[frozenset[str]] = frozenset({".jpg", ".jpeg"})
RESTORED_NAME_PATTERN: Final[re.Pattern[str]] = re.compile(r"^(?P<base>.+?)_restored\.*$", re.IGNORECASE)
EXIF_DATETIME_FORMAT: Final[str] = "%Y:%m:%d 12:00:00"
JPEG_QUALITY: Final[int] = 95
DEFAULT_ICLOUD_DELETE_SETTLE_SECONDS: Final[float] = 30.0
DEFAULT_ICLOUD_DELETE_ATTEMPTS: Final[int] = 3
CONFIG_KEYS: Final[frozenset[str]] = frozenset(
    {
        "updates_directory", "icloud_directory", "backup_directory", "report_path", "replacement_date",
        "icloud_delete_settle_seconds", "icloud_delete_attempts", "superseded_sources_report",
        "prepared_directory", "native_windows_manifest_path", "native_windows_script_path",
        "native_windows_results_path",
    }
)
SUPERSEDED_LEDGER_HEADERS: Final[tuple[str, ...]] = (
    "original_path", "replacement_path", "replacement_sha256", "status",
)
LEDGER_ELIGIBLE_STATUSES: Final[frozenset[str]] = frozenset({"replaced", "delete_not_persistent", "already_replaced"})


class ICloudReplacementError(ValueError):
    """Raised when a replacement plan is not safe to execute."""


@dataclass(frozen=True)
class ICloudReplacementConfig:
    """Configuration for one explicit iCloud replacement batch."""

    updates_directory: Path
    icloud_directory: Path
    backup_directory: Path
    report_path: Path
    replacement_date: date
    icloud_delete_settle_seconds: float = DEFAULT_ICLOUD_DELETE_SETTLE_SECONDS
    icloud_delete_attempts: int = DEFAULT_ICLOUD_DELETE_ATTEMPTS
    superseded_sources_report: Path | None = None
    prepared_directory: Path | None = None
    native_windows_manifest_path: Path | None = None
    native_windows_script_path: Path | None = None
    native_windows_results_path: Path | None = None


@dataclass(frozen=True)
class ReplacementRecord:
    """One planned or completed iCloud replacement decision."""

    update_path: Path
    original_path: Path | None
    replacement_path: Path | None
    backup_path: Path | None
    status: str
    detail: str = ""
    update_sha256: str = ""
    replacement_sha256: str = ""


@dataclass
class ICloudReplacementResult:
    """Records and paths created by a replacement run."""

    config: ICloudReplacementConfig
    dry_run: bool
    records: list[ReplacementRecord] = field(default_factory=list)

    def summary_text(self) -> str:
        """Return a concise, operator-facing replacement summary."""
        counts: dict[str, int] = {}
        for record in self.records:
            counts[record.status] = counts.get(record.status, 0) + 1
        lines = [
            f"Mode: {'dry run' if self.dry_run else 'execute'}",
            f"Updates: {format_report_path(self.config.updates_directory)}",
            f"iCloud Photos: {format_report_path(self.config.icloud_directory)}",
            f"Replacement date: {self.config.replacement_date.isoformat()}",
            f"Records: {len(self.records)}",
        ]
        lines.extend(f"{status}: {count}" for status, count in sorted(counts.items()))
        lines.append(f"Report: {format_report_path(self.config.report_path)}")
        return "\n".join(lines) + "\n"


@dataclass(frozen=True)
class NativePreparationRecord:
    """One offline-prepared JPEG and its exact iCloud original mapping."""

    update_path: Path
    original_filename: str | None
    prepared_path: Path | None
    status: str
    prepared_sha256: str = ""
    detail: str = ""


@dataclass
class NativePreparationResult:
    """Offline preparation artifacts for the native Windows iCloud executor."""

    config: ICloudReplacementConfig
    records: list[NativePreparationRecord] = field(default_factory=list)

    def summary_text(self) -> str:
        counts: dict[str, int] = {}
        for record in self.records:
            counts[record.status] = counts.get(record.status, 0) + 1
        lines = ["Native Windows iCloud preparation completed", f"Records: {len(self.records)}"]
        lines.extend(f"{status}: {count}" for status, count in sorted(counts.items()))
        lines.extend([
            f"Prepared JPEGs: {format_report_path(_prepared_directory(self.config))}",
            f"Manifest: {format_report_path(_native_manifest_path(self.config))}",
            f"PowerShell executor: {format_report_path(_native_script_path(self.config))}",
        ])
        return "\n".join(lines) + "\n"


def load_icloud_replacement_config(path: Path) -> ICloudReplacementConfig:
    """Load and validate a replacement configuration from YAML."""
    config_path = normalize_path(path)
    if not config_path.is_file():
        raise ICloudReplacementError(f"Configuration does not exist: {format_report_path(config_path)}")
    try:
        data = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as error:
        raise ICloudReplacementError(f"Unable to read configuration: {error}") from error
    if not isinstance(data, dict):
        raise ICloudReplacementError("Replacement configuration must be a YAML mapping.")
    unknown_keys = set(data) - CONFIG_KEYS
    if unknown_keys:
        formatted = ", ".join(sorted(str(key) for key in unknown_keys))
        raise ICloudReplacementError(f"Unknown replacement configuration key(s): {formatted}")
    base = config_path.parent
    updates = _required_path(data, "updates_directory", base)
    icloud = _required_path(data, "icloud_directory", base)
    backup = _required_path(data, "backup_directory", base)
    report = _required_path(data, "report_path", base)
    raw_date = data.get("replacement_date")
    if isinstance(raw_date, date):
        replacement_date = raw_date
    elif isinstance(raw_date, str):
        try:
            replacement_date = date.fromisoformat(raw_date)
        except ValueError as error:
            raise ICloudReplacementError("replacement_date must use YYYY-MM-DD format.") from error
    else:
        raise ICloudReplacementError("replacement_date must use YYYY-MM-DD format.")
    config = ICloudReplacementConfig(
        updates,
        icloud,
        backup,
        report,
        replacement_date,
        _nonnegative_number(data.get("icloud_delete_settle_seconds", DEFAULT_ICLOUD_DELETE_SETTLE_SECONDS), "icloud_delete_settle_seconds"),
        _positive_integer(data.get("icloud_delete_attempts", DEFAULT_ICLOUD_DELETE_ATTEMPTS), "icloud_delete_attempts"),
        _optional_path(data.get("superseded_sources_report"), "superseded_sources_report", base),
        _optional_path(data.get("prepared_directory"), "prepared_directory", base),
        _optional_path(data.get("native_windows_manifest_path"), "native_windows_manifest_path", base),
        _optional_path(data.get("native_windows_script_path"), "native_windows_script_path", base),
        _optional_path(data.get("native_windows_results_path"), "native_windows_results_path", base),
    )
    _validate_config(config)
    return config


def prepare_native_windows_replacements(config: ICloudReplacementConfig) -> NativePreparationResult:
    """Prepare restored JPEGs and a native Windows executor without accessing iCloud files."""
    prepared_directory = _prepared_directory(config)
    result = NativePreparationResult(config)
    seen_bases: set[str] = set()
    for update in _update_files(config.updates_directory):
        base = _replacement_base(update)
        if base is None:
            result.records.append(NativePreparationRecord(update, None, None, "invalid_update_filename"))
            continue
        if base.casefold() in seen_bases:
            result.records.append(NativePreparationRecord(update, None, None, "duplicate_update_identifier"))
            continue
        seen_bases.add(base.casefold())
        prepared = prepared_directory / f"{base}_restored.jpg"
        temporary = prepared_directory / f".{prepared.name}.{uuid4().hex}.tmp"
        try:
            prepared_directory.mkdir(parents=True, exist_ok=True)
            _render_replacement_jpeg(update, temporary, config.replacement_date)
            prepared_hash = _sha256(temporary)
            _verify_replacement(temporary, prepared_hash, config.replacement_date)
            if prepared.exists():
                if not prepared.is_file() or _sha256(prepared) != prepared_hash:
                    result.records.append(NativePreparationRecord(
                        update, f"{base}.jpg", prepared, "prepared_target_conflict", prepared_hash,
                        "Existing prepared JPEG differs from the update.",
                    ))
                    continue
                status = "prepared_existing"
            else:
                os.replace(temporary, prepared)
                _verify_replacement(prepared, prepared_hash, config.replacement_date)
                status = "prepared"
            result.records.append(NativePreparationRecord(update, f"{base}.jpg", prepared, status, prepared_hash))
        except (OSError, ValueError) as error:
            result.records.append(NativePreparationRecord(update, f"{base}.jpg", prepared, "preparation_failed", detail=str(error)))
        finally:
            temporary.unlink(missing_ok=True)
    _write_native_manifest(result)
    _write_native_windows_script(config)
    return result


def replace_icloud_photos(config: ICloudReplacementConfig, *, dry_run: bool = True) -> ICloudReplacementResult:
    """Plan or execute verified JPEG replacements without guessing filename matches."""
    if not config.icloud_directory.is_dir():
        raise ICloudReplacementError(f"iCloud directory does not exist: {format_report_path(config.icloud_directory)}")
    originals = _original_index(config.icloud_directory)
    result = ICloudReplacementResult(config, dry_run)
    seen_bases: set[str] = set()
    for update in _update_files(config.updates_directory):
        base = _replacement_base(update)
        if base is None:
            result.records.append(ReplacementRecord(update, None, None, None, "invalid_update_filename"))
            continue
        if base.casefold() in seen_bases:
            result.records.append(ReplacementRecord(update, None, None, None, "duplicate_update_identifier", base))
            continue
        seen_bases.add(base.casefold())
        matches = originals.get(base.casefold(), [])
        target = config.icloud_directory / f"{base}_restored.jpg"
        backup = config.backup_directory / "originals" / (matches[0].name if len(matches) == 1 else f"{base}.jpg")
        if len(matches) > 1:
            result.records.append(ReplacementRecord(update, None, target, None, "ambiguous_original", "More than one .jpg/.jpeg file has this stem."))
            continue
        if not matches:
            status = "already_replaced" if target.is_file() else "missing_original"
            result.records.append(ReplacementRecord(update, None, target, None, status))
            continue
        original = matches[0]
        if dry_run:
            status = "would_resume_or_conflict" if target.exists() else "would_replace"
            result.records.append(ReplacementRecord(update, original, target, backup, status, update_sha256=_sha256(update)))
            continue
        result.records.append(_execute_replacement(config, update, original, target, backup))
    _write_report(result)
    if not dry_run:
        _update_superseded_sources_ledger(result)
    return result


def seed_superseded_sources_ledger(config: ICloudReplacementConfig, report_path: Path) -> int:
    """Create or extend the durable ledger from a completed replacement CSV report.

    This is deliberately read-only with respect to both the iCloud directory and
    image files. Only records that reached a verified replacement copy are kept.
    """
    source_report = normalize_path(report_path).resolve(strict=False)
    if not source_report.is_file():
        raise ICloudReplacementError(f"Replacement report does not exist: {format_report_path(source_report)}")
    with source_report.open(newline="", encoding="utf-8-sig") as file:
        reader = csv.DictReader(file)
        required = {"original_path", "replacement_path", "status", "replacement_sha256"}
        if reader.fieldnames is None or not required.issubset(reader.fieldnames):
            raise ICloudReplacementError("Replacement report is missing fields needed to seed the superseded-source ledger.")
        records = [
            ReplacementRecord(
                update_path=Path(),
                original_path=normalize_path(row["original_path"]).resolve(strict=False) if row["original_path"] else None,
                replacement_path=normalize_path(row["replacement_path"]).resolve(strict=False) if row["replacement_path"] else None,
                backup_path=None,
                status=row["status"],
                replacement_sha256=row["replacement_sha256"],
            )
            for row in reader
        ]
    return _merge_superseded_sources_ledger(config, records)


def seed_native_windows_results(config: ICloudReplacementConfig) -> int:
    """Add verified native Windows replacement outcomes to the USB exclusion ledger."""
    return seed_superseded_sources_ledger(config, _native_results_path(config))


def _prepared_directory(config: ICloudReplacementConfig) -> Path:
    """Return the non-iCloud directory used for rendered restored JPEGs."""
    return config.prepared_directory or config.updates_directory / "ready_for_icloud"


def _native_manifest_path(config: ICloudReplacementConfig) -> Path:
    """Return the offline mapping consumed by the native Windows executor."""
    return config.native_windows_manifest_path or config.updates_directory / "reports" / "native_windows_replacements.csv"


def _native_script_path(config: ICloudReplacementConfig) -> Path:
    """Return the generated native Windows PowerShell executor path."""
    return config.native_windows_script_path or config.updates_directory / "reports" / "apply_icloud_replacements.ps1"


def _native_results_path(config: ICloudReplacementConfig) -> Path:
    """Return the native Windows executor's live result report path."""
    return config.native_windows_results_path or config.updates_directory / "reports" / "native_windows_replacement_results.csv"


def _write_native_manifest(result: NativePreparationResult) -> None:
    """Write the exact offline mapping without inspecting the iCloud folder."""
    manifest_path = _native_manifest_path(result.config)
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = manifest_path.parent / f".{manifest_path.name}.{uuid4().hex}.tmp"
    try:
        with temporary.open("w", newline="", encoding="utf-8") as file:
            writer = csv.writer(file)
            writer.writerow(["update_path", "original_filename", "prepared_path", "prepared_sha256", "status", "detail"])
            for record in result.records:
                writer.writerow([
                    format_report_path(record.update_path),
                    "" if record.original_filename is None else record.original_filename,
                    "" if record.prepared_path is None else format_report_path(record.prepared_path),
                    record.prepared_sha256,
                    record.status,
                    record.detail,
                ])
        os.replace(temporary, manifest_path)
    finally:
        temporary.unlink(missing_ok=True)


def _write_native_windows_script(config: ICloudReplacementConfig) -> None:
    """Generate a native PowerShell executor; it is not run by this utility."""
    script_path = _native_script_path(config)
    script_path.parent.mkdir(parents=True, exist_ok=True)
    values = {
        "backup": format_report_path(config.backup_directory / "originals"),
        "icloud": format_report_path(config.icloud_directory),
        "manifest": format_report_path(_native_manifest_path(config)),
        "results": format_report_path(_native_results_path(config)),
        "settle_seconds": str(config.icloud_delete_settle_seconds),
        "attempts": str(config.icloud_delete_attempts),
    }
    escaped = {key: value.replace("'", "''") for key, value in values.items()}
    script = f'''# Generated by replace_icloud_photos.py. Run with native Windows PowerShell, not WSL.
[CmdletBinding()]
param([switch]$WhatIf, [switch]$SkipBackup, [switch]$SkipICloudHashVerification)

$ErrorActionPreference = 'Stop'
$iCloudDirectory = '{escaped["icloud"]}'
$BackupDirectory = '{escaped["backup"]}'
$ManifestPath = '{escaped["manifest"]}'
$ResultsPath = '{escaped["results"]}'
$SettleSeconds = {escaped["settle_seconds"]}
$DeleteAttempts = {escaped["attempts"]}

function Get-Sha256([string]$Path) {{
    return (Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash.ToLowerInvariant()
}}

function Add-Result([System.Collections.ArrayList]$Results, [object]$Record) {{
    [void]$Results.Add($Record)
    $Results | Export-Csv -LiteralPath $ResultsPath -NoTypeInformation -Encoding utf8
}}

if (-not (Test-Path -LiteralPath $ManifestPath -PathType Leaf)) {{ throw "Manifest not found: $ManifestPath" }}
New-Item -ItemType Directory -Force -Path $BackupDirectory | Out-Null
New-Item -ItemType Directory -Force -Path (Split-Path -Parent $ResultsPath) | Out-Null
$rows = Import-Csv -LiteralPath $ManifestPath | Where-Object {{ $_.status -in @('prepared', 'prepared_existing') }}
$results = [System.Collections.ArrayList]::new()
foreach ($row in $rows) {{
    $originalPath = Join-Path $iCloudDirectory $row.original_filename
    $replacementPath = Join-Path $iCloudDirectory ([System.IO.Path]::GetFileName($row.prepared_path))
    $backupPath = Join-Path $BackupDirectory $row.original_filename
    $status = 'failed'; $detail = ''
    try {{
        if (-not (Test-Path -LiteralPath $row.prepared_path -PathType Leaf)) {{ throw "Prepared JPEG missing: $($row.prepared_path)" }}
        if ((Get-Sha256 $row.prepared_path) -ne $row.prepared_sha256.ToLowerInvariant()) {{ throw 'Prepared JPEG hash changed after preparation.' }}
        if ($WhatIf) {{
            $status = 'would_replace'
        }} else {{
            if ((Test-Path -LiteralPath $originalPath -PathType Leaf) -and -not $SkipBackup) {{
                $originalHash = Get-Sha256 $originalPath
                if (Test-Path -LiteralPath $backupPath -PathType Leaf) {{
                    if ((Get-Sha256 $backupPath) -ne $originalHash) {{ throw "Backup conflict: $backupPath" }}
                }} else {{
                    Copy-Item -LiteralPath $originalPath -Destination $backupPath -ErrorAction Stop
                    if ((Get-Sha256 $backupPath) -ne $originalHash) {{ throw "Backup verification failed: $backupPath" }}
                }}
            }}
            if (Test-Path -LiteralPath $replacementPath -PathType Leaf) {{
                if ($SkipICloudHashVerification) {{
                    $detail = 'Existing restored target was not hash-verified by operator request.'
                }} elseif ((Get-Sha256 $replacementPath) -ne $row.prepared_sha256.ToLowerInvariant()) {{
                    throw "Replacement conflict: $replacementPath"
                }}
            }} else {{
                Copy-Item -LiteralPath $row.prepared_path -Destination $replacementPath -ErrorAction Stop
                if ($SkipICloudHashVerification) {{
                    $detail = 'Copied restored target was not hash-verified by operator request.'
                }} elseif ((Get-Sha256 $replacementPath) -ne $row.prepared_sha256.ToLowerInvariant()) {{
                    throw "Replacement verification failed: $replacementPath"
                }}
            }}
            if (-not (Test-Path -LiteralPath $originalPath -PathType Leaf)) {{
                $status = 'already_replaced'
            }} else {{
                for ($attempt = 1; $attempt -le $DeleteAttempts; $attempt++) {{
                    Remove-Item -LiteralPath $originalPath -Force -ErrorAction Stop
                    Start-Sleep -Seconds $SettleSeconds
                    if (-not (Test-Path -LiteralPath $originalPath -PathType Leaf)) {{ break }}
                }}
                if (Test-Path -LiteralPath $originalPath -PathType Leaf) {{ throw "iCloud recreated the original after $DeleteAttempts deletion attempt(s)." }}
                $status = 'replaced'
            }}
        }}
    }} catch {{
        $detail = $_.Exception.Message
    }}
    Add-Result $results ([PSCustomObject]@{{
        original_path = $originalPath; replacement_path = $replacementPath; replacement_sha256 = $row.prepared_sha256
        status = $status; detail = $detail
    }})
    Write-Host "$status : $($row.original_filename)"
}}
Write-Host "Completed. Results: $ResultsPath"
'''
    temporary = script_path.parent / f".{script_path.name}.{uuid4().hex}.tmp"
    try:
        temporary.write_text(script, encoding="utf-8")
        os.replace(temporary, script_path)
    finally:
        temporary.unlink(missing_ok=True)


def _execute_replacement(
    config: ICloudReplacementConfig,
    update: Path,
    original: Path,
    target: Path,
    backup: Path,
) -> ReplacementRecord:
    """Perform one backup, verified copy, and exact-original deletion."""
    update_hash = _sha256(update)
    staging_directory = config.backup_directory / ".staging"
    staged = staging_directory / f"{target.stem}.{uuid4().hex}.jpg"
    try:
        staging_directory.mkdir(parents=True, exist_ok=True)
        _render_replacement_jpeg(update, staged, config.replacement_date)
        replacement_hash = _sha256(staged)
        _verify_replacement(staged, replacement_hash, config.replacement_date)
        original_hash = _sha256(original)
        if backup.exists():
            if not backup.is_file() or _sha256(backup) != original_hash:
                return ReplacementRecord(update, original, target, backup, "backup_conflict", "Existing backup differs from the original; original was retained.", update_hash, replacement_hash)
        else:
            backup.parent.mkdir(parents=True, exist_ok=True)
            _copy_atomically(original, backup, original_hash)
        if target.exists():
            if not target.is_file() or _sha256(target) != replacement_hash:
                return ReplacementRecord(update, original, target, backup, "replacement_target_conflict", "Existing restored JPEG differs from this update.", update_hash, replacement_hash)
        else:
            _copy_atomically(staged, target, replacement_hash)
        _verify_replacement(target, replacement_hash, config.replacement_date)
        _delete_and_confirm(original, config.icloud_delete_settle_seconds, config.icloud_delete_attempts)
    except ICloudDeletionNotPersistent as error:
        return ReplacementRecord(update, original, target, backup, "delete_not_persistent", str(error), update_hash, replacement_hash)
    except (OSError, ValueError) as error:
        return ReplacementRecord(update, original, target, backup, "replacement_failed", str(error), update_hash)
    finally:
        staged.unlink(missing_ok=True)
    return ReplacementRecord(update, original, target, backup, "replaced", update_sha256=update_hash, replacement_sha256=replacement_hash)


class ICloudDeletionNotPersistent(ICloudReplacementError):
    """Raised when iCloud recreates an original after local deletion attempts."""


def _delete_and_confirm(path: Path, settle_seconds: float, attempts: int) -> None:
    """Delete an original and confirm it remains absent after iCloud settles."""
    for attempt in range(1, attempts + 1):
        if path.exists():
            path.unlink()
        if settle_seconds:
            time.sleep(settle_seconds)
        if not path.exists():
            return
    raise ICloudDeletionNotPersistent(
        f"iCloud restored the original after {attempts} deletion attempt(s); it was retained for a later retry."
    )


def _render_replacement_jpeg(update: Path, destination: Path, replacement_date: date) -> None:
    """Render a new JPEG with fixed EXIF dates while preserving source pixels."""
    with Image.open(update) as image:
        if image.mode == "RGBA":
            background = Image.new("RGB", image.size, "white")
            background.paste(image, mask=image.getchannel("A"))
            image = background
        elif image.mode != "RGB":
            image = image.convert("RGB")
        exif = image.getexif()
        exif_datetime = replacement_date.strftime(EXIF_DATETIME_FORMAT)
        exif[274] = 1
        exif[306] = exif_datetime
        exif[36867] = exif_datetime
        exif[36868] = exif_datetime
        image.save(destination, format="JPEG", quality=JPEG_QUALITY, optimize=True, exif=exif.tobytes())


def _verify_replacement(path: Path, expected_hash: str, replacement_date: date) -> None:
    if _sha256(path) != expected_hash:
        raise ICloudReplacementError("Copied replacement hash does not match its verified staged JPEG.")
    expected_datetime = replacement_date.strftime(EXIF_DATETIME_FORMAT)
    with Image.open(path) as image:
        image.load()
        if image.format != "JPEG":
            raise ICloudReplacementError("Replacement is not a JPEG.")
        exif = image.getexif()
        for tag in (306, 36867, 36868):
            if exif.get(tag) != expected_datetime:
                raise ICloudReplacementError("Replacement EXIF date verification failed.")


def _copy_atomically(source: Path, destination: Path, expected_hash: str) -> None:
    """Copy one verified file to a new path without overwriting an existing file."""
    if destination.exists():
        raise ICloudReplacementError(f"Destination already exists: {format_report_path(destination)}")
    temporary = destination.parent / f".{destination.name}.{uuid4().hex}.tmp"
    try:
        shutil.copyfile(source, temporary)
        if _sha256(temporary) != expected_hash:
            raise ICloudReplacementError("Temporary copy hash verification failed.")
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)


def _original_index(directory: Path) -> dict[str, list[Path]]:
    index: dict[str, list[Path]] = {}
    for path in sorted(directory.iterdir(), key=lambda item: item.name.casefold()):
        if path.is_file() and path.suffix.casefold() in ORIGINAL_EXTENSIONS:
            index.setdefault(path.stem.casefold(), []).append(path)
    return index


def _update_files(directory: Path) -> list[Path]:
    return sorted((path for path in directory.iterdir() if path.is_file() and path.suffix.casefold() in SUPPORTED_UPDATE_EXTENSIONS), key=lambda path: path.name.casefold())


def _replacement_base(path: Path) -> str | None:
    match = RESTORED_NAME_PATTERN.fullmatch(path.stem)
    return match.group("base") if match is not None else None


def _required_path(data: dict[object, object], key: str, base: Path) -> Path:
    value = data.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ICloudReplacementError(f"Configuration requires a non-empty {key}.")
    path = normalize_path(value)
    return (path if path.is_absolute() else base / path).resolve(strict=False)


def _optional_path(data: object, key: str, base: Path) -> Path | None:
    """Read an optional non-empty configuration path."""
    if data is None:
        return None
    if not isinstance(data, str) or not data.strip():
        raise ICloudReplacementError(f"{key} must be a non-empty path string or null.")
    path = normalize_path(data)
    return (path if path.is_absolute() else base / path).resolve(strict=False)


def _nonnegative_number(value: object, key: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
        raise ICloudReplacementError(f"{key} must be zero or a positive number.")
    return float(value)


def _positive_integer(value: object, key: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ICloudReplacementError(f"{key} must be a positive integer.")
    return value


def _validate_config(config: ICloudReplacementConfig) -> None:
    if not config.updates_directory.is_dir():
        raise ICloudReplacementError(f"Updates directory does not exist: {format_report_path(config.updates_directory)}")
    if _is_within(config.backup_directory, config.icloud_directory) or _is_within(config.report_path, config.icloud_directory):
        raise ICloudReplacementError("Backup and report paths must be outside the iCloud Photos directory.")
    if _is_within(config.updates_directory, config.icloud_directory):
        raise ICloudReplacementError("Updates directory must not be inside the iCloud Photos directory.")
    if config.superseded_sources_report is not None and _is_within(config.superseded_sources_report, config.icloud_directory):
        raise ICloudReplacementError("Superseded-source ledger must be outside the iCloud Photos directory.")
    native_paths = (
        _prepared_directory(config), _native_manifest_path(config), _native_script_path(config), _native_results_path(config),
    )
    if any(_is_within(path, config.icloud_directory) for path in native_paths):
        raise ICloudReplacementError("Native Windows preparation artifacts must be outside the iCloud Photos directory.")


def _is_within(path: Path, directory: Path) -> bool:
    try:
        path.resolve(strict=False).relative_to(directory.resolve(strict=False))
    except ValueError:
        return False
    return True


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _write_report(result: ICloudReplacementResult) -> None:
    result.config.report_path.parent.mkdir(parents=True, exist_ok=True)
    with result.config.report_path.open("w", newline="", encoding="utf-8") as file:
        writer = csv.writer(file)
        writer.writerow(["update_path", "original_path", "replacement_path", "backup_path", "status", "detail", "update_sha256", "replacement_sha256"])
        for record in result.records:
            writer.writerow([
                str(record.update_path),
                "" if record.original_path is None else str(record.original_path),
                "" if record.replacement_path is None else str(record.replacement_path),
                "" if record.backup_path is None else str(record.backup_path),
                record.status,
                record.detail,
                record.update_sha256,
                record.replacement_sha256,
            ])


def _update_superseded_sources_ledger(result: ICloudReplacementResult) -> None:
    """Persist originals whose restored JPEG was verified before any delete race."""
    _merge_superseded_sources_ledger(result.config, result.records)


def _merge_superseded_sources_ledger(
    config: ICloudReplacementConfig, records: list[ReplacementRecord]
) -> int:
    """Atomically merge verified restored replacements into the durable ledger."""
    ledger_path = config.superseded_sources_report
    if ledger_path is None:
        return 0
    entries: dict[Path, tuple[Path, str]] = {}
    if ledger_path.exists():
        with ledger_path.open(newline="", encoding="utf-8-sig") as file:
            reader = csv.DictReader(file)
            if reader.fieldnames is None or set(SUPERSEDED_LEDGER_HEADERS) != set(reader.fieldnames):
                raise ICloudReplacementError("Existing superseded-source ledger has an unexpected schema.")
            for row in reader:
                if row["status"] != "superseded":
                    raise ICloudReplacementError("Existing superseded-source ledger contains an unrecognized status.")
                original = normalize_path(row["original_path"]).resolve(strict=False)
                replacement = normalize_path(row["replacement_path"]).resolve(strict=False)
                entries[original] = (replacement, row["replacement_sha256"])
    for record in records:
        if record.status not in LEDGER_ELIGIBLE_STATUSES:
            continue
        if record.original_path is None or record.replacement_path is None or not record.replacement_sha256:
            continue
        entries[record.original_path.resolve(strict=False)] = (
            record.replacement_path.resolve(strict=False), record.replacement_sha256,
        )
    ledger_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = ledger_path.parent / f".{ledger_path.name}.{uuid4().hex}.tmp"
    try:
        with temporary.open("w", newline="", encoding="utf-8") as file:
            writer = csv.writer(file)
            writer.writerow(SUPERSEDED_LEDGER_HEADERS)
            for original, (replacement, replacement_hash) in sorted(entries.items(), key=lambda item: str(item[0]).casefold()):
                writer.writerow([str(original), str(replacement), replacement_hash, "superseded"])
        os.replace(temporary, ledger_path)
    finally:
        temporary.unlink(missing_ok=True)
    return len(entries)
