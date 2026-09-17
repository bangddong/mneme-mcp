"""Core must remain importable when the optional Growth Lab is unavailable."""

import subprocess
import sys
import textwrap


def test_all_core_modules_import_with_growth_lab_blocked():
    """Any Core-to-Growth import must fail in a fresh interpreter."""
    script = textwrap.dedent(
        """
        import importlib
        import importlib.abc
        import pkgutil
        import sys

        class BlockGrowthLab(importlib.abc.MetaPathFinder):
            def find_spec(self, fullname, path=None, target=None):
                if fullname == "mneme.growth_lab" or fullname.startswith("mneme.growth_lab."):
                    raise ImportError(f"blocked optional module: {fullname}")
                return None

        sys.meta_path.insert(0, BlockGrowthLab())
        import mneme.core

        for module in pkgutil.walk_packages(
            mneme.core.__path__, prefix="mneme.core."
        ):
            importlib.import_module(module.name)
        """
    )

    result = subprocess.run(
        [sys.executable, "-c", script],
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
