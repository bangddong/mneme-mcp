"""Build a standards-compliant project wheel without fetching build tools.

The repository's wheel target is the ``mneme`` package.  This test-only builder
mirrors that target so the packaging integration test can stay fully offline.
"""

from __future__ import annotations

import base64
import csv
from hashlib import sha256
from io import StringIO
from pathlib import Path
import sys
import tomllib
from zipfile import ZIP_DEFLATED, ZipFile


def _digest(value: bytes) -> str:
    encoded = base64.urlsafe_b64encode(sha256(value).digest()).rstrip(b"=")
    return "sha256=" + encoded.decode("ascii")


def main() -> int:
    if len(sys.argv) != 3:
        return 2
    repository = Path(sys.argv[1]).resolve(strict=True)
    output = Path(sys.argv[2]).resolve(strict=True)
    configuration = tomllib.loads(
        (repository / "pyproject.toml").read_text(encoding="utf-8")
    )
    if configuration["tool"]["hatch"]["build"]["targets"]["wheel"][
        "packages"
    ] != ["mneme"]:
        raise RuntimeError("test wheel builder requires the pinned mneme wheel target")
    wheel_path = output / "mneme_mcp-0.1.0-py3-none-any.whl"
    dist_info = "mneme_mcp-0.1.0.dist-info"
    files: dict[str, bytes] = {}
    for source in sorted((repository / "mneme").rglob("*")):
        if source.is_file() and source.suffix not in {".pyc", ".pyo"}:
            relative = source.relative_to(repository).as_posix()
            if "__pycache__" not in source.parts:
                files[relative] = source.read_bytes()
    files[f"{dist_info}/METADATA"] = (
        "Metadata-Version: 2.1\n"
        "Name: mneme-mcp\n"
        "Version: 0.1.0\n"
        "Requires-Python: >=3.11\n\n"
    ).encode("utf-8")
    files[f"{dist_info}/WHEEL"] = (
        "Wheel-Version: 1.0\n"
        "Generator: task22-offline-test-builder\n"
        "Root-Is-Purelib: true\n"
        "Tag: py3-none-any\n\n"
    ).encode("utf-8")
    record_path = f"{dist_info}/RECORD"
    record_stream = StringIO(newline="")
    writer = csv.writer(record_stream, lineterminator="\n")
    for name, value in sorted(files.items()):
        writer.writerow((name, _digest(value), len(value)))
    writer.writerow((record_path, "", ""))
    files[record_path] = record_stream.getvalue().encode("utf-8")
    with ZipFile(wheel_path, "w", compression=ZIP_DEFLATED) as archive:
        for name, value in sorted(files.items()):
            archive.writestr(name, value)
    print(wheel_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
