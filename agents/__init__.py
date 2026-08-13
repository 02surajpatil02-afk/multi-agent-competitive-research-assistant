"""
WHY THIS FILE EXISTS
    Marks agents/ as a package. The package holds the five agents, one module each:
    planner, researcher, synthesizer, fact_checker, supervisor.

    There are exactly five, and reflection is not one of them - it is an LLM-powered
    evaluation and routing node that lives in graph/ (CLAUDE.md invariant 8). There is no
    BaseAgent either: five contracts that share nothing but a name would gain nothing from
    a common ancestor, and each module is a function taking ResearchState and returning the
    state fields it owns (guidelines §20).

    Nothing is re-exported here, for the reason tools/__init__.py gives: binding a name
    like `planner` to a function would make the module of the same name unreachable by
    attribute, which breaks anything that patches or introspects it.

WHO CALLS IT
    Nothing directly. Callers import agents.planner, agents.researcher,
    agents.synthesizer, agents.fact_checker, or agents.supervisor.
"""
