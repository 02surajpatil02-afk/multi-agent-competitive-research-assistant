"""
WHY THIS FILE EXISTS
    Marks tools/ as a package. The package is the tool boundary: two tools -
    `search(query)` and `fetch(url)` - plus argument validation, the failure vocabulary,
    and the wrapper that makes page text safe to put in a prompt.

    MCP earns its place by being one interface with one place for argument validation,
    timeouts, size limits, and the injection boundary - not because it is modern
    (guidelines §7). This package is that one place. The Researcher and the Fact-Checker
    import from here and never from tavily or httpx, which is what makes the provider a
    configuration detail rather than a dependency of the agents.

    `fetch` is deliberately ours rather than a delegated call: guidelines §16 defines it
    as a request made from inside our network, and the SSRF check, the byte cap, and the
    redirect re-validation are only meaningful if we are the one making it.

    Nothing is re-exported here on purpose. `from tools import search` would bind the name
    `tools.search` to a function and make the module of the same name unreachable by
    attribute - a trap for anything that introspects or patches it. Callers name the
    module they want.

WHO CALLS IT
    Nothing directly. Callers import tools.search, tools.fetch, tools.validation,
    tools.untrusted, or tools.contracts.
"""
