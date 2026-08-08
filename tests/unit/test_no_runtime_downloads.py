"""Guards against dependencies that fetch things at runtime.

A package that downloads a corpus or a model file on first use breaks two
promises at once: offline development stops working, and a handover install on a
locked-down company machine fails in a way that looks like our bug.

``check_no_local_inference.py`` covers ML runtimes. This covers the subtler case
of an otherwise-innocent library reaching for the network.
"""

from __future__ import annotations

import sys

from modules.scraper.newspaper_extractor import NewspaperExtractor


def test_newspaper_nlp_is_never_invoked() -> None:
    """``newspaper4k``'s ``.nlp()`` downloads NLTK corpora on first call.

    We use only its extraction API. This asserts the source never *calls* it, so
    a well-meaning future edit that adds keyword extraction fails here rather
    than on a customer's machine with no internet access.

    Parsed with ``ast`` rather than searched as text — a substring check matches
    the comment that explains why we don't call it, which is a test that fails
    for being correct.
    """
    import ast
    import inspect
    import textwrap

    tree = ast.parse(textwrap.dedent(inspect.getsource(NewspaperExtractor)))
    calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "nlp"
    ]

    assert not calls, "newspaper's .nlp() triggers an NLTK corpus download"


def test_nltk_is_not_imported_by_our_extraction_path() -> None:
    """Importing the extractor must not pull NLTK into the process."""
    for module in list(sys.modules):
        if module.startswith("nltk"):
            sys.modules.pop(module, None)

    NewspaperExtractor()

    assert not any(m.startswith("nltk") for m in sys.modules)


def test_trafilatura_network_access_is_disabled() -> None:
    """Trafilatura can fetch URLs itself. Letting it would bypass every SSRF
    protection in ``fetcher.py``, so its timeout is pinned to zero and we only
    ever hand it HTML we already fetched safely."""
    from modules.scraper.trafilatura_extractor import _CONFIG

    assert _CONFIG.get("DEFAULT", "EXTRACTION_TIMEOUT") == "0"
