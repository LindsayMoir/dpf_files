"""YAML configuration loading for the AGPTEK preparation utility."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Any, Final

import yaml

from dpf_files.pipeline import PreparationConfig

CONFIG_KEYS: Final[frozenset[str]] = frozenset(
    {"source", "output", "max_files", "dry_run", "overwrite_output", "jpeg_quality"}
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

    source = _required_path(loaded, "source", resolved_path.parent)
    output = _required_path(loaded, "output", resolved_path.parent)
    return PreparationConfig(
        source=source,
        output=output,
        max_files=_optional_positive_integer(loaded.get("max_files"), "max_files"),
        dry_run=_optional_boolean(loaded.get("dry_run", False), "dry_run"),
        overwrite_output=_optional_boolean(
            loaded.get("overwrite_output", False), "overwrite_output"
        ),
        jpeg_quality=_positive_integer(loaded.get("jpeg_quality", 92), "jpeg_quality"),
    )


def apply_overrides(config: PreparationConfig, **overrides: Any) -> PreparationConfig:
    """Apply non-``None`` command-line overrides to a loaded configuration."""
    return replace(config, **{key: value for key, value in overrides.items() if value is not None})


def _required_path(data: dict[str, Any], key: str, base_directory: Path) -> Path:
    """Read a required path and make relative paths configuration-relative."""
    value = data.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"Configuration key '{key}' must be a non-empty path string.")
    path = Path(value).expanduser()
    return path if path.is_absolute() else base_directory / path


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
