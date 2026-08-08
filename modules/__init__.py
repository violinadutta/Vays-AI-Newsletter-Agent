"""Adapter layer — the replaceable parts (Ports & Adapters).

Everything here talks to something outside the process: websites, the LLM
endpoint, the email provider, the database. These are the boundaries most likely
to change, so each one sits behind an interface defined in ``modules/*/base.py``.

Populated across M2–M6: ``scraper``, ``cleaner``, ``ai``, ``template``,
``email``, ``repository``.
"""
