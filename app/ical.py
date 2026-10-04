"""A minimal iCalendar (RFC 5545) writer - just enough for a session plan to be added to Google / Apple / Outlook
calendar: UTC start and end, a title, notes and a place. A leaf module (no app imports)."""
from datetime import datetime, timedelta
from typing import Iterable, Optional

PRODID = "-//nd-world//Session schedule//EN"


def escape_text(value: Optional[str]) -> str:
    """A TEXT value: backslash, semicolon and comma escaped, line breaks as \\n."""
    text = str(value or "").replace("\r\n", "\n").replace("\r", "\n")
    return text.replace("\\", "\\\\").replace(";", "\;").replace(",", "\\,").replace("\n", "\\n")


def fold_line(line: str) -> str:
    """Content lines are at most 75 octets; a longer one continues on the next line after CRLF + one space. Never
    splits a multi-byte character."""
    raw = line.encode("utf-8")
    if len(raw) <= 75:
        return line
    parts, current, size, limit = [], [], 0, 75
    for ch in line:
        n = len(ch.encode("utf-8"))
        if size + n > limit:
            parts.append("".join(current))
            current, size, limit = [], 0, 74          # continuation lines start with a space (1 octet)
        current.append(ch)
        size += n
    parts.append("".join(current))
    return "\r\n ".join(parts)


def utc_stamp(dt: datetime) -> str:
    """A naive UTC datetime as 20310504T183000Z."""
    return dt.strftime("%Y%m%dT%H%M%SZ")


def build_calendar(events: Iterable[dict], name: str = "", prodid: str = PRODID) -> str:
    """events: [{uid, start, end?, summary, description?, location?, url?, stamp?}], datetimes naive UTC."""
    lines = ["BEGIN:VCALENDAR", "VERSION:2.0", f"PRODID:{prodid}", "CALSCALE:GREGORIAN", "METHOD:PUBLISH"]
    if name:
        lines.append(f"X-WR-CALNAME:{escape_text(name)}")
    now = datetime.utcnow()
    for ev in events:
        start = ev["start"]
        end = ev.get("end") or start + timedelta(hours=3)
        lines += [
            "BEGIN:VEVENT",
            f"UID:{ev['uid']}",
            f"DTSTAMP:{utc_stamp(ev.get('stamp') or now)}",
            f"DTSTART:{utc_stamp(start)}",
            f"DTEND:{utc_stamp(end)}",
            f"SUMMARY:{escape_text(ev.get('summary'))}",
        ]
        if ev.get("description"):
            lines.append(f"DESCRIPTION:{escape_text(ev['description'])}")
        if ev.get("location"):
            lines.append(f"LOCATION:{escape_text(ev['location'])}")
        if ev.get("url"):
            lines.append(f"URL:{ev['url']}")
        lines += ["STATUS:CONFIRMED", "END:VEVENT"]
    lines.append("END:VCALENDAR")
    return "".join(fold_line(line) + "\r\n" for line in lines)
