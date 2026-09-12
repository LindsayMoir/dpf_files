"""One-file fingerprint helper isolated from stalled cloud-file reads."""

from __future__ import annotations

import json
from pathlib import Path
import sys

from dpf_files.pipeline import _hash_file
from dpf_files.visual import visual_signature


def _fingerprint(path: Path) -> dict[str, int | str]:
    """Return serializable fingerprints or an error for one source path."""
    try:
        digest, size = _hash_file(path)
        signature = visual_signature(path)
    except Exception as error:  # Image decoders and mounted files can raise varied errors.
        return {"error": f"{type(error).__name__}: {error}"}
    return {"sha256": digest, "size": size, "signature": signature}


def main(argv: list[str] | None = None) -> int:
    """Write one source image's exact and visual fingerprints as JSON."""
    arguments = sys.argv[1:] if argv is None else argv
    if arguments == ["--serve"]:
        for raw_path in sys.stdin:
            print(json.dumps(_fingerprint(Path(raw_path.rstrip("\n")))), flush=True)
        return 0
    if len(arguments) != 1:
        return 2
    payload = _fingerprint(Path(arguments[0]))
    print(json.dumps(payload))
    return 1 if "error" in payload else 0


if __name__ == "__main__":
    raise SystemExit(main())
