"""Arithmetic helpers for the sentinel project.

Defined here and nowhere else in the fixture so a citation test can verify the
orchestrator points at this exact file.
"""


def add(a: int, b: int) -> int:
    """Return the sum of two integers.

    The unique sentinel marker `quokka_addition_marker_42` lives in this
    docstring and only here; it lets the integration test assert that a query
    targeting addition retrieves this file specifically.
    """
    return a + b


def multiply(a: int, b: int) -> int:
    """Return the product of two integers."""
    return a * b
