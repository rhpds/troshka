import sys
import os

# Add operator source to path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

# Mock kopf before any handler imports
from unittest.mock import MagicMock

sys.modules["kopf"] = MagicMock()
