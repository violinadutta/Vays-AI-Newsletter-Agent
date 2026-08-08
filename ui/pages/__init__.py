"""Streamlit pages.

Each module exposes a single ``render()`` function, which ``app.py`` wires into
``st.navigation``. Pages call ``services/`` and never touch the database, the
network, or an adapter directly — the one architectural rule that keeps the UI
replaceable and the logic testable.

Specification: ``docs/05_UI_SPEC.md``.
"""
