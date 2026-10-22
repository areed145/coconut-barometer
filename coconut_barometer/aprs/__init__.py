"""APRS: where the callsigns in Settings → APRS are, heard live from APRS-IS (no history API; the track
builds up from when the app starts listening).

  stream.py   the APRS-IS connection: a read-only login filtered to the callsigns; positions are stored
  service.py  holds the hub and the stream for the web app
"""
