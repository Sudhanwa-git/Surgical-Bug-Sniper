# sample/core.py

import math

def normalize_tensors(tensors):
    """
    Normalizes a list of tensor values.
    BUG: Fails when the sum of magnitudes is zero or list is empty.
    """
    total_magnitude = math.sqrt(sum(x**2 for x in tensors))
    
    # Surgical Bug: Missing zero-check
    return [x / total_magnitude for x in tensors]
