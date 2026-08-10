"""Command-line interface for the AGPTEK media preparation utility."""

from __future__ import annotations

import argparse
import logging
from pathlib import Path
from typing import Sequence

from dpf_files.config import ConfigError, apply_overrides, load_config
from dpf_files.pipeline import SafetyError, prepare_library

DEFAULT_CONFIG_PATH = Path("config.yaml")


def build_parser() -> argparse.ArgumentParser:
    """Build the command-line argument parser."""
    parser = argparse.ArgumentParser(
        description="Create a deduplicated, flat image library for an AGPTEK player."
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=DEFAULT_CONFIG_PATH,
        help="YAML configuration file (default: config.yaml).",
    )
    parser.add_argument("--source", type=Path, help="Override the configured source root.")
    parser.add_argument("--output", type=Path, help="Override the configured output root.")
    parser.add_argument(
        "--overwrite-output",
        action="store_true",
        default=None,
        help="Explicitly clear this utility's existing images and reports directories.",
    )
    parser.add_argument(
        "--jpeg-quality",
        type=int,
        help="Override JPEG quality for HEIC/HEIF conversions (1-100).",
    )
    parser.add_argument(
        "--max-files",
        type=int,
        help="Process only the first N supported images in deterministic path order.",
    )
    parser.add_argument(
        "--dry-run",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="Scan and write reports without creating, replacing, or deleting output images.",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Run the command-line application and return its process status."""
    args = build_parser().parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    try:
        config = apply_overrides(
            load_config(args.config),
            source=args.source,
            output=args.output,
            overwrite_output=args.overwrite_output,
            jpeg_quality=args.jpeg_quality,
            max_files=args.max_files,
            dry_run=args.dry_run,
        )
        result = prepare_library(config)
    except SafetyError as error:
        logging.error("Stopped safely: %s", error)
        return 2
    except (ConfigError, ValueError) as error:
        logging.error("Invalid configuration: %s", error)
        return 2

    print(result.summary_text())
    return 0
