"""CLI for safe, explicit replacement of iCloud photo originals."""

from __future__ import annotations

import argparse
import logging
from pathlib import Path
from typing import Sequence

from dpf_files.icloud_replacements import (
    ICloudReplacementError,
    load_icloud_replacement_config,
    prepare_native_windows_replacements,
    replace_icloud_photos,
    seed_native_windows_results,
    seed_superseded_sources_ledger,
)


def main(argv: Sequence[str] | None = None) -> int:
    """Plan replacements by default; require ``--execute`` for live changes."""
    parser = argparse.ArgumentParser(description="Replace named iCloud originals with verified restored JPEGs.")
    parser.add_argument("--config", type=Path, default=Path("icloud_replacements.yaml"))
    parser.add_argument("--execute", action="store_true", help="Copy replacements and delete verified matching iCloud originals.")
    parser.add_argument(
        "--seed-superseded-ledger",
        action="store_true",
        help="Create or extend the durable USB exclusion ledger from the completed replacement report.",
    )
    parser.add_argument(
        "--prepare-native-windows",
        action="store_true",
        help="Prepare JPEGs and generate a native Windows PowerShell iCloud executor without accessing iCloud files.",
    )
    parser.add_argument(
        "--seed-native-windows-results",
        action="store_true",
        help="Add verified successful native Windows executor results to the USB exclusion ledger.",
    )
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    try:
        config = load_icloud_replacement_config(args.config)
        operations = sum((args.execute, args.seed_superseded_ledger, args.prepare_native_windows, args.seed_native_windows_results))
        if operations > 1:
            raise ValueError("Use only one of --execute, --seed-superseded-ledger, --prepare-native-windows, or --seed-native-windows-results.")
        if args.seed_superseded_ledger:
            entries = seed_superseded_sources_ledger(config, config.report_path)
            print(f"Superseded originals recorded: {entries}")
            print(f"Ledger: {config.superseded_sources_report}")
            return 0
        if args.prepare_native_windows:
            print(prepare_native_windows_replacements(config).summary_text())
            return 0
        if args.seed_native_windows_results:
            entries = seed_native_windows_results(config)
            print(f"Superseded originals recorded: {entries}")
            print(f"Ledger: {config.superseded_sources_report}")
            return 0
        result = replace_icloud_photos(config, dry_run=not args.execute)
    except (ICloudReplacementError, OSError, ValueError) as error:
        logging.error("Stopped safely: %s", error)
        return 2
    print(result.summary_text())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
