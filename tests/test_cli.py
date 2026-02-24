"""
Tests for the LZ4 CLI entry point.

Tests cover:
- --version flag
- Basic compress/decompress via stdin/stdout
- Compression level flags
- -d flag for decompress mode
- -z flag for compress mode
- -f flag for force overwrite
- Error handling for invalid files
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile

import pytest

# Import the CLI main function directly for in-process testing
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from lz4.cli.main import main, build_parser
from lz4.block import compress, decompress


class TestCLIVersion:
    """Test --version flag."""

    def test_version(self, capsys):
        with pytest.raises(SystemExit) as exc_info:
            main(["--version"])
        assert exc_info.value.code == 0
        captured = capsys.readouterr()
        assert "1.10.0" in captured.out

    def test_version_short(self, capsys):
        with pytest.raises(SystemExit) as exc_info:
            main(["-V"])
        assert exc_info.value.code == 0


class TestCLIParser:
    """Test argument parsing."""

    def test_default_compress(self):
        parser = build_parser()
        args = parser.parse_args([])
        assert not args.decompress
        assert not args.compress

    def test_decompress_flag(self):
        parser = build_parser()
        args = parser.parse_args(["-d"])
        assert args.decompress

    def test_compress_flag(self):
        parser = build_parser()
        args = parser.parse_args(["-z"])
        assert args.compress

    def test_force_flag(self):
        parser = build_parser()
        args = parser.parse_args(["-f"])
        assert args.force

    def test_quiet_flag(self):
        parser = build_parser()
        args = parser.parse_args(["-q"])
        assert args.quiet

    def test_verbose_flag(self):
        parser = build_parser()
        args = parser.parse_args(["-v"])
        assert args.verbose

    def test_compression_level(self):
        parser = build_parser()
        args = parser.parse_args(["-1"])
        assert args.level == 1
        args = parser.parse_args(["-9"])
        assert args.level == 9


class TestCLICompressDecompress:
    """Test compress/decompress with files."""

    def test_compress_file(self, tmp_path):
        """Test compressing a file."""
        input_file = tmp_path / "input.txt"
        output_file = tmp_path / "output.lz4"
        data = b"Hello World! " * 100
        input_file.write_bytes(data)

        result = main(["-z", str(input_file), str(output_file)])
        assert result == 0
        assert output_file.exists()

        # The output should be compressed (smaller than input for repetitive data)
        compressed = output_file.read_bytes()
        assert len(compressed) < len(data)

    def test_decompress_file(self, tmp_path):
        """Test decompressing a file."""
        input_file = tmp_path / "input.txt"
        compressed_file = tmp_path / "compressed.lz4"
        output_file = tmp_path / "output.txt"

        data = b"Hello World! " * 100
        input_file.write_bytes(data)

        # First compress
        compressed = compress(data)
        compressed_file.write_bytes(compressed)

        # Then decompress via CLI
        result = main(["-d", str(compressed_file), str(output_file)])
        assert result == 0
        # Note: decompress without known size uses heuristic
        decompressed = output_file.read_bytes()
        assert decompressed == data

    def test_roundtrip_file(self, tmp_path):
        """Test compress then decompress roundtrip."""
        input_file = tmp_path / "input.txt"
        compressed_file = tmp_path / "compressed.lz4"
        output_file = tmp_path / "output.txt"

        data = b"Test roundtrip data " * 50
        input_file.write_bytes(data)

        # Compress
        result = main(["-z", str(input_file), str(compressed_file)])
        assert result == 0

        # Decompress
        result = main(["-d", str(compressed_file), str(output_file)])
        assert result == 0

        decompressed = output_file.read_bytes()
        assert decompressed == data

    def test_file_not_found(self, capsys):
        """Test error on non-existent input file."""
        result = main(["nonexistent_file.txt", "/dev/null"])
        assert result == 1

    def test_force_overwrite(self, tmp_path):
        """Test -f flag for overwriting existing files."""
        input_file = tmp_path / "input.txt"
        output_file = tmp_path / "output.lz4"

        data = b"test data" * 10
        input_file.write_bytes(data)
        output_file.write_bytes(b"existing content")

        # Without -f, should fail
        result = main(["-z", str(input_file), str(output_file)])
        assert result == 1

        # With -f, should succeed
        result = main(["-z", "-f", str(input_file), str(output_file)])
        assert result == 0
