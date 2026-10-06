"""Read-only access to Markdown files below one filesystem root."""

from pathlib import Path, PurePosixPath, PureWindowsPath


class FileSystemSource:
    """Expose Markdown below *root* without providing mutation operations."""

    def __init__(self, root: Path):
        self.root = Path(root).resolve()

    def list_markdown(self) -> list[str]:
        """Return safe Markdown paths relative to the source root, sorted."""
        if not self.root.is_dir():
            return []

        paths: list[str] = []
        for candidate in self.root.rglob("*.md"):
            if self._is_safe_candidate(candidate) and candidate.is_file():
                paths.append(candidate.relative_to(self.root).as_posix())
        return sorted(paths)

    def read(self, path: str) -> str | None:
        """Read one safe, relative UTF-8 Markdown path, or return ``None``."""
        candidate = self._candidate_for(path)
        if candidate is None or not self._is_safe_candidate(candidate):
            return None
        try:
            if not candidate.is_file():
                return None
            return candidate.read_text(encoding="utf-8")
        except (OSError, UnicodeError):
            return None

    def _candidate_for(self, path: str) -> Path | None:
        if not isinstance(path, str) or not path:
            return None

        normalized_path = path.replace("\\", "/")
        posix_path = PurePosixPath(normalized_path)
        windows_path = PureWindowsPath(normalized_path)
        if (
            posix_path.is_absolute()
            or windows_path.is_absolute()
            or windows_path.drive
            or windows_path.root
            or ".." in posix_path.parts
        ):
            return None

        return self.root.joinpath(*posix_path.parts)

    def _is_safe_candidate(self, candidate: Path) -> bool:
        try:
            resolved = candidate.resolve()
            return (
                resolved.is_relative_to(self.root)
                and resolved.suffix.lower() == ".md"
            )
        except (OSError, ValueError):
            return False
