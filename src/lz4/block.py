"""
LZ4 Block Compression/Decompression - Pure Python Implementation.

This module implements the LZ4 block format as specified in doc/lz4_Block_format.md.
It is a direct port of the C reference implementation (lib/lz4.c, lib/lz4.h).

License: BSD 2-Clause

Public API
----------
- compress(data, acceleration=1) -> bytes
- decompress(data, uncompressed_size=-1) -> bytes
- compress_bound(input_size) -> int
- LZ4StreamEncode  - block-level streaming compression
- LZ4StreamDecode  - block-level streaming decompression
"""

from __future__ import annotations

import struct
from typing import Union

from lz4 import LZ4CompressError, LZ4DecompressError

# ---------------------------------------------------------------------------
# Constants (ported from lz4.h / lz4.c)
# ---------------------------------------------------------------------------

# Version
VERSION_MAJOR = 1
VERSION_MINOR = 10
VERSION_RELEASE = 0
VERSION_NUMBER = VERSION_MAJOR * 100 * 100 + VERSION_MINOR * 100 + VERSION_RELEASE

# Memory usage
MEMORY_USAGE_MIN = 10
MEMORY_USAGE_DEFAULT = 14
MEMORY_USAGE_MAX = 20
MEMORY_USAGE = MEMORY_USAGE_DEFAULT

HASHLOG = MEMORY_USAGE - 2          # 12
HASH_SIZE_U32 = 1 << HASHLOG        # 4096
HASHTABLESIZE = 1 << MEMORY_USAGE   # 16384

# Input/output limits
LZ4_MAX_INPUT_SIZE = 0x7E000000     # 2,113,929,216 bytes

# Distance
LZ4_DISTANCE_MAX = 65535
LZ4_DISTANCE_ABSOLUTE_MAX = 65535

# Block format constants
MINMATCH = 4
WILDCOPYLENGTH = 8
LASTLITERALS = 5
MFLIMIT = 12
MATCH_SAFEGUARD_DISTANCE = (2 * WILDCOPYLENGTH) - MINMATCH  # 12

ML_BITS = 4
ML_MASK = (1 << ML_BITS) - 1       # 0x0F = 15
RUN_BITS = 8 - ML_BITS             # 4
RUN_MASK = (1 << RUN_BITS) - 1     # 0x0F = 15

# Acceleration
ACCELERATION_DEFAULT = 1
ACCELERATION_MAX = 65537

# Internal constants
_LZ4_64Klimit = (64 * 1024) + (MFLIMIT - 1)  # 65547
_LZ4_skipTrigger = 6
_LZ4_minLength = MFLIMIT + 1  # 13

# Mask for 32-bit and 16-bit integers
_MASK32 = 0xFFFFFFFF
_MASK16 = 0xFFFF

# Hash multiplier (prime from the C code)
_HASH_PRIME32 = 2654435761

# Type alias for buffer inputs
BufferType = Union[bytes, bytearray, memoryview]


# ---------------------------------------------------------------------------
# Helper functions
# ---------------------------------------------------------------------------

