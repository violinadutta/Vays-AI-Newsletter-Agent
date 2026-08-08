"""Presentation layer — Streamlit pages and reusable components.

Responsible for rendering, capturing input and displaying state. Business logic,
direct database access and direct HTTP calls are all forbidden here: pages call
``services/`` and nothing else.

Populated in M7.
"""
