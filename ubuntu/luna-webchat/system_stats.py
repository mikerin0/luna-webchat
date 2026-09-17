import asyncio
import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any


def _read_cpu_times() -> tuple[int, int]:
  with open("/proc/stat", "r", encoding="utf-8") as handle:
    first_line = handle.readline().strip()

  parts = first_line.split()
  if len(parts) < 5 or parts[0] != "cpu":
    raise OSError("/proc/stat did not contain a cpu line")

  values = [int(value) for value in parts[1:]]
  idle = values[3] + (values[4] if len(values) > 4 else 0)
  total = sum(values)
  return total, idle


async def _cpu_usage_percent(sample_delay: float = 0.12) -> float | None:
  try:
    total_1, idle_1 = _read_cpu_times()
    await asyncio.sleep(sample_delay)
    total_2, idle_2 = _read_cpu_times()
  except (OSError, ValueError):
    return None

  total_delta = total_2 - total_1
  idle_delta = idle_2 - idle_1
  if total_delta <= 0:
    return None

  usage = (1.0 - (idle_delta / total_delta)) * 100.0
  return max(0.0, min(100.0, usage))


def _gpu_usage_percent() -> float | None:
  try:
    result = subprocess.run(
      ["nvidia-smi", "--query-gpu=utilization.gpu", "--format=csv,noheader,nounits"],
      capture_output=True,
      text=True,
      timeout=2,
      check=False,
    )
    values = [float(line.strip()) for line in result.stdout.splitlines() if line.strip()]
    if not values:
      return None
    return round(max(0.0, min(100.0, sum(values) / len(values))), 1)
  except (OSError, ValueError, subprocess.SubprocessError):
    return None


def _temperature_readings() -> list[dict[str, Any]]:
  readings: list[dict[str, Any]] = []

  def _add_reading(label: str, raw_value: str) -> None:
    try:
      value = float(raw_value.strip())
    except ValueError:
      return
    temp_c = value / 1000.0 if value > 1000 else value
    if temp_c < -30 or temp_c > 150:
      return
    readings.append({"label": label, "celsius": round(temp_c, 1)})

  thermal_root = Path("/sys/class/thermal")
  if thermal_root.exists():
    for zone in sorted(thermal_root.glob("thermal_zone*")):
      temp_file = zone / "temp"
      if not temp_file.exists():
        continue
      label = zone.name
      type_file = zone / "type"
      if type_file.exists():
        label_text = type_file.read_text(encoding="utf-8", errors="ignore").strip()
        if label_text:
          label = label_text
      _add_reading(label, temp_file.read_text(encoding="utf-8", errors="ignore"))

  hwmon_root = Path("/sys/class/hwmon")
  if hwmon_root.exists():
    for hwmon in sorted(hwmon_root.glob("hwmon*")):
      name_file = hwmon / "name"
      chip_name = hwmon.name
      if name_file.exists():
        chip_label = name_file.read_text(encoding="utf-8", errors="ignore").strip()
        if chip_label:
          chip_name = chip_label
      for temp_file in sorted(hwmon.glob("temp*_input")):
        prefix = temp_file.name[:-6]
        label_file = hwmon / f"{prefix}_label"
        label = chip_name
        if label_file.exists():
          label_text = label_file.read_text(encoding="utf-8", errors="ignore").strip()
          if label_text:
            label = f"{chip_name} {label_text}"
        _add_reading(label, temp_file.read_text(encoding="utf-8", errors="ignore"))

  unique: list[dict[str, Any]] = []
  seen: set[tuple[str, float]] = set()
  for row in readings:
    key = (str(row["label"]), float(row["celsius"]))
    if key in seen:
      continue
    seen.add(key)
    unique.append(row)

  unique.sort(key=lambda item: float(item["celsius"]), reverse=True)
  return unique[:5]


def _mem_info_gb() -> tuple[float | None, float | None]:
  total_kb = None
  available_kb = None
  try:
    for line in Path("/proc/meminfo").read_text(encoding="utf-8", errors="ignore").splitlines():
      if line.startswith("MemTotal:"):
        total_kb = float(line.split()[1])
      elif line.startswith("MemAvailable:"):
        available_kb = float(line.split()[1])
  except OSError:
    return None, None

  total_gb = None if total_kb is None else round(total_kb / (1024 ** 2), 1)
  available_gb = None if available_kb is None else round(available_kb / (1024 ** 2), 1)
  return total_gb, available_gb


