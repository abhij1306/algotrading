"""HAT order tags (HAT-BRK-100, HAT-DEC-I10, HAT-SCH-053).

A tag is 20 alphanumeric characters, the Fyers `orderTag` limit:

    "HT" + purpose letter + sequence character + 16 characters

The 16 characters are the Crockford base32 encoding (upper case) of the lowest 80 bits of the
signal's uuid7, which hold its 74 random bits (12-bit counter plus 62 random bits). The tag is
not decoded back to the full uuid: the order row is found by `hat_order.tag`.

The sequence character is `0`-`9` then `A`-`Z` (36 values): the number of earlier orders with the
same signal and purpose. A retry of one logical order reuses its tag; a new logical order never
does. A 37th order for one (signal, purpose) is refused (fail closed).

The purpose letters, the sequence character and the bit selection are an [OWNER-PENDING] choice.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass

from ..errors import HatError

__all__ = [
    "BASE32_ALPHABET",
    "FRAGMENT_BITS",
    "MAX_SEQUENCE",
    "PURPOSE_LETTERS",
    "TAG_LENGTH",
    "TAG_PATTERN",
    "TAG_PREFIX",
    "DecodedTag",
    "TagError",
    "TagExhaustedError",
    "decode_tag",
    "encode_tag",
    "is_gtt_tag",
    "is_hat_tag",
    "signal_fragment",
]

TAG_PREFIX = "HT"
TAG_LENGTH = 20
FRAGMENT_BITS = 80
FRAGMENT_CHARS = FRAGMENT_BITS // 5
BASE32_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"  # Crockford: no I, L, O, U
SEQUENCE_ALPHABET = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ"
MAX_SEQUENCE = len(SEQUENCE_ALPHABET) - 1

PURPOSE_LETTERS: dict[str, str] = {
    "entry": "E",
    "stop_day": "D",
    "stop_gtt": "G",
    "exit_partial": "P",
    "exit_full": "F",
    "flatten": "L",
}
_LETTER_PURPOSES = {letter: purpose for purpose, letter in PURPOSE_LETTERS.items()}

TAG_PATTERN = re.compile(r"^HT[EDGPFL][0-9A-Z][0-9A-HJKMNP-TV-Z]{16}$")


class TagError(HatError):
    """A tag could not be built or read."""


class TagExhaustedError(TagError):
    """A (signal, purpose) already has 36 orders: the 37th is refused (fail closed)."""


@dataclass(frozen=True)
class DecodedTag:
    purpose: str
    sequence: int
    fragment: int  # the 80-bit signal fragment


def signal_fragment(signal_id: uuid.UUID) -> int:
    """The lowest 80 bits of the signal's uuid."""
    return signal_id.int & ((1 << FRAGMENT_BITS) - 1)


def _base32(value: int) -> str:
    chars = []
    for _ in range(FRAGMENT_CHARS):
        chars.append(BASE32_ALPHABET[value & 31])
        value >>= 5
    return "".join(reversed(chars))


def encode_tag(signal_id: uuid.UUID, purpose: str, sequence: int) -> str:
    """The tag for one logical order. Raises TagError for an unknown purpose or a sequence
    outside 0-35 (TagExhaustedError from 36 up)."""
    letter = PURPOSE_LETTERS.get(purpose)
    if letter is None:
        raise TagError("unknown order purpose")
    if isinstance(sequence, bool) or not isinstance(sequence, int) or sequence < 0:
        raise TagError("sequence must be a non-negative integer")
    if sequence > MAX_SEQUENCE:
        raise TagExhaustedError("order sequence exhausted for this signal and purpose")
    return f"{TAG_PREFIX}{letter}{SEQUENCE_ALPHABET[sequence]}{_base32(signal_fragment(signal_id))}"


def is_hat_tag(tag: object) -> bool:
    """True for a string of exactly the HAT tag shape (what reconciliation considers)."""
    return isinstance(tag, str) and TAG_PATTERN.fullmatch(tag) is not None


def is_gtt_tag(tag: object) -> bool:
    """True for a HAT tag of a GTT stop (purpose `stop_gtt`)."""
    return isinstance(tag, str) and is_hat_tag(tag) and tag[2] == PURPOSE_LETTERS["stop_gtt"]


def decode_tag(tag: str) -> DecodedTag:
    """Purpose, sequence and signal fragment of a HAT tag; TagError for anything else."""
    if not is_hat_tag(tag):
        raise TagError("not a HAT order tag")
    value = 0
    for char in tag[4:]:
        value = (value << 5) | BASE32_ALPHABET.index(char)
    return DecodedTag(
        purpose=_LETTER_PURPOSES[tag[2]],
        sequence=SEQUENCE_ALPHABET.index(tag[3]),
        fragment=value,
    )
