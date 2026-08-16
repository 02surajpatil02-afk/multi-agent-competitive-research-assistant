"""
WHY THIS FILE EXISTS
    Marks routes/ as a package. It holds the outermost boundary in the system: the five
    FastAPI endpoints in api.py, and the API-key authentication that stands in front of four
    of them in auth.py.

WHO CALLS IT
    app.py, which builds the application. Nothing else imports it.
"""
