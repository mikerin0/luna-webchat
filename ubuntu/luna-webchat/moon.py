import re
from datetime import date, datetime

_MOON_PHASE_RE = re.compile(r"\b(moon\s*phase|phase of the moon)\b", re.IGNORECASE)

# Synodic month (new moon to new moon) and one known reference new moon,
# used to compute phase by simple modular arithmetic - no lookup needed.
_SYNODIC_MONTH_DAYS = 29.530588861
_REFERENCE_NEW_MOON = datetime(2000, 1, 6, 18, 14)

_PHASE_THRESHOLDS = [
    (0.033, "New Moon"),
    (0.216, "Waxing Crescent"),
    (0.283, "First Quarter"),
    (0.467, "Waxing Gibbous"),
    (0.533, "Full Moon"),
    (0.716, "Waning Gibbous"),
    (0.783, "Last Quarter"),
    (0.966, "Waning Crescent"),
    (1.001, "New Moon"),
]

_MONTHS = [
    "january", "february", "march", "april", "may", "june",
    "july", "august", "september", "october", "november", "december",
]
_MONTH_DATE_RE = re.compile(
    r"\b(" + "|".join(_MONTHS) + r")\s+(\d{1,2})(?:st|nd|rd|th)?,?\s+(\d{4})\b", re.IGNORECASE
)
_SLASH_DATE_RE = re.compile(r"\b(\d{1,2})/(\d{1,2})/(\d{4})\b")
_ISO_DATE_RE = re.compile(r"\b(\d{4})-(\d{1,2})-(\d{1,2})\b")


def moon_phase_requested(text: str) -> bool:
  return bool(_MOON_PHASE_RE.search(text or ""))


def _extract_date(text: str) -> date | None:
  m = _ISO_DATE_RE.search(text)
  if m:
    try:
      return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    except ValueError:
      pass

  m = _SLASH_DATE_RE.search(text)
  if m:
    try:
      return date(int(m.group(3)), int(m.group(1)), int(m.group(2)))
    except ValueError:
      pass

  m = _MONTH_DATE_RE.search(text)
  if m:
    month = _MONTHS.index(m.group(1).lower()) + 1
    try:
      return date(int(m.group(3)), month, int(m.group(2)))
    except ValueError:
      pass

  return None


def _phase_name_for(target: date) -> str:
  dt = datetime(target.year, target.month, target.day, 12, 0)
  days_since_new = (dt - _REFERENCE_NEW_MOON).total_seconds() / 86400.0
  fraction = (days_since_new % _SYNODIC_MONTH_DAYS) / _SYNODIC_MONTH_DAYS
  for threshold, name in _PHASE_THRESHOLDS:
    if fraction < threshold:
      return name
  return "New Moon"


def moon_phase_reply(text: str) -> str:
  target = _extract_date(text) or date.today()
  phase = _phase_name_for(target)
  return f"The moon's phase on {target.strftime('%B %-d, %Y')} is {phase} (calculated directly, not looked up or recalled)."
