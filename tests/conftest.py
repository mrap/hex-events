"""Configure sys.path so tests can import hex-events modules."""
import sys
import os

# Add parent dir (the hex-events root) to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
