"""YAML configuration loading for the AGPTEK preparation utility."""

from __future__ import annotations

from dataclasses import replace
import os
import platform
from pathlib import Path, PureWindowsPath
from typing import Any, Final

import yaml

from dpf_files.pipeline import PreparationConfig

CONFIG_KEYS: Final[frozenset[str]] = frozenset(
    {
        "source", "sources", "output", "max_files", "dry_run", "overwrite_output",
        "jpeg_quality", "video_output", "delete_visual_duplicates", "superseded_sources_report",
    }
)


class ConfigError(ValueError):
    """Raised when the YAML configuration is missing or invalid."""


def load_config(path: Path) -> PreparationConfig:
    """Load a complete preparation configuration from a YAML file.

    Relative source and output paths are resolved from the configuration file's
    directory, rather than from the caller's current working directory.
    """
    resolved_path = path.expanduser().resolve(strict=False)
    if not resolved_path.is_file():
        raise ConfigError(f"Configuration file does not exist: {resolved_path}")
    try:
        loaded = yaml.safe_load(resolved_path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as error:
        raise ConfigError(f"Unable to read configuration file {resolved_path}: {error}") from error
    if not isinstance(loaded, dict):
        raise ConfigError("Configuration must contain a YAML mapping.")
    unknown_keys = set(loaded) - CONFIG_KEYS
    if unknown_keys:
        formatted_keys = ", ".join(sorted(str(key) for key in unknown_keys))
        raise ConfigError(f"Unknown configuration key(s): {formatted_keys}")

    sources = _source_paths(loaded, resolved_path.parent)
    output = _required_path(loaded, "output", resolved_path.parent)
    return PreparationConfig(
        source=sources[0],
        output=output,
        additional_sources=tuple(sources[1:]),
        max_files=_optional_positive_integer(loaded.get("max_files"), "max_files"),
        dry_run=_optional_boolean(loaded.get("dry_run", False), "dry_run"),
        overwrite_output=_optional_boolean(
            loaded.get("overwrite_output", False), "overwrite_output"
        ),
        jpeg_quality=_positive_integer(loaded.get("jpeg_quality", 92), "jpeg_quality"),
        video_output=_optional_path(loaded.get("video_output"), "video_output", resolved_path.parent),
        delete_visual_duplicates=_optional_boolean(
            loaded.get("delete_visual_duplicates", True), "delete_visual_duplicates"
        ),
        superseded_sources_report=_optional_path(
            loaded.get("superseded_sources_report"), "superseded_sources_report", resolved_path.parent
        ),
    )


def apply_overrides(config: PreparationConfig, **overrides: Any) -> PreparationConfig:
    """Apply non-``None`` command-line overrides to a loaded configuration."""
    selected = {key: value for key, value in overrides.items() if value is not None}
    if "source" in selected:
        selected["additional_sources"] = ()
    return replace(config, **selected)


def _source_paths(data: dict[str, Any], base_directory: Path) -> list[Path]:
    """Read either one legacy source path or a non-empty list of source paths."""
    has_source = "source" in data
    has_sources = "sources" in data
    if has_source and has_sources:
        raise ConfigError("Use either 'source' or 'sources', not both.")
    if has_source:
        return [_required_path(data, "source", base_directory)]
    if not has_sources:
        raise ConfigError("Configuration requires 'source' or 'sources'.")
    values = data["sources"]
    if not isinstance(values, list) or not values:
        raise ConfigError("Configuration key 'sources' must be a non-empty list of paths.")
    paths: list[Path] = []
    for index, value in enumerate(values, start=1):
        if not isinstance(value, str) or not value.strip():
            raise ConfigError(f"Configuration key 'sources' item {index} must be a non-empty path string.")
        path = normalize_path(value)
        paths.append(path if path.is_absolute() else base_directory / path)
    return paths


def _required_path(data: dict[str, Any], key: str, base_directory: Path) -> Path:
    """Read a required path and make relative paths configuration-relative."""
    value = data.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"Configuration key '{key}' must be a non-empty path string.")
    path = normalize_path(value)
    return path if path.is_absolute() else base_directory / path


def _optional_path(value: Any, key: str, base_directory: Path) -> Path | None:
    """Read an optional non-empty path string from configuration data."""
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"Configuration key '{key}' must be a non-empty path string or null.")
    path = normalize_path(value)
    return path if path.is_absolute() else base_directory / path


def normalize_path(value: str | Path) -> Path:
    """Return a native path, translating Windows drive paths when run in WSL."""
    raw_value = str(value)
    windows_path = PureWindowsPath(raw_value)
    if _is_wsl() and windows_path.drive and windows_path.root:
        drive = windows_path.drive.removesuffix(":").lower()
        return Path("/mnt", drive, *windows_path.parts[1:])
    return Path(raw_value).expanduser()


def _is_wsl() -> bool:
    """Return whether this process is running under Windows Subsystem for Linux."""
    return bool(os.environ.get("WSL_DISTRO_NAME")) or "microsoft" in platform.release().casefold()


def _optional_positive_integer(value: Any, key: str) -> int | None:
    """Validate an optional positive integer YAML setting."""
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ConfigError(f"Configuration key '{key}' must be a positive integer or null.")
    return value


def _positive_integer(value: Any, key: str) -> int:
    """Validate a required positive integer YAML setting."""
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ConfigError(f"Configuration key '{key}' must be a positive integer.")
    return value


def _optional_boolean(value: Any, key: str) -> bool:
    """Validate a YAML boolean setting."""
    if not isinstance(value, bool):
        raise ConfigError(f"Configuration key '{key}' must be true or false.")
    return value
