#!/usr/bin/env python3
"""Compose the reproducible N3 Linux artifact from already-built inputs."""
from __future__ import annotations
import argparse
from pathlib import Path
from runtime.remote_access.linux_package import build_linux_package

def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--sidecar", type=Path, required=True)
    parser.add_argument("--connector", type=Path, required=True,
                        help="self-contained connector executable built from the supplied wheel")
    parser.add_argument("--wheel", type=Path, required=True)
    parser.add_argument("--version", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    sidecar_root = Path(__file__).resolve().parents[1] / "tsnet-sidecar"
    print(build_linux_package(args.output, args.sidecar, args.connector, args.wheel,
        sidecar_root / "third_party/dependency-inventory.json",
        sidecar_root / "third_party/THIRD_PARTY_NOTICES.md", version=args.version))

if __name__ == "__main__":
    main()
