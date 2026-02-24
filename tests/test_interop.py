"""
Cross-compatibility tests with the C LZ4 binary.

These tests are skipped if the C `lz4` binary is not available on the system.
They verify that:
- Python-compressed data can be decompressed by the C lz4 tool.
- C-compressed data can be decompressed by the Python implementation.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile

import pytest

# Skip all tests in this module if lz4 binary is not available
pytestmark = pytest.mark.skipif(
    shutil.which("lz4") is None,
    reason="C lz4 binary not found on PATH",
)


class TestInterop:
    """Cross-compatibility tests with C LZ4."""

    # Placeholder for future cross-compatibility tests.
    # These will be implemented in Milestone 6 when the frame format is available,
    # since the C lz4 CLI operates on framed data, not raw blocks.

    def test_placeholder(self):
        """Placeholder - real interop tests require frame format support."""
        pytest.skip("Interop tests require frame format (Milestone 3+)")