def compress_bound(input_size: int) -> int:
    """Return the maximum compressed size for a given input size.

    Returns 0 if *input_size* is too large.
    """
    if input_size > LZ4_MAX_INPUT_SIZE or input_size < 0:
        return 0
    return input_size + (input_size // 255) + 16


def _ensure_bytes(data: BufferType) -> bytes:
    """Ensure data is a flat bytes-like object."""
    if isinstance(data, memoryview):
        return bytes(data)
    if isinstance(data, bytearray):
        return bytes(data)
    return data


def _read16_le(buf: bytes | bytearray, pos: int) -> int:
    """Read a little-endian uint16 at *pos*."""
    return buf[pos] | (buf[pos + 1] << 8)


def _write16_le(buf: bytearray, pos: int, value: int) -> None:
    """Write a little-endian uint16 at *pos*."""
    buf[pos] = value & 0xFF
    buf[pos + 1] = (value >> 8) & 0xFF


def _read32_le(buf: bytes | bytearray, pos: int) -> int:
    """Read a little-endian uint32 at *pos*."""
    return (buf[pos]
            | (buf[pos + 1] << 8)
            | (buf[pos + 2] << 16)
            | (buf[pos + 3] << 24))


# ---------------------------------------------------------------------------
# Hash functions (ported from lz4.c)
# ---------------------------------------------------------------------------

def _hash4(sequence: int, hash_log: int) -> int:
    """4-byte hash function (LZ4_hash4).

    *sequence* is a 32-bit value, *hash_log* is the number of hash bits.
    """
    return ((sequence * _HASH_PRIME32) & _MASK32) >> (32 - hash_log)


def _hash_position(src: bytes | bytearray, pos: int, hash_log: int) -> int:
    """Hash 4 bytes starting at *pos* in *src*."""
    if pos + 4 <= len(src):
        seq = _read32_le(src, pos)
    else:
        # Shouldn't happen in normal operation, but handle gracefully
        seq = 0
        for i in range(min(4, len(src) - pos)):
            seq |= src[pos + i] << (8 * i)
    return _hash4(seq, hash_log)


# ---------------------------------------------------------------------------
# Match counting (LZ4_count equivalent)
# ---------------------------------------------------------------------------

def _count_match(src: bytes | bytearray, p1: int, p2: int, limit: int) -> int:
    """Count the number of matching bytes starting at p1 and p2 up to limit.

    Returns the number of matching bytes (starting from 0).
    """
    start = p1
    while p1 < limit and src[p1] == src[p2]:
        p1 += 1
        p2 += 1
    return p1 - start


def _count_match2(src1: bytes | bytearray, p1: int, src2: bytes | bytearray, p2: int, limit1: int, limit2: int) -> int:
    """Count matching bytes across two buffers."""
    start = p1
    while p1 < limit1 and p2 < limit2 and src1[p1] == src2[p2]:
        p1 += 1
        p2 += 1
    return p1 - start


# ---------------------------------------------------------------------------
# Compression State
# ---------------------------------------------------------------------------

# Hash table size must accommodate both byU32 (HASHLOG bits) and byU16 (HASHLOG+1 bits)
_HASH_TABLE_SIZE = 1 << (HASHLOG + 1)  # 8192 - covers both byU32 and byU16 modes


class _CompressState:
    """Internal compression state, mirroring LZ4_stream_t_internal."""

    __slots__ = (
        'hash_table', 'current_offset', 'table_type',
        'dictionary', 'dict_size', 'dict_ctx',
    )

    def __init__(self) -> None:
        self.hash_table: list[int] = [0] * _HASH_TABLE_SIZE
        self.current_offset: int = 0
        self.table_type: int = 0  # 0 = clearedTable
        self.dictionary: bytes | bytearray | None = None
        self.dict_size: int = 0
        self.dict_ctx: _CompressState | None = None

    def reset(self) -> None:
        """Full reset."""
        self.hash_table = [0] * _HASH_TABLE_SIZE
        self.current_offset = 0
        self.table_type = 0
        self.dictionary = None
        self.dict_size = 0
        self.dict_ctx = None


# Table types (from C enum)
_TABLE_CLEARED = 0
_TABLE_BY_U32 = 2
_TABLE_BY_U16 = 3

# Dict directives
_NO_DICT = 0
_WITH_PREFIX64K = 1
_USING_EXT_DICT = 2
_USING_DICT_CTX = 3

# Dict issue
_NO_DICT_ISSUE = 0
_DICT_SMALL = 1

# Output directives
_NOT_LIMITED = 0
_LIMITED_OUTPUT = 1
_FILL_OUTPUT = 2


# ---------------------------------------------------------------------------
# Core compression (LZ4_compress_generic_validated)
# ---------------------------------------------------------------------------

def _compress_generic(
    ctx: _CompressState,
    source: bytes | bytearray,
    max_output_size: int,
    output_directive: int,
    table_type: int,
    dict_directive: int,
    dict_issue: int,
    acceleration: int,
) -> tuple[bytes, int]:
    """Core compression function.

    Returns (compressed_bytes, input_consumed).
    input_consumed is only meaningful when output_directive == _FILL_OUTPUT.
    """
    src = source
    input_size = len(src)

    if input_size > LZ4_MAX_INPUT_SIZE:
        return b'', 0

    # Empty input
    if input_size == 0:
        if output_directive != _NOT_LIMITED and max_output_size <= 0:
            return b'', 0
        return bytes([0]), 0

    hash_log = HASHLOG + 1 if table_type == _TABLE_BY_U16 else HASHLOG

    start_index = ctx.current_offset
    # The hash table maps hash -> index, where index = start_index + position
    ht = ctx.hash_table

    # Dictionary setup
    dict_data: bytes | bytearray | None = None
    d_size = 0
    if dict_directive == _USING_DICT_CTX and ctx.dict_ctx is not None:
        dict_data = ctx.dict_ctx.dictionary
        d_size = ctx.dict_ctx.dict_size
    elif dict_directive in (_WITH_PREFIX64K, _USING_EXT_DICT):
        dict_data = ctx.dictionary
        d_size = ctx.dict_size

    prefix_idx_limit = start_index - d_size if d_size > 0 else start_index

    # Output buffer
    if output_directive == _NOT_LIMITED:
        out_capacity = compress_bound(input_size)
    else:
        out_capacity = max_output_size
    if out_capacity <= 0 and output_directive == _FILL_OUTPUT:
        return b'', 0

    out = bytearray(out_capacity)
    op = 0  # output position
    olimit = out_capacity

    # Update context
    if dict_directive == _USING_DICT_CTX:
        ctx.dict_ctx = None
        ctx.dict_size = input_size
    else:
        ctx.dict_size += input_size
    ctx.current_offset += input_size
    ctx.table_type = table_type

    # Pointers
    ip = 0  # current input position
    anchor = 0  # start of unmatched literals
    iend = input_size
    mflimit_plus_one = iend - MFLIMIT + 1
    matchlimit = iend - LASTLITERALS

    if input_size < _LZ4_minLength:
        # Too small for compression, go straight to last literals
        ip = iend  # skip main loop
    else:
        # Hash first byte
        h = _hash_position(src, ip, hash_log)
        ht[h] = start_index + ip
        ip += 1
        forward_h = _hash_position(src, ip, hash_log)

        # Main loop
        while True:
            # Find a match
            forward_ip = ip
            step = 1
            search_match_nb = acceleration << _LZ4_skipTrigger

            while True:
                h = forward_h
                current_idx = start_index + forward_ip
                match_index = ht[h]

                ip = forward_ip
                forward_ip += step
                step = search_match_nb >> _LZ4_skipTrigger
                search_match_nb += 1

                if forward_ip > mflimit_plus_one:
                    # No match found before end, encode remaining as literals
                    ip = iend  # force fall-through to last_literals
                    break

                ht[h] = start_index + ip
                forward_h = _hash_position(src, forward_ip, hash_log)

                # Check match validity
                if dict_directive == _NO_DICT or dict_directive == _WITH_PREFIX64K:
                    # Simple single-segment mode
                    match_pos = match_index - start_index
                    if match_pos < 0:
                        continue
                    if (dict_issue == _DICT_SMALL) and (match_index < prefix_idx_limit):
                        continue
                    if (table_type != _TABLE_BY_U16 or LZ4_DISTANCE_MAX < LZ4_DISTANCE_ABSOLUTE_MAX):
                        if match_index + LZ4_DISTANCE_MAX < current_idx:
                            continue
                    # Check 4-byte match
                    if match_pos + 3 < len(src) and ip + 3 < len(src):
                        if _read32_le(src, match_pos) == _read32_le(src, ip):
                            break  # Match found!
                    continue

                elif dict_directive == _USING_EXT_DICT:
                    if match_index < start_index:
                        # Match in external dictionary
                        if dict_data is None:
                            continue
                        dict_match_pos = match_index - (start_index - d_size)
                        if dict_match_pos < 0 or dict_match_pos + 3 >= d_size:
                            continue
                        if match_index + LZ4_DISTANCE_MAX < current_idx:
                            continue
                        if _read32_le(dict_data, dict_match_pos) == _read32_le(src, ip):
                            break  # Match found in dictionary!
                        continue
                    else:
                        match_pos = match_index - start_index
                        if match_pos + 3 >= len(src):
                            continue
                        if (dict_issue == _DICT_SMALL) and (match_index < prefix_idx_limit):
                            continue
                        if match_index + LZ4_DISTANCE_MAX < current_idx:
                            continue
                        if _read32_le(src, match_pos) == _read32_le(src, ip):
                            break  # Match found in current segment!
                        continue
                else:
                    # _USING_DICT_CTX or other - treat as simple
                    match_pos = match_index - start_index
                    if match_pos < 0:
                        continue
                    if match_index + LZ4_DISTANCE_MAX < current_idx:
                        continue
                    if match_pos + 3 < len(src) and ip + 3 < len(src):
                        if _read32_le(src, match_pos) == _read32_le(src, ip):
                            break
                    continue

            if ip >= iend:
                break  # go to last_literals

            # Determine match position and offset
            in_ext_dict = False
            if dict_directive == _USING_EXT_DICT and match_index < start_index:
                # Match is in external dictionary
                in_ext_dict = True
                match_pos = match_index - (start_index - d_size)
                offset = (start_index + ip) - match_index
            else:
                match_pos = match_index - start_index
                offset = ip - match_pos

            # Catch up: extend match backwards
            if not in_ext_dict:
                while ip > anchor and match_pos > 0 and src[ip - 1] == src[match_pos - 1]:
                    ip -= 1
                    match_pos -= 1
            else:
                # Can extend backwards into source for prefix matches only, not for extDict
                pass

            # Encode Literals
            lit_length = ip - anchor
            token_pos = op
            op += 1

            # Check output buffer overflow
            if output_directive == _LIMITED_OUTPUT:
                if op + lit_length + (2 + 1 + LASTLITERALS) + (lit_length // 255) > olimit:
                    return b'', 0
            elif output_directive == _FILL_OUTPUT:
                if op + (lit_length + 240) // 255 + lit_length + 2 + 1 + MFLIMIT - MINMATCH > olimit:
                    op = token_pos
                    ip = iend  # go to last_literals
                    break

            if lit_length >= RUN_MASK:
                out[token_pos] = RUN_MASK << ML_BITS
                remaining = lit_length - RUN_MASK
                while remaining >= 255:
                    out[op] = 255
                    op += 1
                    remaining -= 255
                out[op] = remaining
                op += 1
            else:
                out[token_pos] = lit_length << ML_BITS

            # Copy literals
            out[op:op + lit_length] = src[anchor:anchor + lit_length]
            op += lit_length

            # Encode Offset (little-endian 16-bit)
            if output_directive == _FILL_OUTPUT:
                if op + 2 + 1 + MFLIMIT - MINMATCH > olimit:
                    op = token_pos
                    ip = iend
                    break

            _write16_le(out, op, offset & 0xFFFF)
            op += 2

            # Encode MatchLength
            if not in_ext_dict:
                match_code = _count_match(src, ip + MINMATCH, match_pos + MINMATCH, matchlimit)
            else:
                # Match starts in dictionary
                dict_end = d_size
                limit = min(ip + (dict_end - match_pos), matchlimit)
                match_code = 0
                i1 = ip + MINMATCH
                i2 = match_pos + MINMATCH
                while i1 < limit and i2 < dict_end:
                    if src[i1] != dict_data[i2]:
                        break
                    i1 += 1
                    i2 += 1
                    match_code += 1
                # If we reached the end of dict match, continue matching into source
                if i2 >= dict_end and i1 < matchlimit:
                    more = _count_match(src, i1, 0, matchlimit)
                    match_code += more

            ip += match_code + MINMATCH

            # Check output space for match length encoding
            if output_directive != _NOT_LIMITED:
                if op + 1 + LASTLITERALS + (match_code + 240) // 255 > olimit:
                    if output_directive == _FILL_OUTPUT:
                        # Reduce match length to fit
                        new_match_code = 15 - 1 + (olimit - op - 1 - LASTLITERALS) * 255
                        if new_match_code < 0:
                            new_match_code = 0
                        ip -= match_code - new_match_code
                        match_code = new_match_code
                    else:
                        return b'', 0

            if match_code >= ML_MASK:
                out[token_pos] += ML_MASK
                remaining = match_code - ML_MASK
                while remaining >= 255:
                    out[op] = 255
                    op += 1
                    remaining -= 255
                out[op] = remaining
                op += 1
            else:
                out[token_pos] += match_code

            anchor = ip

            # Test end of chunk
            if ip >= mflimit_plus_one:
                break

            # Fill table with ip-2 position
            h = _hash_position(src, ip - 2, hash_log)
            ht[h] = start_index + (ip - 2)

            # Test next position for immediate match
            h = _hash_position(src, ip, hash_log)
            current_idx = start_index + ip
            match_index = ht[h]
            ht[h] = current_idx

            in_ext_dict2 = False
            can_match = False

            if dict_directive == _USING_EXT_DICT and match_index < start_index:
                if dict_data is not None:
                    dict_match_pos = match_index - (start_index - d_size)
                    if (dict_match_pos >= 0 and dict_match_pos + 3 < d_size
                            and match_index + LZ4_DISTANCE_MAX >= current_idx):
                        if _read32_le(dict_data, dict_match_pos) == _read32_le(src, ip):
                            in_ext_dict2 = True
                            can_match = True
            else:
                mp = match_index - start_index
                if (mp >= 0 and mp + 3 < len(src)
                        and match_index + LZ4_DISTANCE_MAX >= current_idx
                        and ((dict_issue != _DICT_SMALL) or match_index >= prefix_idx_limit)):
                    if _read32_le(src, mp) == _read32_le(src, ip):
                        can_match = True

            if can_match:
                # Immediate match at ip: zero literals
                token_pos2 = op
                op += 1
                out[token_pos2] = 0

                if in_ext_dict2:
                    match_pos = match_index - (start_index - d_size)
                    offset2 = current_idx - match_index
                else:
                    match_pos = match_index - start_index
                    offset2 = ip - match_pos

                # Encode offset
                if output_directive == _FILL_OUTPUT:
                    if op + 2 + 1 + MFLIMIT - MINMATCH > olimit:
                        op = token_pos2
                        ip = iend
                        break
                _write16_le(out, op, offset2 & 0xFFFF)
                op += 2

                # Calculate match length
                if not in_ext_dict2:
                    mc = _count_match(src, ip + MINMATCH, match_pos + MINMATCH, matchlimit)
                else:
                    dict_end = d_size
                    mc = 0
                    i1 = ip + MINMATCH
                    i2 = match_pos + MINMATCH
                    while i1 < matchlimit and i2 < dict_end:
                        if src[i1] != dict_data[i2]:
                            break
                        i1 += 1
                        i2 += 1
                        mc += 1
                    if i2 >= dict_end and i1 < matchlimit:
                        mc += _count_match(src, i1, 0, matchlimit)

                ip += mc + MINMATCH

                if output_directive != _NOT_LIMITED:
                    if op + 1 + LASTLITERALS + (mc + 240) // 255 > olimit:
                        if output_directive == _FILL_OUTPUT:
                            new_mc = 15 - 1 + (olimit - op - 1 - LASTLITERALS) * 255
                            if new_mc < 0:
                                new_mc = 0
                            ip -= mc - new_mc
                            mc = new_mc
                        else:
                            return b'', 0

                if mc >= ML_MASK:
                    out[token_pos2] += ML_MASK
                    remaining = mc - ML_MASK
                    while remaining >= 255:
                        out[op] = 255
                        op += 1
                        remaining -= 255
                    out[op] = remaining
                    op += 1
                else:
                    out[token_pos2] += mc

                anchor = ip
                if ip >= mflimit_plus_one:
                    break

                # Fill table
                h = _hash_position(src, ip - 2, hash_log)
                ht[h] = start_index + (ip - 2)

            # Prepare next loop
            forward_h = _hash_position(src, ip, hash_log)

    # Last literals
    last_run = iend - anchor
    if output_directive != _NOT_LIMITED:
        if op + last_run + 1 + ((last_run + 255 - RUN_MASK) // 256) > olimit:
            if output_directive == _FILL_OUTPUT:
                last_run = olimit - op - 1
                last_run -= (last_run + 256 - RUN_MASK) // 256
                if last_run < 0:
                    last_run = 0
            else:
                return b'', 0

    if last_run >= RUN_MASK:
        out[op] = RUN_MASK << ML_BITS
        op += 1
        accumulator = last_run - RUN_MASK
        while accumulator >= 255:
            out[op] = 255
            op += 1
            accumulator -= 255
        out[op] = accumulator
        op += 1
    else:
        out[op] = last_run << ML_BITS
        op += 1

    out[op:op + last_run] = src[anchor:anchor + last_run]
    op += last_run

    input_consumed = anchor + last_run if output_directive == _FILL_OUTPUT else input_size
    return bytes(out[:op]), input_consumed


# ---------------------------------------------------------------------------
# Public compression API
# ---------------------------------------------------------------------------

def compress(
    data: BufferType,
    *,
    acceleration: int = 1,
    max_output_size: int | None = None,
) -> bytes:
    """Compress *data* using LZ4 block compression.

    Parameters
    ----------
    data : bytes | bytearray | memoryview
        Input data to compress.
    acceleration : int, optional
        Acceleration factor >= 1 (default 1). Higher values yield faster
        but larger output.
    max_output_size : int | None, optional
        If set, limits the output buffer size. Returns empty bytes on failure.

    Returns
    -------
    bytes
        Compressed data in LZ4 block format.

    Raises
    ------
    LZ4CompressError
        If the input exceeds LZ4_MAX_INPUT_SIZE.
    """
    src = _ensure_bytes(data)
    input_size = len(src)

    if input_size > LZ4_MAX_INPUT_SIZE:
        raise LZ4CompressError(
            f"Input size {input_size} exceeds LZ4_MAX_INPUT_SIZE ({LZ4_MAX_INPUT_SIZE})"
        )

    if acceleration < 1:
        acceleration = ACCELERATION_DEFAULT
    if acceleration > ACCELERATION_MAX:
        acceleration = ACCELERATION_MAX

    ctx = _CompressState()

    if max_output_size is not None:
        output_directive = _LIMITED_OUTPUT
        max_out = max_output_size
    else:
        max_out = compress_bound(input_size)
        if max_out == 0 and input_size > 0:
            raise LZ4CompressError("Input too large")
        output_directive = _NOT_LIMITED

    # Determine table type
    if input_size < _LZ4_64Klimit:
        table_type = _TABLE_BY_U16
    else:
        table_type = _TABLE_BY_U32

    result, _ = _compress_generic(
        ctx, src, max_out,
        output_directive, table_type,
        _NO_DICT, _NO_DICT_ISSUE,
        acceleration,
    )

    if not result and input_size > 0 and output_directive == _LIMITED_OUTPUT:
        raise LZ4CompressError("Compression failed: output buffer too small")

    return result


def compress_dest_size(
    data: BufferType,
    target_dst_size: int,
    *,
    acceleration: int = 1,
) -> tuple[bytes, int]:
    """Compress as much of *data* as possible into *target_dst_size* bytes.

    Returns (compressed_bytes, input_consumed).
    """
    src = _ensure_bytes(data)
    input_size = len(src)

    if acceleration < 1:
        acceleration = ACCELERATION_DEFAULT
    if acceleration > ACCELERATION_MAX:
        acceleration = ACCELERATION_MAX

    ctx = _CompressState()

    if target_dst_size >= compress_bound(input_size):
        result, _ = _compress_generic(
            ctx, src, target_dst_size,
            _NOT_LIMITED,
            _TABLE_BY_U16 if input_size < _LZ4_64Klimit else _TABLE_BY_U32,
            _NO_DICT, _NO_DICT_ISSUE,
            acceleration,
        )
        return result, input_size

    table_type = _TABLE_BY_U16 if input_size < _LZ4_64Klimit else _TABLE_BY_U32
    result, consumed = _compress_generic(
        ctx, src, target_dst_size,
        _FILL_OUTPUT, table_type,
        _NO_DICT, _NO_DICT_ISSUE,
        acceleration,
    )
    return result, consumed


# ---------------------------------------------------------------------------
# Decompression (LZ4_decompress_generic)
# ---------------------------------------------------------------------------

def _read_variable_length(src: bytes | bytearray, ip: int, ilimit: int, initial_check: bool) -> tuple[int, int]:
    """Read a variable-length field from compressed data.

    Returns (length, new_ip) or raises on error.
    """
    if initial_check and ip >= ilimit:
        return -1, ip

    length = 0
    s = src[ip]
    ip += 1
    length += s

    if ip > ilimit:
        return -1, ip

    if s != 255:
        return length, ip

    while True:
        s = src[ip]
        ip += 1
        length += s
        if ip > ilimit:
            return -1, ip
        if s != 255:
            break

    return length, ip


def _decompress_generic(
    src: bytes | bytearray,
    src_size: int,
    output_size: int,
    partial_decoding: bool,
    dict_directive: int,
    low_prefix: int,       # offset into output where prefix starts (for dict matching)
    dict_start: bytes | bytearray | None,
    dict_size: int,
) -> tuple[int, bytearray]:
    """Core decompression function.

    Returns (decompressed_size, output_buffer).
    Raises LZ4DecompressError on malformed input.
    """
    if output_size < 0:
        raise LZ4DecompressError("Invalid output size")

    if output_size == 0:
        if partial_decoding:
            return 0, bytearray()
        if src_size == 1 and src[0] == 0:
            return 0, bytearray()
        raise LZ4DecompressError("Non-empty compressed data for zero output")

    if src_size == 0:
        raise LZ4DecompressError("Empty compressed input")

    ip = 0
    iend = src_size

    out = bytearray(output_size)
    op = 0
    oend = output_size

    dict_end = (dict_start[:dict_size] if dict_start is not None else None) if dict_size > 0 else None
    check_offset = (dict_size < 64 * 1024)

    while True:
        if ip >= iend:
            raise LZ4DecompressError("Unexpected end of compressed data")

        # Read token
        token = src[ip]
        ip += 1

        # Decode literal length
        length = token >> ML_BITS

        if length == RUN_MASK:
            addl, ip = _read_variable_length(src, ip, iend - RUN_MASK, True)
            if addl < 0:
                raise LZ4DecompressError("Error reading literal length")
            length += addl

        # Copy literals
        cpy = op + length

        if (cpy > oend - MFLIMIT) or (ip + length > iend - (2 + 1 + LASTLITERALS)):
            # Near end of buffers
            if partial_decoding:
                if ip + length > iend:
                    length = iend - ip
                    cpy = op + length
                if cpy > oend:
                    cpy = oend
                    length = oend - op
            else:
                if (ip + length != iend) or (cpy > oend):
                    raise LZ4DecompressError(
                        "Corrupted data: literal length exceeds bounds"
                    )

            out[op:op + length] = src[ip:ip + length]
            ip += length
            op += length

            if not partial_decoding or (cpy == oend) or (ip >= iend - 2):
                break
        else:
            out[op:op + length] = src[ip:ip + length]
            ip += length
            op = cpy

        # Get offset
        if ip + 2 > iend:
            raise LZ4DecompressError("Unexpected end: cannot read offset")
        offset = _read16_le(src, ip)
        ip += 2

        match = op - offset

        # Get match length
        ml_token = token & ML_MASK

        if ml_token == ML_MASK:
            addl, ip = _read_variable_length(src, ip, iend - LASTLITERALS + 1, False)
            if addl < 0:
                raise LZ4DecompressError("Error reading match length")
            ml_token += addl

        ml = ml_token + MINMATCH

        # Check offset validity
        if check_offset:
            if match + dict_size < low_prefix:
                raise LZ4DecompressError(
                    f"Offset {offset} outside valid range"
                )

        # Handle external dictionary match
        if dict_directive == _USING_EXT_DICT and match < low_prefix:
            if dict_end is None:
                raise LZ4DecompressError("Offset references non-existent dictionary")

            if partial_decoding and op + ml > oend - LASTLITERALS:
                ml = min(ml, oend - op)

            copy_from_dict = low_prefix - match
            if ml <= copy_from_dict:
                # Match entirely within external dictionary
                dict_offset = dict_size - copy_from_dict
                out[op:op + ml] = dict_end[dict_offset:dict_offset + ml]
                op += ml
            else:
                # Match spans dictionary and current output
                dict_offset = dict_size - copy_from_dict
                out[op:op + copy_from_dict] = dict_end[dict_offset:dict_offset + copy_from_dict]
                op += copy_from_dict
                rest = ml - copy_from_dict
                # Copy from beginning of output (lowPrefix)
                copy_src = low_prefix
                if rest > op - low_prefix:
                    # Overlap copy
                    end_of_match = op + rest
                    copy_from = low_prefix
                    while op < end_of_match:
                        out[op] = out[copy_from]
                        op += 1
                        copy_from += 1
                else:
                    out[op:op + rest] = out[low_prefix:low_prefix + rest]
                    op += rest
            continue

        # Validate match position for simple case
        if match < 0:
            raise LZ4DecompressError(f"Invalid offset {offset}")
        if offset == 0:
            raise LZ4DecompressError("Invalid zero offset")

        # Partial decoding: may end within match
        if partial_decoding and (op + ml > oend - MATCH_SAFEGUARD_DISTANCE):
            mlen = min(ml, oend - op)
            if match + mlen > op:
                # Overlap copy
                for i in range(mlen):
                    out[op + i] = out[match + i]
            else:
                out[op:op + mlen] = out[match:match + mlen]
            op += mlen
            if op == oend:
                break
            continue

        # Full match copy
        cpy = op + ml
        if cpy > oend - LASTLITERALS:
            if not partial_decoding:
                raise LZ4DecompressError(
                    "Match extends too close to end of output"
                )
            cpy = oend
            ml = oend - op

        # Handle overlapping match (offset < ml)
        if offset < ml:
            # Byte-by-byte copy for overlap
            for i in range(ml):
                out[op + i] = out[match + i]
        else:
            out[op:op + ml] = out[match:match + ml]
        op = op + ml

    return op, out


# ---------------------------------------------------------------------------
# Public decompression API
# ---------------------------------------------------------------------------

def decompress(
    data: BufferType,
    uncompressed_size: int = -1,
) -> bytes:
    """Decompress LZ4 block-compressed *data*.

    Parameters
    ----------
    data : bytes | bytearray | memoryview
        Compressed data in LZ4 block format.
    uncompressed_size : int, optional
        Expected decompressed size. If -1, a heuristic upper bound is used.
        For reliable use, the caller should know the original size.

    Returns
    -------
    bytes
        Decompressed data.

    Raises
    ------
    LZ4DecompressError
        If the compressed data is malformed or the output buffer is too small.
    """
    src = _ensure_bytes(data)
    src_size = len(src)

    if src_size == 0:
        raise LZ4DecompressError("Empty input")

    if uncompressed_size < 0:
        # Heuristic: try progressively larger buffers
        # Start with a reasonable estimate
        estimate = max(src_size * 4, 256)
        max_tries = 20
        for attempt in range(max_tries):
            try:
                n, out = _decompress_generic(
                    src, src_size, estimate,
                    False, _NO_DICT, 0, None, 0,
                )
                return bytes(out[:n])
            except LZ4DecompressError:
                estimate = min(estimate * 2, LZ4_MAX_INPUT_SIZE)
                if estimate >= LZ4_MAX_INPUT_SIZE:
                    break
        raise LZ4DecompressError(
            "Decompression failed: could not determine output size. "
            "Please provide uncompressed_size."
        )

    n, out = _decompress_generic(
        src, src_size, uncompressed_size,
        False, _NO_DICT, 0, None, 0,
    )
    return bytes(out[:n])


def decompress_partial(
    data: BufferType,
    compressed_size: int,
    target_output_size: int,
    dst_capacity: int,
) -> bytes:
    """Partial decompression: decode up to *target_output_size* bytes.

    Parameters
    ----------
    data : bytes | bytearray | memoryview
        Compressed input.
    compressed_size : int
        Exact compressed size.
    target_output_size : int
        Maximum bytes to decompress.
    dst_capacity : int
        Output buffer capacity (>= target_output_size).

    Returns
    -------
    bytes
        Decompressed data (up to target_output_size bytes).
    """
    src = _ensure_bytes(data)
    capacity = min(target_output_size, dst_capacity)
    n, out = _decompress_generic(
        src, compressed_size, capacity,
        True, _NO_DICT, 0, None, 0,
    )
    return bytes(out[:n])


def decompress_using_dict(
    data: BufferType,
    compressed_size: int,
    dst_capacity: int,
    dict_start: BufferType | None,
    dict_size: int,
) -> bytes:
    """Decompress with an external dictionary.

    Parameters
    ----------
    data : bytes | bytearray | memoryview
        Compressed input.
    compressed_size : int
        Exact compressed size.
    dst_capacity : int
        Output buffer capacity.
    dict_start : bytes | bytearray | memoryview | None
        Dictionary data.
    dict_size : int
        Dictionary size.

    Returns
    -------
    bytes
        Decompressed data.
    """
    src = _ensure_bytes(data)
    dict_data = _ensure_bytes(dict_start) if dict_start is not None else None

    if dict_data is not None and dict_size > 0:
        n, out = _decompress_generic(
            src, compressed_size, dst_capacity,
            False, _USING_EXT_DICT, 0,
            dict_data, dict_size,
        )
    else:
        n, out = _decompress_generic(
            src, compressed_size, dst_capacity,
            False, _NO_DICT, 0, None, 0,
        )
    return bytes(out[:n])


def decompress_partial_using_dict(
    data: BufferType,
    compressed_size: int,
    target_output_size: int,
    dst_capacity: int,
    dict_start: BufferType | None,
    dict_size: int,
) -> bytes:
    """Partial decompression with external dictionary."""
    src = _ensure_bytes(data)
    dict_data = _ensure_bytes(dict_start) if dict_start is not None else None
    capacity = min(target_output_size, dst_capacity)

    if dict_data is not None and dict_size > 0:
        n, out = _decompress_generic(
            src, compressed_size, capacity,
            True, _USING_EXT_DICT, 0,
            dict_data, dict_size,
        )
    else:
        n, out = _decompress_generic(
            src, compressed_size, capacity,
            True, _NO_DICT, 0, None, 0,
        )
    return bytes(out[:n])


# ---------------------------------------------------------------------------
# Streaming compression (LZ4_stream_t equivalent)
# ---------------------------------------------------------------------------

class LZ4StreamEncode:
    """Block-level streaming compression state.

    Mirrors the C `LZ4_stream_t` for compressing a sequence of dependent
    blocks that share a sliding 64 KB window.
    """

    def __init__(self) -> None:
        self._ctx = _CompressState()

    def reset(self) -> None:
        """Full reset of the stream state."""
        self._ctx.reset()

    def reset_fast(self) -> None:
        """Fast reset - reuse hash table where safe."""
        self._ctx.hash_table = [0] * _HASH_TABLE_SIZE
        self._ctx.current_offset = 0
        self._ctx.table_type = _TABLE_CLEARED
        self._ctx.dictionary = None
        self._ctx.dict_size = 0
        self._ctx.dict_ctx = None

    def load_dict(self, dictionary: BufferType) -> int:
        """Load a dictionary for upcoming compression.

        Returns the effective dictionary size (at most 64 KB).
        """
        dict_data = _ensure_bytes(dictionary)
        dict_size = len(dict_data)

        self._ctx.reset()
        self._ctx.current_offset += 64 * 1024

        if dict_size < 8:  # HASH_UNIT = sizeof(reg_t) ~ 8 on 64-bit
            return 0

        if dict_size > 64 * 1024:
            dict_data = dict_data[dict_size - 64 * 1024:]
            dict_size = 64 * 1024

        self._ctx.dictionary = dict_data
        self._ctx.dict_size = dict_size
        self._ctx.table_type = _TABLE_BY_U32

        # Populate hash table by scanning dictionary
        hash_log = HASHLOG
        idx = self._ctx.current_offset - dict_size
        p = 0
        while p <= dict_size - 8:
            h = _hash_position(dict_data, p, hash_log)
            self._ctx.hash_table[h] = idx
            p += 3
            idx += 3

        return dict_size

    def load_dict_slow(self, dictionary: BufferType) -> int:
        """Load dictionary with thorough hash population."""
        result = self.load_dict(dictionary)
        if result == 0:
            return 0

        dict_data = self._ctx.dictionary
        dict_size = self._ctx.dict_size
        hash_log = HASHLOG
        limit = self._ctx.current_offset - 64 * 1024

        idx = self._ctx.current_offset - dict_size
        p = 0
        while p <= dict_size - 8:
            h = _hash_position(dict_data, p, hash_log)
            if self._ctx.hash_table[h] <= limit:
                self._ctx.hash_table[h] = idx
            p += 1
            idx += 1

        return dict_size

    def compress_continue(
        self,
        source: BufferType,
        max_output_size: int | None = None,
        acceleration: int = 1,
    ) -> bytes:
        """Compress the next block using history from previous blocks.

        The previous source data (up to 64 KB) must still be available
        at the same memory location.

        Returns compressed bytes, or raises on failure.
        """
        src = _ensure_bytes(source)
        input_size = len(src)

        if acceleration < 1:
            acceleration = ACCELERATION_DEFAULT
        if acceleration > ACCELERATION_MAX:
            acceleration = ACCELERATION_MAX

        # Renorm if needed (overflow protection)
        if self._ctx.current_offset + input_size > 0x80000000:
            delta = self._ctx.current_offset - 64 * 1024
            for i in range(HASH_SIZE_U32):
                if self._ctx.hash_table[i] < delta:
                    self._ctx.hash_table[i] = 0
                else:
                    self._ctx.hash_table[i] -= delta
            self._ctx.current_offset = 64 * 1024
            if self._ctx.dict_size > 64 * 1024:
                self._ctx.dict_size = 64 * 1024

        # Invalidate tiny dictionaries
        if (self._ctx.dict_size < 4
                and self._ctx.dict_ctx is None
                and input_size > 0):
            self._ctx.dict_size = 0
            self._ctx.dictionary = src

        if max_output_size is None:
            max_out = compress_bound(input_size)
        else:
            max_out = max_output_size

        # Determine dict mode
        dict_end_is_source = False
        if self._ctx.dictionary is not None and self._ctx.dict_size > 0:
            dict_end = bytes(self._ctx.dictionary[self._ctx.dict_size - 1:self._ctx.dict_size]) if self._ctx.dict_size > 0 else b''
            # Simplified: for pure Python we always use external dict mode
            dict_directive = _USING_EXT_DICT
            dict_issue = _DICT_SMALL if (self._ctx.dict_size < 64 * 1024 and self._ctx.dict_size < self._ctx.current_offset) else _NO_DICT_ISSUE
        else:
            dict_directive = _NO_DICT
            dict_issue = _NO_DICT_ISSUE

        result, _ = _compress_generic(
            self._ctx, src, max_out,
            _LIMITED_OUTPUT, _TABLE_BY_U32,
            dict_directive, dict_issue,
            acceleration,
        )

        # Update dictionary to current source
        self._ctx.dictionary = src
        self._ctx.dict_size = input_size

        if not result and input_size > 0:
            raise LZ4CompressError("Streaming compression failed")

        return result

    def save_dict(self, max_dict_size: int) -> bytes:
        """Save the last *max_dict_size* bytes of history.

        The returned bytes can be used as a dictionary for future blocks.
        """
        if max_dict_size > 64 * 1024:
            max_dict_size = 64 * 1024
        if max_dict_size > self._ctx.dict_size:
            max_dict_size = self._ctx.dict_size

        if max_dict_size <= 0 or self._ctx.dictionary is None:
            return b''

        dict_data = self._ctx.dictionary
        d_size = self._ctx.dict_size
        saved = bytes(dict_data[d_size - max_dict_size:d_size])

        self._ctx.dictionary = saved
        self._ctx.dict_size = max_dict_size

        return saved


# ---------------------------------------------------------------------------
# Streaming decompression (LZ4_streamDecode_t equivalent)
# ---------------------------------------------------------------------------

class LZ4StreamDecode:
    """Block-level streaming decompression state.

    Mirrors the C `LZ4_streamDecode_t` for decompressing dependent blocks.
    """

    def __init__(self) -> None:
        self._external_dict: bytes | bytearray | None = None
        self._prefix_end: bytes | bytearray | None = None
        self._ext_dict_size: int = 0
        self._prefix_size: int = 0

    def set_stream_decode(self, dictionary: BufferType | None, dict_size: int = 0) -> bool:
        """Set the initial dictionary for the decode stream."""
        if dictionary is not None and dict_size > 0:
            dict_data = _ensure_bytes(dictionary)
            self._prefix_size = dict_size
            self._prefix_end = dict_data[:dict_size]
        else:
            self._prefix_size = 0
            self._prefix_end = None
        self._external_dict = None
        self._ext_dict_size = 0
        return True

    def decompress_continue(
        self,
        data: BufferType,
        compressed_size: int,
        dst_capacity: int,
    ) -> bytes:
        """Decompress the next block in a streaming sequence.

        Previous output (up to 64 KB) is used as dictionary context.
        """
        src = _ensure_bytes(data)

        if self._external_dict is not None and self._ext_dict_size > 0:
            n, out = _decompress_generic(
                src, compressed_size, dst_capacity,
                False, _USING_EXT_DICT,
                0,
                self._external_dict, self._ext_dict_size,
            )
        elif self._prefix_end is not None and self._prefix_size > 0:
            n, out = _decompress_generic(
                src, compressed_size, dst_capacity,
                False, _USING_EXT_DICT,
                0,
                self._prefix_end, self._prefix_size,
            )
        else:
            n, out = _decompress_generic(
                src, compressed_size, dst_capacity,
                False, _NO_DICT, 0, None, 0,
            )

        result = bytes(out[:n])

        # Update streaming state: previous output becomes the dictionary
        self._external_dict = self._prefix_end
        self._ext_dict_size = self._prefix_size
        self._prefix_end = result
        self._prefix_size = n

        return result
