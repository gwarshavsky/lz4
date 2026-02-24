"""
Compressible data generator test tool.

Port of tests/datagen.c from the C LZ4 project.
Generates test data with controllable compressibility for use in testing.

License: GPL-2.0-or-later (test tooling)
"""

from __future__ import annotations

import sys

# ---------------------------------------------------------------------------
# Constants (matching datagen.c)
# ---------------------------------------------------------------------------

PRIME1 = 2654435761
PRIME2 = 2246822519

LTLOG = 13
LTSIZE = 1 << LTLOG       # 8192
LTMASK = LTSIZE - 1

RDG_DICTSIZE = 32 * 1024   # 32 KB
RDG_BLOCKSIZE = 128 * 1024  # 128 KB

_MASK32 = 0xFFFFFFFF


# ---------------------------------------------------------------------------
# Random number generator (matching C implementation exactly)
# ---------------------------------------------------------------------------

def _rdg_rand(seed: list[int]) -> int:
    """Pseudo-random number generator matching the C implementation.

    *seed* is a single-element list holding the U32 seed value (mutable).
    Returns the new random U32 value.
    """
    rand32 = seed[0]
    rand32 = (rand32 * PRIME1) & _MASK32
    rand32 ^= PRIME2
    rand32 = ((rand32 << 13) | (rand32 >> 19)) & _MASK32
    seed[0] = rand32
    return rand32


# ---------------------------------------------------------------------------
# Literal distribution table
# ---------------------------------------------------------------------------

def _fill_literal_distrib(ld: float) -> bytearray:
    """Build a literal distribution table of size LTSIZE.

    *ld* controls the variety of literal characters:
    - ld <= 0: full byte range 0..255
    - ld > 0: ASCII-ish subset '(' .. '}', biased towards '0'
    """
    lt = bytearray(LTSIZE)

    if ld <= 0.0:
        first_char = 0
        last_char = 255
        character = 0
    else:
        first_char = ord('(')
        last_char = ord('}')
        character = ord('0')

    u = 0
    while u < LTSIZE:
        weight = int((LTSIZE - u) * ld) + 1
        end = min(u + weight, LTSIZE)
        while u < end:
            lt[u] = character & 0xFF
            u += 1
        character += 1
        if character > last_char:
            character = first_char

    return lt


def _rdg_gen_char(seed: list[int], lt: bytearray) -> int:
    """Generate a single character using the distribution table."""
    idx = _rdg_rand(seed) & LTMASK
    return lt[idx]


# ---------------------------------------------------------------------------
# Block generation
# ---------------------------------------------------------------------------

