"""Configure sys.path so tests can import hex-events modules."""
import sys
import os
import pytest

# Repo root = parent of this conftest.py's directory
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Add parent dir (the hex-events root) to path
sys.path.insert(0, REPO_ROOT)


@pytest.fixture
def repo_root():
    """Return the absolute path to the hex-events repo root."""
    return REPO_ROOT
