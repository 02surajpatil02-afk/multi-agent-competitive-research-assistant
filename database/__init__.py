"""
WHY THIS FILE EXISTS
    Marks database/ as a package. The package holds the durable side of the system: the
    five application tables in schema.py, the statements that read and write them in
    queries.py, and the Alembic migrations that create them in migrations/.

    LangGraph's checkpoint tables are deliberately not here. The checkpointer owns them
    through its own setup(), Alembic does not touch them, and the factory that builds it
    lives with the rest of the graph wiring in graph/build.py (guidelines §19).

WHO CALLS IT
    Nothing directly. Callers import database.schema or database.queries.
"""
