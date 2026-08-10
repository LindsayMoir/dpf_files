# AGPTEK media preparation utility

This Windows-first Python utility creates a clean, flat photo directory for an
AGPTEK digital picture frame. It recursively scans a source library without
changing it, removes exact byte-for-byte duplicates, converts HEIC/HEIF files
to JPEG, and writes traceability reports.

Videos and near-duplicate image matching are deliberately outside this MVP.

## Safety guarantees

- Source files are only opened for reading; the utility never deletes, moves,
  renames, edits, or overwrites them.
- The output cannot be the source directory or a parent of it.
- A non-empty `images` output folder is refused unless
  `--overwrite-output` is explicitly supplied.
- Only this utility's `images` and `reports` directories are cleared when that
  flag is used.

## Installation (Windows)

Use Python 3.10 or later. In PowerShell, from the repository folder:

```powershell
py -m venv .venv
.\.venv\Scripts\Activate.ps1
py -m pip install --upgrade pip
py -m pip install -e ".[dev]"
```

`pillow-heif` supplies HEIC/HEIF support. If its installation reports a
platform-specific issue, upgrade `pip` first and use a supported 64-bit Python
release.

## Usage

First inspect the intended work without creating, replacing, or deleting output
images:

```powershell
py prepare_agptek.py --source "D:\OneDrive\USB" --output "D:\OneDrive\USB_OUTPUT" --dry-run
```

Run the preparation:

```powershell
py prepare_agptek.py --source "D:\OneDrive\USB" --output "D:\OneDrive\USB_OUTPUT"
```

To intentionally rebuild a previous output library, add
`--overwrite-output`. HEIC/HEIF conversions use JPEG quality 92 by default;
override it with `--jpeg-quality 1..100`.

## Output

```text
USB_OUTPUT/
  images/
    000001.jpg
    000002.png
  reports/
    summary.txt
    manifest.csv
    duplicates.csv
    errors.csv
    processing.log
```

Output filenames are sequential and deterministic based on the case-insensitive
sorted source path. `manifest.csv` preserves the original filename and full
source path. Exact duplicates are recorded in `duplicates.csv`; recoverable
read, hash, copy, and conversion failures are recorded in `errors.csv`.