async def _server_stats() -> dict[str, Any]:
  cpu = await _cpu_usage_percent()
  gpu = _gpu_usage_percent()
  disk = shutil.disk_usage("/")
  disk_used_pct = round((disk.used / (disk.total or 1)) * 100.0, 1)
  temps = _temperature_readings()

  load_avg = None
  try:
    load_avg = os.getloadavg()
  except OSError:
    load_avg = None

  uptime_seconds = None
  try:
    uptime_text = Path("/proc/uptime").read_text(encoding="utf-8", errors="ignore").split()[0]
    uptime_seconds = int(float(uptime_text))
  except (OSError, ValueError, IndexError):
    uptime_seconds = None

  return {
    "cpu_percent": None if cpu is None else round(cpu, 1),
    "gpu_percent": gpu,
    "memory_free_gb": _mem_info_gb()[1],
    "disk_total_gb": round(disk.total / (1024 ** 3), 1),
    "disk_used_gb": round(disk.used / (1024 ** 3), 1),
    "disk_free_gb": round(disk.free / (1024 ** 3), 1),
    "disk_used_percent": disk_used_pct,
    "temps": temps,
    "load_average": None if load_avg is None else [round(value, 2) for value in load_avg],
    "uptime_seconds": uptime_seconds,
  }


def _server_monitoring_requested(text: str) -> bool:
  q = (text or "").lower().strip()
  if not q:
    return False

  has_stats_words = bool(re.search(r"\b(cpu|disk|storage|temp|temperature|load|uptime|usage|utilization|health|stats?)\b", q))
  has_monitoring_intent = bool(re.search(r"\b(what|how|show|check|watch|monitor|current|right now|status)\b", q))
  has_server_context = bool(re.search(r"\b(server|linux|machine|host|system|this box|this server)\b", q))
  return has_stats_words and (has_monitoring_intent or has_server_context)


def _linux_stats_requested(text: str) -> bool:
  q = (text or "").lower().strip()
  return bool(re.search(r"\b(show|give|tell|check|get)\b", q) and re.search(r"\blinux\s+stats?\b", q))


def _model_is_too_large_for_host(model_name: str) -> str | None:
  model = (model_name or "").lower()
  if "70b" not in model:
    return None

  total_gb, available_gb = _mem_info_gb()
  if total_gb is None:
    return "This machine does not report enough memory information to safely run a 70B model. Use 8B instead."

  if total_gb < 48 or (available_gb is not None and available_gb < 20):
    avail_text = f"{available_gb} GB available" if available_gb is not None else "unknown available memory"
    return (
      f"{model_name} is too large for this host ({total_gb} GB RAM, {avail_text}). "
      "It will stall or take an impractically long time to respond. Use 8B instead."
    )

  return None


async def _server_stats_context() -> str:
  stats = await _server_stats()
  temps = stats.get("temps") or []
  temp_lines = ", ".join(f"{item['label']}: {item['celsius']} C" for item in temps) if temps else "unavailable"
  load_avg = stats.get("load_average")
  load_text = ", ".join(str(value) for value in load_avg) if load_avg else "unavailable"
  uptime_seconds = stats.get("uptime_seconds")
  uptime_text = f"{uptime_seconds} seconds" if uptime_seconds is not None else "unavailable"

  return (
    "Current Linux server stats for this host. Use these facts directly when answering the user:\n"
    f"- CPU usage: {stats.get('cpu_percent', 'unavailable')}%\n"
    f"- GPU usage: {stats.get('gpu_percent', 'unavailable')}%\n"
    f"- Free memory: {stats.get('memory_free_gb', 'unavailable')} GB\n"
    f"- Disk usage on /: {stats.get('disk_used_percent', 'unavailable')}% used "
    f"({stats.get('disk_used_gb', 'unavailable')} GB used / {stats.get('disk_total_gb', 'unavailable')} GB total)\n"
    f"- Temperatures: {temp_lines}\n"
    f"- Load average: {load_text}\n"
    f"- Uptime: {uptime_text}\n"
    "If a value is unavailable, say so plainly instead of guessing."
  )


async def _linux_stats_reply() -> str:
  stats = await _server_stats()
  cpu = stats.get("cpu_percent")
  gpu = stats.get("gpu_percent")
  memory = stats.get("memory_free_gb")
  cpu_text = "unavailable" if cpu is None else f"{cpu:.1f}%"
  gpu_text = "unavailable" if gpu is None else f"{gpu:.1f}%"
  memory_text = "unavailable" if memory is None else f"{memory:.1f} GB"
  return f"Linux stats: CPU {cpu_text}, GPU {gpu_text}, free memory {memory_text}."
