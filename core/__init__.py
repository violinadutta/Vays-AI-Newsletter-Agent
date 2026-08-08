"""Domain layer — pure Python.

Nothing in this package performs I/O or imports a framework. That constraint is
enforced by an ``import-linter`` contract in ``pyproject.toml`` and is what makes
the domain logic fully unit-testable with no database, no network and no GPU.
"""
