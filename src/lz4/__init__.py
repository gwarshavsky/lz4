"""
LZ4 - Pure Python implementation of the LZ4 compression algorithm.

This package provides block-level compression and decompression compatible
with the LZ4 block format specification. Higher-level frame format, HC mode,
streaming, and file I/O support will be added in future milestones.

License: BSD 2-Clause
"""

__version__ = "1.10.0"
__version_info__ = (1, 10, 0)

# Version constants matching the C library
VERSION_MAJOR = 1
VERSION_MINOR = 10
VERSION_RELEASE = 0
VERSION_NUMBER = VERSION_MAJOR * 100 * 100 + VERSION_MINOR * 100 + VERSION_RELEASE
VERSION_STRING = __version__


# ---------------------------------------------------------------------------
# Exception hierarchy
# ---------------------------------------------------------------------------

class LZ4Error(Exception):
    """Base exception for all LZ4 errors."""
    pass


class LZ4CompressError(LZ4Error):
    """Raised when compression fails."""
    pass


class LZ4DecompressError(LZ4Error):
    """Raised when decompression fails (corrupted data, truncation, etc.)."""
    pass


# ---------------------------------------------------------------------------
# Top-level convenience API (delegates to block module)
# ---------------------------------------------------------------------------

def compress(data: bytes, acceleration: int = 1) -> bytes:
    """Compress *data* using LZ4 block compression.

    Parameters
    ----------
    data : bytes | bytearray | memoryview
        Input data to compress.
    acceleration : int, optional
        Acceleration factor (1 = default, higher = faster but less compression).

    Returns
    -------
    bytes
        Compressed data in LZ4 block format.
    """
    from lz4.block import compress as _compress
    return _compress(data, acceleration=acceleration)


def decompress(data: bytes, uncompressed_size: int = -1) -> bytes:
    """Decompress LZ4 block-compressed *data*.

    Parameters
    ----------
    data : bytes | bytearray | memoryview
        Compressed data in LZ4 block format.
    uncompressed_size : int, optional
        Expected uncompressed size. If -1 (default), a safe upper bound
        is estimated.

    Returns
    -------
    bytes
        Decompressed data.
    """
    from lz4.block import decompress as _decompress
    return _decompress(data, uncompressed_size=uncompressed_size)


def compress_bound(input_size: int) -> int:
    """Return the worst-case compressed size for *input_size* bytes."""
    from lz4.block import compress_bound as _compress_bound
    return _compress_bound(input_size)
