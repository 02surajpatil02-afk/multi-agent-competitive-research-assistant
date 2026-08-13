"""
WHY THIS FILE EXISTS
    Marks graph/ as a package. The package holds the LangGraph side of the system:
    ResearchState in state.py, the reflection node in reflection.py, and the wiring,
    the nine nodes, and the checkpointer setup in build.py.

WHO CALLS IT
    Nothing directly. Callers import graph.state, graph.reflection, or graph.build.
"""
