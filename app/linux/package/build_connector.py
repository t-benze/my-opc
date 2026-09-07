#!/usr/bin/env python3
"""Build the N3 self-contained connector from an already-built real wheel."""
from __future__ import annotations

import argparse
from pathlib import Path
from pathlib import PurePosixPath
import subprocess
import sys
import tempfile
import zipfile


def _extract_wheel(wheel: Path, target: Path) -> None:
    """Install a pure-Python wheel without depending on ambient pip."""
    seen: set[str] = set()
    with zipfile.ZipFile(wheel) as archive:
        for member in archive.infolist():
            path = PurePosixPath(member.filename)
            if path.is_absolute() or not path.parts or ".." in path.parts:
                raise SystemExit("wheel_member_invalid")
            normalized = path.as_posix().rstrip("/")
            if normalized in seen:
                raise SystemExit("wheel_member_duplicate")
            seen.add(normalized)
            destination = target.joinpath(*path.parts)
            if member.is_dir():
                destination.mkdir(parents=True, exist_ok=True)
                continue
            destination.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(member) as source, destination.open("wb") as sink:
                sink.write(source.read())


def build_connector(wheel: Path, output: Path) -> Path:
    if not zipfile.is_zipfile(wheel):
        raise SystemExit("wheel_invalid")
    output = output.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="happyranch-connector-build-") as raw:
        root = Path(raw)
        installed = root / "installed-wheel"
        entry = root / "connector_entry.py"
        _extract_wheel(wheel.resolve(), installed)
        entry.write_text(
            "from runtime.remote_access.cli import main\n"
            "if __name__ == '__main__': main()\n",
            encoding="utf-8",
        )
        subprocess.run(
            [sys.executable, "-m", "PyInstaller", "--clean", "--noconfirm", "--onefile",
             "--name", "happyranch-connector", "--paths", str(installed),
             "--distpath", str(output.parent), "--workpath", str(root / "work"),
             "--specpath", str(root), str(entry)],
            cwd=root, check=True,
        )
        built = output.parent / "happyranch-connector"
        if built != output:
            built.replace(output)
    output.chmod(0o700)
    return output


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--wheel", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    print(build_connector(args.wheel, args.output))


if __name__ == "__main__":
    main()
