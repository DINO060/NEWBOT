"""
Shared batch state and constants to avoid circular imports.
"""
from typing import Dict, List

# Global batch storage
user_batches: Dict[int, List[dict]] = {}

# Maximum files allowed in batch
MAX_BATCH_FILES: int = 24

__all__ = [
    'user_batches',
    'MAX_BATCH_FILES',
]


