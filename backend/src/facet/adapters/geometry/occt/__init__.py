"""OpenCascade geometry adapter.

Imports lazily enough that a machine without ``cadquery-ocp`` installed simply
gets an ImportError here, which the composition root catches to fall back to the
analytic kernel.
"""

from .kernel import OcctKernel

__all__ = ["OcctKernel"]
