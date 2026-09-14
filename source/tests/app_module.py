"""Load the standalone .pyw entry point on every supported test platform."""

import importlib.util
import sys
from importlib.machinery import SourceFileLoader
from pathlib import Path

SOURCE_DIR = Path(__file__).resolve().parents[1]
if str(SOURCE_DIR) not in sys.path:
    sys.path.insert(0, str(SOURCE_DIR))
loader = SourceFileLoader("snipvoice", str(SOURCE_DIR / "snipvoice.pyw"))
spec = importlib.util.spec_from_loader(loader.name, loader)
snipvoice = importlib.util.module_from_spec(spec)
sys.modules[loader.name] = snipvoice
loader.exec_module(snipvoice)
