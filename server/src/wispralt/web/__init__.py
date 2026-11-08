"""Shared web (Jinja templates + HTMX helpers) for the admin/employee UI.

Phase 2 review fix: extract the templating singleton and HTMX header dep so
``routes/admin_ui.py``, ``routes/admin_data.py``, and ``routes/me.py`` don't
each drift their own copy.
"""
