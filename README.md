# AGPTEK media preparation utility

This Windows-first Python utility creates a clean, flat photo directory for an
AGPTEK digital picture frame. It recursively scans a source library without
changing it, removes exact and conservative visual duplicates, converts HEIC/HEIF files
to JPEG, physically normalizes EXIF orientation for frame-compatible output, and
writes traceability reports.

Videos are deliberately outside this MVP.

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

If the date-selection rules are updated after a successful rebuild, regroup the
already-generated USB files without hashing, converting, or copying from
iCloud again:

```powershell
python prepare_agptek.py --reshuffle-dates
```

This command validates that every file under `photos` is represented in the
manifest, stages the existing USB files into a verified replacement layout, and
then updates `manifest.csv` with the corrected folder, filename, capture date,
and date source. It does not include files deferred by an earlier full rebuild.

Visual duplicates are deleted from the configured iCloud Photos source by
default after the pipeline identifies them, so they cannot return to iCloud or
the USB output on a later run. Each decision is recorded in
`duplicates.csv` through its `source_action` column. To delete visual
duplicates found by an already-completed run, use:

```powershell
python prepare_agptek.py --delete-reported-visual-duplicates
```

This command writes `reports/visual_duplicates_deleted.csv`. Set
`delete_visual_duplicates: false` only when a normal rebuild should report,
rather than delete, future visual matches.

### Configuration reference

```yaml
source: "C:/Users/Lindsay/Pictures/iCloud Photos/Photos" # authoritative, read-only photo library
output: "D:/OneDrive/USB_OUTPUT" # required output root
max_files: 10                    # positive integer, or null for every image
dry_run: true                    # true creates reports only
overwrite_output: false          # true permits rebuilding images/reports
delete_visual_duplicates: true   # delete detected visual duplicates from source
superseded_sources_report: null  # optional verified-restoration ledger; listed originals are excluded
jpeg_quality: 92                 # HEIC/HEIF conversion quality, 1 through 100
video_output: null               # leave unset so videos are never moved
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
tie-breaker. The date selector prioritizes EXIF capture metadata, then
machine-readable and human-readable filename dates, then unambiguous folder
dates, and uses filesystem time only as a final fallback. `manifest.csv` maps
each generated output file to its original source file, source folder, and
selected date in both `canonical_date` and `capture_date`, with `date_source`
recording the decision. Its `output_path` and `source_path` columns use Windows
paths when the application is run from WSL, so either value can be pasted
directly into Windows File Explorer.

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
output folder can be traced back to the file to edit or delete. Exact and
visual duplicates are recorded in `duplicates.csv`, including a
`duplicate_type` column; recoverable read, hash, copy, and conversion failures
are recorded in `errors.csv`.

## Importing originals into iCloud Photos

Do not copy the numbered files under `USB_OUTPUT/photos` into iCloud. They are
frame playback files, and HEIC originals may have been converted to JPEG. The
manifest instead identifies every original source photo and its SHA-256 hash.

First create an audit-only plan (this is the default):

```powershell
python import_icloud_photos.py `
  --manifest "D:\OneDrive\USB_OUTPUT\reports\manifest.csv" `
  --destination "C:\Users\Lindsay\Pictures\iCloud Photos\Photos"
```

Review `D:\OneDrive\USB_OUTPUT\reports\icloud_import.csv`. It lists every
planned copy, exact-content duplicate, missing source, and filename collision.
When it looks correct, run the same command with `--execute` to copy the
missing original photos:

```powershell
python import_icloud_photos.py `
  --manifest "D:\OneDrive\USB_OUTPUT\reports\manifest.csv" `
  --destination "C:\Users\Lindsay\Pictures\iCloud Photos\Photos" `
  --execute
```

The command hashes iCloud destination files before importing, never overwrites
an existing file, and verifies each source against its manifest hash immediately
before copying. It preserves original filenames where possible, and adds a
stable hash suffix only for a different photo with the same name. It excludes
videos because the preparation manifest contains image records only. It is
safe to rerun: already imported content is reported as a duplicate rather than
copied again.

## Replacing iCloud photos with manually restored versions

Use `replace_icloud_photos.py` only for completed manual/AI restorations named
`<original-stem>_restored.<extension>` in the configured update directory.
Use the native-Windows workflow below: it prepares each update as a verified
JPEG with `1955:04:27 12:00:00` in its three EXIF date fields, then generates a
single PowerShell executor. This avoids accessing iCloud from WSL.

