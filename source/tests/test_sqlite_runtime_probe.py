import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlite_runtime_probe import probe_sqlite_runtime


class SqliteRuntimeProbeTests(unittest.TestCase):
    def test_probe_uses_in_memory_database_and_reports_fts5(self):
        capabilities = probe_sqlite_runtime()
        self.assertTrue(capabilities["sqlite"])
        self.assertTrue(capabilities["fts5"])


if __name__ == "__main__":
    unittest.main()
