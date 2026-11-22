#!/usr/bin/env python3
"""Build the standalone `adz` Python zipapp archive from `hestia_adz/`."""

from __future__ import annotations

import argparse
import shutil
import tempfile
import zipapp
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent


def build_zipapp(output: Path, compressed: bool = True) -> Path:
    output = output.expanduser().resolve()
    output.parent.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(prefix="hestia-adz-build-") as tmpdir:
        staging = Path(tmpdir)
        shutil.copytree(
            REPO_ROOT / "hestia_adz",
            staging / "hestia_adz",
            ignore=shutil.ignore_patterns("__pycache__", "*.py[cod]", "*.pyo"),
        )
        shutil.copy2(REPO_ROOT / "__main__.py", staging / "__main__.py")
        zipapp.create_archive(
            staging,
            target=output,
            interpreter="/usr/bin/env python3",
            compressed=compressed,
        )

    output.chmod(0o755)
    return output


def main() -> int:
    parser = argparse.ArgumentParser(description="Build the standalone adz zipapp executable.")
    parser.add_argument(
        "-o",
        "--output",
        type=Path,
        default=REPO_ROOT / "dist" / "adz",
        help="Destination path for built zipapp (default: dist/adz)",
    )
    parser.add_argument(
        "--uncompressed",
        action="store_true",
        help="Disable deflate compression inside the zipapp archive",
    )
    args = parser.parse_args()
    built = build_zipapp(args.output, compressed=not args.uncompressed)
    print(f"Built {built} ({built.stat().st_size} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
