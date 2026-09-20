"""Text layout helpers for Telegram messages."""

from __future__ import annotations

from collections.abc import Iterable

# Approximate advance widths (per 1000 em) in Roboto, Telegram's Android font. Other
# platforms use different fonts but keep the same proportions, which is all alignment
# needs. Anything not listed (accents, Cyrillic, symbols) falls back to a mid width.
_GLYPH_WIDTHS: dict[str, int] = {
    " ": 247, ":": 251, ".": 268, ",": 200, "-": 266, "/": 400, "(": 340, ")": 340,
    "&": 630, "'": 130, "%": 730, "#": 640,
    "A": 651, "B": 651, "C": 655, "D": 656, "E": 570, "F": 553, "G": 683, "H": 719,
    "I": 273, "J": 544, "K": 650, "L": 538, "M": 875, "N": 719, "O": 687, "P": 639,
    "Q": 687, "R": 644, "S": 601, "T": 613, "U": 655, "V": 638, "W": 887, "X": 641,
    "Y": 611, "Z": 611,
    "a": 544, "b": 561, "c": 523, "d": 564, "e": 530, "f": 348, "g": 561, "h": 551,
    "i": 243, "j": 239, "k": 507, "l": 243, "m": 876, "n": 552, "o": 570, "p": 561,
    "q": 568, "r": 338, "s": 516, "t": 327, "u": 551, "v": 493, "w": 762, "x": 498,
    "y": 479, "z": 501,
    "0": 561, "1": 561, "2": 561, "3": 561, "4": 561, "5": 561, "6": 561, "7": 561,
    "8": 561, "9": 561,
}  # fmt: skip
_DEFAULT_GLYPH_WIDTH = 540
_SPACE_WIDTH = _GLYPH_WIDTHS[" "]


def text_width(text: str) -> int:
    """Estimated rendered width of *text*, in 1/1000 em."""
    return sum(_GLYPH_WIDTHS.get(ch, _DEFAULT_GLYPH_WIDTH) for ch in text)


def align_fields(fields: Iterable[tuple[str, str]], *, gap: int = 2) -> list[str]:
    """Render ``label: value`` rows with the values starting at the same x-position.

    Telegram's chat font is proportional, so padding every label to the same number of
    *characters* leaves wide labels ("First reg:") sticking out past narrow ones
    ("VIN:"). Instead, each label gets as many spaces as it takes to reach the column
    of the widest label plus *gap* spaces — accurate to within about half a space.

    Only labels are measured, so values may carry HTML (``<code>…</code>``) and any
    length. Labels must be plain text; escape them first if they are not constants.
    """
    rows = list(fields)
    labels = [f"{label}:" for label, _ in rows]
    column = max(map(text_width, labels), default=0) + gap * _SPACE_WIDTH
    return [
        f"{label}{' ' * max(1, round((column - text_width(label)) / _SPACE_WIDTH))}{value}"
        for label, (_, value) in zip(labels, rows, strict=True)
    ]
