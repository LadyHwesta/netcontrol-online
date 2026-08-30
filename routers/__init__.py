"""Domain routers for the NetControl Online backend.

Each module here holds one feature domain's FastAPI routes, wired up via
`app.include_router(...)` in main.py. `deps.py`, `schemas.py`, and
`helpers.py` hold the auth/db dependencies, Pydantic schemas, and helper
functions shared across 2+ of those domains — see TECH_DEBT.md's (resolved)
"Single-file backend" entry for the design behind this split.
"""
