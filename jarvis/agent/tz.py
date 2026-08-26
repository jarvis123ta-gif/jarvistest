"""tz.py — a timezone that works on Windows without installing anything.

Python's `zoneinfo` reads the system IANA database. Linux and macOS ship
one; **Windows does not**, so `ZoneInfo("America/Chicago")` raises
ZoneInfoNotFoundError on a stock Windows install unless the `tzdata` package
is present. Since this project promises no package manager, the rules for
the zones the principals actually live in are implemented here.

Order of preference:
  1. The system database, via zoneinfo — always correct, always current.
  2. The `tzdata` package if someone installed it anyway.
  3. A hand-rolled US zone below, with the post-2007 DST rule.
  4. A fixed offset, and `fallback_note()` says so out loud rather than
     letting a silently wrong clock ship a wrong deadline.

US DST since the Energy Policy Act of 2005 (in force from 2007): forward on
the second Sunday in March at 02:00 local, back on the first Sunday in
November at 02:00 local. Arizona and Hawaii do not observe it.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone, tzinfo

_HOUR = timedelta(hours=1)
_ZERO = timedelta(0)

# name -> (standard offset hours, abbreviation pair, observes DST)
_US_ZONES = {
    "America/New_York":    (-5, ("EST", "EDT"), True),
    "America/Detroit":     (-5, ("EST", "EDT"), True),
    "America/Chicago":     (-6, ("CST", "CDT"), True),
    "America/Winnipeg":    (-6, ("CST", "CDT"), True),
    "America/Denver":      (-7, ("MST", "MDT"), True),
    "America/Phoenix":     (-7, ("MST", "MST"), False),
    "America/Los_Angeles": (-8, ("PST", "PDT"), True),
    "America/Anchorage":   (-9, ("AKST", "AKDT"), True),
    "Pacific/Honolulu":    (-10, ("HST", "HST"), False),
    "UTC":                 (0, ("UTC", "UTC"), False),
}

_fallback_used: str | None = None


def _nth_weekday(year: int, month: int, weekday: int, n: int, hour: int) -> datetime:
    """The nth given weekday of a month, at `hour` local. weekday: Monday=0."""
    d = datetime(year, month, 1, hour)
    d += timedelta(days=(weekday - d.weekday()) % 7)
    return d + timedelta(weeks=n - 1)


class _USTimeZone(tzinfo):
    """Enough of a US zone to be correct for dates from 2007 onward."""

    def __init__(self, name: str, std_hours: int,
                 abbrevs: tuple[str, str], dst_observed: bool):
        self._name = name
        self._std = timedelta(hours=std_hours)
        self._abbrevs = abbrevs
        self._dst = dst_observed

    def utcoffset(self, dt):
        return self._std + self.dst(dt)

    def tzname(self, dt):
        return self._abbrevs[1 if self.dst(dt) else 0]

    def dst(self, dt):
        if not self._dst or dt is None:
            return _ZERO
        naive = dt.replace(tzinfo=None)
        # Spring forward is compared at 03:00, not 02:00. The hour from
        # 02:00 to 02:59 on that Sunday never happens — the clock jumps
        # straight over it — and zoneinfo resolves such a nonexistent local
        # time to the offset *before* the transition. Comparing at 02:00
        # would disagree with the real database for exactly that hour.
        start = _nth_weekday(naive.year, 3, 6, 2, 3)   # 2nd Sunday in March
        end = _nth_weekday(naive.year, 11, 6, 1, 2)    # 1st Sunday in November
        return _HOUR if start <= naive < end else _ZERO

    def __repr__(self):
        return f"<tz {self._name} (built in, no tzdata needed)>"

    def __str__(self):
        return self._name


def get(name: str) -> tzinfo:
    """A tzinfo for `name`, whatever this machine happens to have."""
    global _fallback_used
    try:
        from zoneinfo import ZoneInfo
        return ZoneInfo(name)                      # system db, or tzdata
    except Exception:                              # noqa: BLE001
        pass

    if name in _US_ZONES:
        std, abbrevs, dst = _US_ZONES[name]
        _fallback_used = (
            f"No system timezone database on this machine, so {name} is using "
            "built-in US rules. Correct for dates from 2007 onward. Install "
            "the 'tzdata' package if you want the real IANA database.")
        return _USTimeZone(name, std, abbrevs, dst)

    _fallback_used = (
        f"No timezone database and no built-in rule for {name!r} — falling "
        "back to UTC. Times shown will be wrong for your location. Either "
        "install 'tzdata' or set JARVIS_TZ to a US zone.")
    return timezone.utc


def fallback_note() -> str | None:
    """What the UI shows when the clock is not fully trustworthy."""
    return _fallback_used


def now(name: str) -> datetime:
    return datetime.now(get(name))


if __name__ == "__main__":
    import sys
    zone = sys.argv[1] if len(sys.argv) > 1 else "America/Chicago"
    tz = get(zone)
    n = now(zone)
    print(f"zone      {zone}")
    print(f"impl      {tz!r}")
    print(f"now       {n.isoformat()}  ({n.tzname()})")
    print(f"offset    {n.utcoffset()}")
    print(f"fallback  {fallback_note() or 'none — system database in use'}")
