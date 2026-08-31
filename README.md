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
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -e ".[dev]"
```

`pillow-heif` supplies HEIC/HEIF support. If its installation reports a
platform-specific issue, upgrade `pip` first and use a supported 64-bit Python
release.

## Usage

All machine-specific settings live in [config.yaml](config.yaml), not in the
application code. Update its `source` and `output` values for this computer.
The checked-in configuration is intentionally safe: it performs a dry run and
selects only the first 10 supported images in deterministic path order.

Run that small trial from PowerShell:

```powershell
python prepare_agptek.py
```

This produces reports under the configured output directory but does not create
or change any output images. Review `reports/summary.txt`, `manifest.csv`, and
`errors.csv` before continuing.

To create a real test output for only 10 images, change `dry_run` to `false` in
`config.yaml`, then run the same command. You can also make a one-time trial
without editing the file:

```powershell
python prepare_agptek.py --max-files 10 --no-dry-run
```

When the trial looks correct, set `max_files: null` and `dry_run: false` in
`config.yaml` to process the entire library. A full run can take time, but its
output names stay deterministic.

Command-line values override YAML for a single run. For example:

```powershell
python prepare_agptek.py --config config.yaml --max-files 25 --dry-run
python prepare_agptek.py --source "E:\Photos" --output "E:\Frame_Output" --max-files 5 --dry-run
```

To intentionally rebuild a previous output library, add
`--overwrite-output`. HEIC/HEIF conversions use the `jpeg_quality` setting
(92 by default); the command-line override is `--jpeg-quality 1..100`.

### Configuration reference

```yaml
sources:                         # required source directories; read-only
  - "D:/OneDrive/USB"
  - "C:/Users/Lindsay/Pictures"
output: "D:/OneDrive/USB_OUTPUT" # required output root
max_files: 10                    # positive integer, or null for every image
dry_run: true                    # true creates reports only
overwrite_output: false          # true permits rebuilding images/reports
jpeg_quality: 92                 # HEIC/HEIF conversion quality, 1 through 100
video_output: "D:/OneDrive/USB/Videos" # optional archive for discovered videos
```

Use `source` for one directory or `sources` for two or more directories; do not
set both. Relative source paths and `output` are resolved relative to the YAML
file. Every source is scanned recursively, and exact duplicate files are
included only once.
Unknown or invalid settings cause a clear error before any processing begins.
When run from WSL, Windows drive paths such as `D:/OneDrive/USB` are
automatically translated to `/mnt/d/OneDrive/USB`; use the Windows-style paths
in `config.yaml` on either Windows or WSL.

Within every output folder, sequential filenames are assigned in ascending
capture-date order. Equal capture timestamps use the source path as a stable
tie-breaker. `manifest.csv` maps each generated output file to its original
source file, source folder, and canonical date. Its `output_path` and
`source_path` columns use Windows paths when the application is run from WSL,
so either value can be pasted directly into Windows File Explorer.

When `video_output` is configured, MP4, MOV, M4V, AVI, MKV, and WMV files are
moved from the configured source directories to that archive. Existing archive
files are never overwritten; collision names receive a numeric suffix. Every
planned or completed video transfer appears in `reports/videos.csv`.

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

Output filenames are sequential in ascending capture-date order within each
output folder. `manifest.csv` includes the exact generated `output_path` plus
the original `source_path` and `source_folder`, so an image shown from an
output folder can be traced back to the file to edit or delete. Exact duplicates are recorded in `duplicates.csv`; recoverable read,
hash, copy, and conversion failures are recorded in `errors.csv`.
