import re

from config import DISPLAY_LCD_PHRASES, DISPLAY_LED_PHRASES, DISPLAY_PC_PHRASES, DISPLAY_TRIGGER_VERBS

_PREP_FRAGMENT = r"(?:on|to|onto)\s+(?:the\s+|my\s+|that\s+|a\s+)?"


def _alt(phrases: list[str]) -> str:
  return "|".join(re.escape(p) for p in sorted(phrases, key=len, reverse=True))


def _target_regex(phrases: list[str]) -> re.Pattern | None:
  if not phrases:
    return None
  verbs = _alt(DISPLAY_TRIGGER_VERBS) or "show|display|put|send"
  # Up to 10 filler words between the verb and "on/to the <target>" -- wide
  # enough for natural phrasing like "display image from camera 1 on pc
  # screen" (4 words in between), which a tighter cap missed entirely.
  return re.compile(
    rf"\b(?:{verbs})\b(?:\s+\w+){{0,10}}?\s+{_PREP_FRAGMENT}(?:{_alt(phrases)})\b",
    re.IGNORECASE,
  )


def _build_targets() -> list[tuple[str, re.Pattern | None]]:
  # PC first: "pc"/"computer" phrases are more specific and shouldn't be
  # shadowed by looser LED/LCD matches.
  return [
    ("pc", _target_regex(DISPLAY_PC_PHRASES)),
    ("lcd", _target_regex(DISPLAY_LCD_PHRASES)),
    ("led", _target_regex(DISPLAY_LED_PHRASES)),
  ]


_TARGETS = _build_targets()


def _detect_display_target(prompt: str) -> tuple[str | None, str]:
  """Looks for a "display this on <led/lcd/pc>" instruction anywhere in the
  prompt. Returns (target, prompt_with_that_instruction_removed); target is
  None and the prompt unchanged if nothing matched."""
  text = str(prompt or "")
  for target, pattern in _TARGETS:
    if pattern is None:
      continue
    match = pattern.search(text)
    if not match:
      continue
    stripped = (text[: match.start()] + " " + text[match.end():]).strip(" ,.:;-")
    stripped = re.sub(r"\s+", " ", stripped).strip()
    return target, stripped or text
  return None, text
