"""The review queue's logic: ReviewApp (app.py) holds the photos in review, what you do with each
(apply, ignore, re-process), and the background worker that makes the Flickr and vision calls.
The web app (coconut_barometer.web) serves it as the Review page and /api/review."""
from .app import STATUSES, ReviewApp

__all__ = ["ReviewApp", "STATUSES"]