```powershell
python replace_icloud_photos.py --config icloud_replacements.yaml --prepare-native-windows
```

This only reads the update directory. It creates:

- `ready_for_icloud\` — verified JPEGs ready to upload;
- `reports\native_windows_replacements.csv` — the exact old-to-new mapping;
- `reports\apply_icloud_replacements.ps1` — the native Windows executor.

Open **PowerShell** (not WSL) and first run its non-destructive preview:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "D:\OneDrive\USB\Family Photos Updated\reports\apply_icloud_replacements.ps1" -WhatIf
```

When the preview is correct, run the same command without `-WhatIf`:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "D:\OneDrive\USB\Family Photos Updated\reports\apply_icloud_replacements.ps1"
```

The script runs natively on Windows, backs up each existing original outside
iCloud, verifies the prepared JPEG and its iCloud copy by SHA-256, deletes only
the exact mapped original, then waits 30 seconds and retries up to three times
if iCloud recreates it. It writes progress after every file to
`reports\native_windows_replacement_results.csv`.

If the originals are already safely backed up elsewhere and iCloud is blocking
on an on-demand download, add `-SkipBackup`. This avoids reading or downloading
the original before deletion; it still verifies the prepared replacement and
uses the exact mapped original filename:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "D:\OneDrive\USB\Family Photos Updated\reports\apply_icloud_replacements.ps1" -SkipBackup
```

If iCloud on-demand files are stalling hash reads, add
`-SkipICloudHashVerification` as well. The prepared JPEG is still verified
offline; the results CSV records that the iCloud-side hash check was skipped:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "D:\OneDrive\USB\Family Photos Updated\reports\apply_icloud_replacements.ps1" -SkipBackup -SkipICloudHashVerification
```

After the native run is complete, update the USB exclusion ledger without
touching iCloud:

```powershell
python replace_icloud_photos.py --config icloud_replacements.yaml --seed-native-windows-results
```

The older `--execute` mode remains for compatibility, but do not use it from
WSL against the iCloud Photos folder.

Every executed replacement also maintains the configured
`icloud_superseded_sources.csv` ledger. The normal USB build reads that ledger
before it fingerprints source files: an original with a verified restored JPEG
is excluded even when iCloud for Windows rehydrates the old original locally.
The build writes its applied decisions to
`USB_OUTPUT/reports/superseded_sources_excluded.csv`. This protects USB output
from iCloud cache races without asserting that the cloud deletion succeeded.
For replacements made before the ledger was enabled, seed it from the completed
replacement report without opening or changing any iCloud photo files:

```powershell
python replace_icloud_photos.py --config icloud_replacements.yaml --seed-superseded-ledger
```

### Auditing visually matching photos

SHA-256 detects only byte-identical files. To audit likely duplicates that were
re-encoded, resized, or saved in another image format, run the normal dry-run
command with `--visual-dedup`:

```powershell
python import_icloud_photos.py `
  --manifest "D:\OneDrive\USB_OUTPUT\reports\manifest.csv" `
  --destination "C:\Users\Lindsay\Pictures\iCloud Photos\Photos" `
  --visual-dedup `
  --visual-audit-only
```

This decodes supported images, normalizes their orientation, and compares a
small perceptual signature. It writes
`possible_visual_duplicates.csv` beside `icloud_import.csv`, including the
source and destination paths plus the Hamming distance (lower is more similar).
It also writes `possible_visual_duplicates.html`, with paired source and
destination thumbnails for every proposed match. The audit also scans the
destination library against itself, so it finds possible duplicates already in
iCloud. `--visual-audit-only` avoids the normal SHA-256 import inventory and
does not replace `icloud_import.csv`, making it the preferred option for an
existing iCloud-library cleanup. The feature is audit-only: it never skips a
planned copy or deletes anything. Review the report before making any cleanup
decisions; similar photos and edits can be reported as possible matches.

Every `--execute` iCloud import automatically applies the same visual check and
records `duplicate_visual_content` instead of copying a source that matches an
existing iCloud photo. This prevents cleaned duplicates from returning in
subsequent standard imports.

To remove the newest file in every audited visual-duplicate group, use the
explicit cleanup command. It deletes from the configured iCloud Photos folder
and writes `visual_duplicate_cleanup.csv` beside the visual report:

```powershell
python import_icloud_photos.py `
  --manifest "D:\OneDrive\USB_OUTPUT\reports\manifest.csv" `
  --destination "C:\Users\Lindsay\Pictures\iCloud Photos\Photos" `
  --delete-visual-duplicates
```
