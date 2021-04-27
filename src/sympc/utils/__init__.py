"""Util functions needed around the repository."""

from .mpc_utils import count_wraps
from .mpc_utils import decompose
from .mpc_utils import generate_random_element
from .mpc_utils import get_new_generator
from .mpc_utils import get_type_from_ring
from .utils import get_session
from .utils import islocal
from .utils import ispointer
from .utils import parallel_execution

__all__ = [
    "ispointer",
    "islocal",
    "parallel_execution",
    "get_session",
    "count_wraps",
    "get_new_generator",
    "generate_random_element",
    "get_type_from_ring",
    "decompose",
]
