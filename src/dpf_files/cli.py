"""Command-line interface for the AGPTEK media preparation utility."""

from __future__ import annotations

import argparse
import logging
from pathlib import Path
from typing import Sequence

from dpf_files.pipeline import PreparationConfig, SafetyError, prepare_library

DEFAULT_SOURCE = Path(r"D:\OneDrive\USB")
DEFAULT_OUTPUT = Path(r"D:\OneDrive\USB_OUTPUT")


def build_parser() -> argparse.ArgumentParser:
    """Build the command-line argument parser."""
    parser = argparse.ArgumentParser(
        description="Create a deduplicated, flat image library for an AGPTEK player."
    )
    parser.add_argument("--source", type=Path, default=DEFAULT_SOURCE, help="Source root to scan.")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT, help="Output root to create.")
    parser.add_argument(
        "--overwrite-output",
        action="store_true",
        help="Explicitly clear this utility's existing images and reports directories.",
    )
    parser.add_argument(
        "--jpeg-quality",
        type=int,
        default=92,
        help="JPEG quality for HEIC/HEIF conversions (1-100; default: 92).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Scan and write reports without creating, replacing, or deleting output images.",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Run the command-line application and return its process status."""
    args = build_parser().parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    try:
        result = prepare_library(
            PreparationConfig(
                source=args.source,
                output=args.output,
                overwrite_output=args.overwrite_output,
                jpeg_quality=args.jpeg_quality,
                dry_run=args.dry_run,
            )
        )
    except SafetyError as error:
        logging.error("Stopped safely: %s", error)
        return 2
    except ValueError as error:
        logging.error("Invalid configuration: %s", error)
        return 2

    print(result.summary_text())
    return 0
