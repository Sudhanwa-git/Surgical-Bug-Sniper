# sample/test_core.py

import pytest
from core import normalize_tensors

def test_normalize_tensors_basic():
    """Correctly normalizes a simple vector."""
    result = normalize_tensors([3, 4])
    assert result == [0.6, 0.8]

def test_normalize_tensors_zero_magnitude():
    """BUG: Fails when magnitude is zero (e.g. empty or zeros)."""
    # This will trigger ZeroDivisionError in core.py
    result = normalize_tensors([0, 0])
    assert result == [0, 0]

def test_normalize_tensors_empty():
    """BUG: Fails on empty input."""
    result = normalize_tensors([])
    assert result == []
