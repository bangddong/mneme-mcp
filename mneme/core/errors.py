"""Domain errors shared by Madi Core filesystem boundaries."""


class MadiError(Exception):
    """Base class for deterministic Core failures."""


class ArtifactExists(MadiError):
    """An immutable artifact path is already occupied."""


class ConcurrentWrite(MadiError):
    """A generation compare-and-swap observed stale state."""


class InvalidArtifact(MadiError):
    """Artifact content does not satisfy the foundational format contract."""


class PortabilityViolation(InvalidArtifact):
    """A portable write could disclose local-only state."""


class UnsafePath(InvalidArtifact):
    """A requested path escapes or violates its storage boundary."""
