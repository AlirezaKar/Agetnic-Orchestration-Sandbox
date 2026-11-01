"""The single place the version lives.

``app.main`` re-exports it (``from app.version import VERSION``), so the API, the
page's ``?v=`` cache-bust and the changelog file names all come from here.
"""

VERSION = "0.11.1"
