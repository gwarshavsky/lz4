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

# Stub - will be implemented in subsequent tasks.
# This file exists to make the package importable during scaffolding.

from lz4 import LZ4CompressError, LZ4DecompressError  # noqa: F401


def compress_bound(input_size: int) -> int:
    raise NotImplementedError("Block compression not yet implemented")


def compress(data: bytes | bytearray | memoryview, *, acceleration: int = 1) -> bytes:
    raise NotImplementedError("Block compression not yet implemented")


def decompress(
    data: bytes | bytearray | memoryview,
    uncompressed_size: int = -1,
) -> bytes:
    raise NotImplementedError("Block decompression not yet implemented")
