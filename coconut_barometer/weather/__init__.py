"""WeatherFlow Tempest stations: REST history, the live WebSocket, and what the Weather pages show.

  parse.py    obs_st arrays (positional) -> named, metric fields
  client.py   the REST API (stations, observations by device), with a log of recent requests
  sync.py     fills a device's history from REST: forward from the newest stored, back to backfill_days
  stream.py   the WebSocket: live observations are stored as they arrive, rapid wind is kept in memory
  service.py  holds the client, the live hub and the stream for the web app
"""
