"""The review tool: approve each photo's proposed changes; they're applied in the background.

    python -m flickr_organizer review      # opens http://127.0.0.1:8765

  app.py      ReviewApp: the queue, your decisions, the background worker
  server.py   serve(): local web server and JSON API
  static/     the page (index.html, style.css, app.js)
"""
from .app import STATUSES, ReviewApp
from .server import serve

__all__ = ["ReviewApp", "serve", "STATUSES"]
