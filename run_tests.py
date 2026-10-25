"""Alle Tests ausführen.

    python run_tests.py        kurze Übersicht
    python run_tests.py -v     jeden Test einzeln anzeigen

Ein Lauf zählt nur als bestanden, wenn am Ende ALL TESTS PASSED steht.
Die Tests brauchen keine Hardware und kein pyserial.
"""

import logging
import sys
import unittest
from pathlib import Path


def main() -> int:
    verbose = "-v" in sys.argv
    logging.basicConfig(level=logging.DEBUG if verbose else logging.CRITICAL)
    root = Path(__file__).resolve().parent
    sys.path.insert(0, str(root))
    suite = unittest.defaultTestLoader.discover(str(root / "test"), pattern="test_*.py", top_level_dir=str(root))
    result = unittest.TextTestRunner(verbosity=2 if verbose else 1).run(suite)
    if result.wasSuccessful():
        print("\nALL TESTS PASSED")
        return 0
    print("\nTESTS FAILED")
    return 1


if __name__ == "__main__":
    sys.exit(main())
