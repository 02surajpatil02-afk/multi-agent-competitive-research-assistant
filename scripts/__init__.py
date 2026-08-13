"""
WHY THIS FILE EXISTS
    Marks scripts/ as a package, so that check_model.py has exactly one module name.
    Without it the same file is importable as both `check_model` and `scripts.check_model`,
    which mypy reports as an error and which would let two copies of a script diverge.

WHO CALLS IT
    Nothing directly. The preflight is run as `python scripts/check_model.py`; the tests
    import `scripts.check_model`.
"""
