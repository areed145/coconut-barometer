"""A period to report on: the last N days (the 7 days … All buttons), or a custom one picked as two dates.

Every page with a range takes either `days`, or `start` and `end` (YYYY-MM-DD, both days included). The dates are
days in whatever zone the page counts in: the viewer's for Music, UTC for the site's visitors, Flickr's GMT days for
photo analytics. The period before a custom one, for "vs before", is the same number of days just ahead of it."""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

ALL = 3650                                  # "All" on the buttons: every day there is
MAX_DAYS = 36600                            # a custom period of up to a century


class BadPeriod(ValueError):
    """Dates that don't make a period: the message is safe to show."""


@dataclass(frozen=True)
class Dates:
    """A custom period: first and last day, both included."""
    start: dt.date
    end: dt.date

    @property
    def days(self) -> int:
        return (self.end - self.start).days + 1

    def before(self) -> "Dates":
        """The same number of days just ahead of it."""
        return Dates(self.start - dt.timedelta(days=self.days), self.start - dt.timedelta(days=1))


def custom(start: str | None, end: str | None, today: dt.date) -> Dates | None:
    """The custom period `start`..`end`, or None if neither is given. The end can't be after today (a period running on
    past it is cut there); neither date can be past today at all."""
    if not start and not end:
        return None
    if not (start and end):
        raise BadPeriod("a custom period needs both a start and an end")
    try:
        first, last = dt.date.fromisoformat(start), dt.date.fromisoformat(end)
    except ValueError:
        raise BadPeriod("dates are YYYY-MM-DD") from None
    if first > today:
        raise BadPeriod("the period starts after today")
    last = min(last, today)
    if first > last:
        raise BadPeriod("the period ends before it starts")
    if (last - first).days >= MAX_DAYS:
        raise BadPeriod("that period is too long")
    return Dates(first, last)


def window(days: int, dates: Dates | None, zone, now: int) -> dict:
    """The period in epoch seconds: since (included) and until (left out; None, no end), and `before`, where the
    period just ahead of it starts (None for All, which has nothing before it). days: its length, for the color strip.
    A custom period runs from midnight of its first day to midnight after its last, in the viewer's zone."""
    if dates:
        midnight = lambda d: int(dt.datetime.combine(d, dt.time(), zone).timestamp())
        return {"since": midnight(dates.start), "until": midnight(dates.end + dt.timedelta(days=1)),
                "before": midnight(dates.before().start), "days": dates.days}
    if days >= ALL:
        return {"since": 0, "until": None, "before": None, "days": ALL}
    since = now - days * 86400
    return {"since": since, "until": None, "before": since - days * 86400, "days": days}


def within(rows: list[dict], w: dict, key: str = "played_at") -> list[dict]:
    """The rows before the window's end (rows from a query that already starts where it should)."""
    return rows if w["until"] is None else [r for r in rows if r[key] < w["until"]]
