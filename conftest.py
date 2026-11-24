"""Ensures the repository root is on sys.path so `import app` works in tests."""
import os
import tempfile

# `app.main` builds a module-level application at import time.  Point it at a
# scratch directory so running the suite never touches the checked-in
# data/index.sqlite3.
os.environ.setdefault("DATA_DIR", tempfile.mkdtemp(prefix="gazetteer-test-"))
