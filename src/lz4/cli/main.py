"""
Minimal CLI entry point for the LZ4 compression tool.

This is a stub that will be extended in Milestone 5 with full CLI support.
Currently supports basic block compress/decompress and --version.

License: GPL-2.0-or-later (CLI portion)
"""

import argparse
import sys
from pathlib import Path

import lz4


def _get_default_mode() -> str:
    """Determine default mode based on invocation name."""
    name = Path(sys.argv[0]).stem.lower() if sys.argv else "lz4"
    if name in ("unlz4", "lz4cat"):
        return "decompress"
    return "compress"


def build_parser() -> argparse.ArgumentParser:
    """Build the argument parser."""
    parser = argparse.ArgumentParser(
        prog="lz4",
        description="LZ4 block compression tool (pure Python)",
    )
    parser.add_argument(
        "--version", "-V",
        action="version",
        version=f"lz4 {lz4.__version__} (pure Python)",
    )
    parser.add_argument(
        "-d", "--decompress",
        action="store_true",
        default=False,
        help="Decompress (default mode when invoked as unlz4 or lz4cat)",
    )
    parser.add_argument(
        "-z", "--compress",
        action="store_true",
        default=False,
        help="Force compression mode",
    )
    parser.add_argument(
        "-f", "--force",
        action="store_true",
        default=False,
        help="Force overwrite of output file",
    )
    parser.add_argument(
        "-q", "--quiet",
        action="store_true",
        default=False,
        help="Suppress warnings and progress",
    )
    parser.add_argument(
        "-v", "--verbose",
        action="store_true",
        default=False,
        help="Verbose mode",
    )
    # Compression level (1-9 for fast mode)
    for level in range(1, 10):
        parser.add_argument(
            f"-{level}",
            dest="level",
            action="store_const",
            const=level,
            help=argparse.SUPPRESS,
        )
    parser.add_argument(
        "input",
        nargs="?",
        default=None,
        help="Input file (default: stdin)",
    )
    parser.add_argument(
        "output",
        nargs="?",
        default=None,
        help="Output file (default: stdout)",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    """CLI entry point."""
    parser = build_parser()
    args = parser.parse_args(argv)

    default_mode = _get_default_mode()

    # Determine mode
    if args.decompress:
        mode = "decompress"
    elif args.compress:
        mode = "compress"
    else:
        mode = default_mode

    acceleration = 1
    if args.level is not None:
        # In fast mode, higher level numbers map to higher acceleration
        acceleration = args.level

    # Read input
    try:
        if args.input and args.input != "-":
            with open(args.input, "rb") as f:
                data = f.read()
        else:
            data = sys.stdin.buffer.read()
    except FileNotFoundError:
        print(f"lz4: {args.input}: No such file or directory", file=sys.stderr)
        return 1
    except OSError as e:
        print(f"lz4: {e}", file=sys.stderr)
        return 1

    # Process
    try:
        if mode == "compress":
            from lz4.block import compress
            result = compress(data, acceleration=acceleration)
        else:
            from lz4.block import decompress
            result = decompress(data, uncompressed_size=-1)
    except lz4.LZ4Error as e:
        print(f"lz4: {e}", file=sys.stderr)
        return 1

    # Write output
    try:
        if args.output and args.output != "-":
            write_mode = "wb" if args.force or not Path(args.output).exists() else "xb"
            try:
                with open(args.output, write_mode) as f:
                    f.write(result)
            except FileExistsError:
                print(
                    f"lz4: {args.output}: File exists; use -f to overwrite",
                    file=sys.stderr,
                )
                return 1
        else:
            sys.stdout.buffer.write(result)
    except OSError as e:
        print(f"lz4: {e}", file=sys.stderr)
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
