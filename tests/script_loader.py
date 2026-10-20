"""Import executable Python scripts without changing their standalone layout."""

import importlib.machinery
import importlib.util
import sys
from pathlib import Path
from types import ModuleType

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def load_script(name: str) -> ModuleType:
    """Load a repository script as a module so tests can patch its dependencies."""
    module_name = name.replace("-", "_")
    loader = importlib.machinery.SourceFileLoader(module_name, str(PROJECT_ROOT / name))
    spec = importlib.util.spec_from_loader(module_name, loader)
    if spec is None:
        raise ImportError(f"Cannot load script: {name}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    loader.exec_module(module)
    return module
