"""Command-line interface for importing manifest-backed photos into iCloud."""

from __future__ import annotations

import argparse
import logging
from pathlib import Path
from typing import Sequence

from dpf_files.icloud_import import (
    ICloudImportError,
    delete_visual_duplicates,
    import_manifest_to_icloud,
)


def build_parser() -> argparse.ArgumentParser:
    """Build the iCloud import command-line parser."""
    parser = argparse.ArgumentParser(
        description="Import original photos from an AGPTEK manifest into an iCloud Photos folder."
    )
    parser.add_argument("--manifest", required=True, type=Path, help="Path to reports/manifest.csv.")
    parser.add_argument("--destination", required=True, type=Path, help="iCloud Photos folder to receive originals.")
    parser.add_argument("--report", type=Path, help="CSV audit report path (default: beside manifest).")
    parser.add_argument(
        "--execute",
        action="store_true",
        help="Copy missing photos. Without this flag, only a dry-run report is written.",
    )
    parser.add_argument(
        "--visual-dedup",
        action="store_true",
        help=(
            "Audit possible visual duplicates and write possible_visual_duplicates.csv. "
            "In execute mode, matching sources are skipped but no existing files are deleted."
        ),
    )
    parser.add_argument(
        "--visual-audit-only",
        action="store_true",
        help="Scan the destination library only, without hashing import sources or replacing icloud_import.csv.",
    )
    parser.add_argument(
        "--delete-visual-duplicates",
        action="store_true",
        help="Delete the newest file in every visual-duplicate group from the iCloud destination.",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Run the iCloud import command and return its process status."""
    args = build_parser().parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    try:
        if args.delete_visual_duplicates:
            visual_report = args.report or args.manifest.parent / "possible_visual_duplicates.csv"
            records = delete_visual_duplicates(visual_report, args.destination)
            deleted = sum(record.status == "deleted_newest_visual_duplicate" for record in records)
            print(f"Deleted newest visual duplicates: {deleted}")
            print(f"Cleanup report: {visual_report.parent / 'visual_duplicate_cleanup.csv'}")
            return 0
        result = import_manifest_to_icloud(
            args.manifest,
            args.destination,
            dry_run=not args.execute,
            report_path=args.report,
            visual_dedup=args.visual_dedup or args.execute,
            visual_audit_only=args.visual_audit_only,
        )
    except (ICloudImportError, OSError, ValueError) as error:
        logging.error("Stopped safely: %s", error)
        return 2
    print(result.summary_text())
    return 0