def _rdg_gen_block(
    buf: bytearray,
    buf_size: int,
    prefix_size: int,
    match_proba: float,
    lt: bytearray,
    seed: list[int],
) -> None:
    """Generate a block of compressible data in *buf*.

    *prefix_size* bytes at the beginning of *buf* are treated as existing
    context (dictionary) and not overwritten.
    """
    match_proba32 = int(32768 * match_proba)
    pos = prefix_size

    # Special case: match_proba >= 1.0 generates sparse (mostly zero) data
    while match_proba >= 1.0:
        size0 = _rdg_rand(seed) & 3
        size0 = 1 << (16 + size0 * 2)
        size0 += _rdg_rand(seed) & (size0 - 1)
        if buf_size < pos + size0:
            buf[pos:buf_size] = b'\x00' * (buf_size - pos)
            return
        buf[pos:pos + size0] = b'\x00' * size0
        pos += size0
        buf[pos - 1] = _rdg_gen_char(seed, lt)

    # Init
    if pos == 0:
        buf[0] = _rdg_gen_char(seed, lt)
        pos = 1

    # Generate compressible data
    while pos < buf_size:
        rand15 = (_rdg_rand(seed) >> 3) & 32767
        if rand15 < match_proba32:
            # Copy (match within 32K)
            rand_len_selector = (_rdg_rand(seed) >> 7) & 7
            if rand_len_selector != 0:
                length = (_rdg_rand(seed) & 15)
            else:
                length = (_rdg_rand(seed) & 511) + 15
            length += 4

            offset = ((_rdg_rand(seed) >> 3) & 32767) + 1
            if offset > pos:
                offset = pos
            match = pos - offset
            d = min(pos + length, buf_size)
            while pos < d:
                buf[pos] = buf[match]
                pos += 1
                match += 1
        else:
            # Literal (noise)
            rand_len_selector = (_rdg_rand(seed) >> 7) & 7
            if rand_len_selector != 0:
                length = (_rdg_rand(seed) & 15)
            else:
                length = (_rdg_rand(seed) & 511) + 15

            d = min(pos + length, buf_size)
            while pos < d:
                buf[pos] = _rdg_gen_char(seed, lt)
                pos += 1


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def generate_buffer(
    size: int,
    match_proba: float = 0.5,
    lit_proba: float = 0.0,
    seed: int = 0,
) -> bytes:
    """Generate *size* bytes of compressible test data.

    Parameters
    ----------
    size : int
        Number of bytes to generate.
    match_proba : float
        Match probability (0.0 = incompressible, 1.0 = sparse/very compressible).
    lit_proba : float
        Literal distribution parameter (0.0 = auto from match_proba).
    seed : int
        Random seed for reproducibility.

    Returns
    -------
    bytes
        Generated test data.
    """
    if size <= 0:
        return b''

    if lit_proba == 0.0:
        lit_proba = match_proba / 4.5

    lt = _fill_literal_distrib(lit_proba)
    buf = bytearray(size)
    seed_state = [seed & _MASK32]
    _rdg_gen_block(buf, size, 0, match_proba, lt, seed_state)
    return bytes(buf)


def generate_out(
    size: int,
    match_proba: float = 0.5,
    lit_proba: float = 0.0,
    seed: int = 0,
) -> None:
    """Generate *size* bytes and write to stdout (binary).

    This mirrors the streaming behavior of the C `RDG_genOut()` which
    uses a 32KB dictionary and 128KB blocks for efficient generation
    of large amounts of data.
    """
    if lit_proba == 0.0:
        lit_proba = match_proba / 4.5

    lt = _fill_literal_distrib(lit_proba)
    seed_state = [seed & _MASK32]

    buf = bytearray(RDG_DICTSIZE + RDG_BLOCKSIZE)

    # Generate dictionary portion
    _rdg_gen_block(buf, RDG_DICTSIZE, 0, match_proba, lt, seed_state)

    total = 0
    stdout = sys.stdout.buffer

    while total < size:
        _rdg_gen_block(buf, RDG_DICTSIZE + RDG_BLOCKSIZE, RDG_DICTSIZE,
                       match_proba, lt, seed_state)
        gen_size = min(RDG_BLOCKSIZE, size - total)
        stdout.write(buf[:gen_size])
        total += gen_size
        # Update dictionary: copy last portion to beginning
        buf[:RDG_DICTSIZE] = buf[RDG_BLOCKSIZE:RDG_BLOCKSIZE + RDG_DICTSIZE]


# ---------------------------------------------------------------------------
# CLI entry point (datagen command-line tool)
# ---------------------------------------------------------------------------

def main() -> None:
    """Command-line datagen tool, matching datagencli.c interface."""
    import argparse

    parser = argparse.ArgumentParser(
        prog="datagen",
        description="Generate compressible test data",
    )
    parser.add_argument(
        "-g", "--size",
        type=int,
        default=65536,
        help="Number of bytes to generate (default: 64KB)",
    )
    parser.add_argument(
        "-s", "--seed",
        type=int,
        default=0,
        help="Random seed (default: 0)",
    )
    parser.add_argument(
        "-P", "--compressibility",
        type=float,
        default=50.0,
        help="Compressibility percentage 0-100 (default: 50)",
    )
    parser.add_argument(
        "-L", "--literal",
        type=float,
        default=0.0,
        help="Literal distribution percentage 0-100 (default: auto)",
    )
    args = parser.parse_args()

    match_proba = args.compressibility / 100.0
    lit_proba = args.literal / 100.0

    generate_out(args.size, match_proba, lit_proba, args.seed)


if __name__ == "__main__":
    main()
