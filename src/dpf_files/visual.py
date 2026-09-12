"""Shared, conservative visual signatures for image deduplication."""

from __future__ import annotations

from itertools import combinations
from pathlib import Path
from typing import Final, Iterable

from PIL import Image, ImageOps
from pillow_heif import register_heif_opener

VISUAL_HASH_WIDTH: Final[int] = 9
VISUAL_HASH_HEIGHT: Final[int] = 8
VISUAL_HASH_MAX_DISTANCE: Final[int] = 2
VISUAL_DHASH_BITS: Final[int] = 40


def visual_signature(path: Path) -> int:
    """Return a 64-bit difference hash of an orientation-normalized image."""
    register_heif_opener()
    with Image.open(path) as image:
        oriented = ImageOps.exif_transpose(image)
        grayscale = oriented.convert("L")
        thumbnail = grayscale.resize((VISUAL_HASH_WIDTH, VISUAL_HASH_HEIGHT), Image.Resampling.LANCZOS)
        pixels = list(thumbnail.getdata())
        average_red, average_green, average_blue = oriented.convert("RGB").resize(
            (1, 1), Image.Resampling.LANCZOS
        ).getpixel((0, 0))

    signature = 0
    for row in range(VISUAL_HASH_HEIGHT):
        offset = row * VISUAL_HASH_WIDTH
        for column in range(VISUAL_HASH_WIDTH - 1):
            signature = (signature << 1) | int(pixels[offset + column] > pixels[offset + column + 1])
    average_color = (average_red << 16) | (average_green << 8) | average_blue
    return (average_color << VISUAL_DHASH_BITS) | (signature >> (64 - VISUAL_DHASH_BITS))


def nearest_visual_match(signature: int, signatures: dict[int, Path]) -> tuple[int, Path, int] | None:
    """Find the closest indexed dHash within the intentionally strict limit."""
    best_match: tuple[int, Path, int] | None = None
    for candidate in nearby_signatures(signature):
        destination = signatures.get(candidate)
        if destination is None:
            continue
        distance = (signature ^ candidate).bit_count()
        if best_match is None or distance < best_match[2] or (
            distance == best_match[2] and str(destination).casefold() < str(best_match[1]).casefold()
        ):
            best_match = (candidate, destination, distance)
    return best_match


def is_distinctive_signature(signature: int) -> bool:
    """Return whether a dHash has enough structure for safe visual matching."""
    return 0 <= signature < (1 << 64)


def nearby_signatures(signature: int) -> Iterable[int]:
    """Yield every 64-bit dHash within the configured Hamming-distance limit."""
    for distance in range(VISUAL_HASH_MAX_DISTANCE + 1):
        for changed_bits in combinations(range(VISUAL_DHASH_BITS), distance):
            candidate = signature
            for bit in changed_bits:
                candidate ^= 1 << bit
            yield candidate
