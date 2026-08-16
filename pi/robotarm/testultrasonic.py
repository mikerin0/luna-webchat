import argparse
import statistics
import time

DEFAULT_TRIG = 23  # BCM 23, physical pin 16
DEFAULT_ECHO = 24  # BCM 24, physical pin 18


def _measure_rpigpio(trig: int, echo: int, timeout_s: float = 0.03) -> float | None:
    import RPi.GPIO as GPIO

    # 10 microsecond trigger pulse.
    GPIO.output(trig, True)
    time.sleep(0.00001)
    GPIO.output(trig, False)

    t0 = time.time()
    while GPIO.input(echo) == 0:
        if time.time() - t0 > timeout_s:
            return None
    pulse_start = time.time()

    while GPIO.input(echo) == 1:
        if time.time() - pulse_start > timeout_s:
            return None
    pulse_end = time.time()

    pulse_duration = pulse_end - pulse_start
    return round(pulse_duration * 17150.0, 2)


def run_rpigpio(trig: int, echo: int, samples: int, interval_s: float):
    import RPi.GPIO as GPIO

    GPIO.setmode(GPIO.BCM)
    GPIO.setwarnings(False)
    GPIO.setup(trig, GPIO.OUT)
    GPIO.setup(echo, GPIO.IN)
    GPIO.output(trig, False)
    time.sleep(0.1)

    distances = []
    timeouts = 0
    try:
        for idx in range(samples):
            # Detect a likely wiring/floating issue before each pulse.
            if GPIO.input(echo) == 1:
                print(f"[{idx + 1}/{samples}] echo HIGH before trigger (possible stuck-high or pin conflict)")
            d = _measure_rpigpio(trig, echo)
            if d is None:
                timeouts += 1
                print(f"[{idx + 1}/{samples}] timeout waiting for echo")
            else:
                distances.append(d)
                print(f"[{idx + 1}/{samples}] {d:.2f} cm")
            time.sleep(interval_s)
    finally:
        GPIO.cleanup()

    return distances, timeouts


def _measure_lgpio(h: int, trig: int, echo: int, timeout_s: float = 0.03) -> float | None:
    import lgpio

    lgpio.gpio_write(h, trig, 1)
    time.sleep(0.00001)
    lgpio.gpio_write(h, trig, 0)

    t0 = time.time()
    while lgpio.gpio_read(h, echo) == 0:
        if time.time() - t0 > timeout_s:
            return None
    pulse_start = time.time()

    while lgpio.gpio_read(h, echo) == 1:
        if time.time() - pulse_start > timeout_s:
            return None
    pulse_end = time.time()

    pulse_duration = pulse_end - pulse_start
    return round(pulse_duration * 17150.0, 2)


def run_lgpio(trig: int, echo: int, samples: int, interval_s: float):
    import lgpio

    h = lgpio.gpiochip_open(0)
    distances = []
    timeouts = 0
    try:
        lgpio.gpio_claim_output(h, trig)
        lgpio.gpio_claim_input(h, echo)
        lgpio.gpio_write(h, trig, 0)
        time.sleep(0.1)

        for idx in range(samples):
            if lgpio.gpio_read(h, echo) == 1:
                print(f"[{idx + 1}/{samples}] echo HIGH before trigger (possible stuck-high or pin conflict)")
            d = _measure_lgpio(h, trig, echo)
            if d is None:
                timeouts += 1
                print(f"[{idx + 1}/{samples}] timeout waiting for echo")
            else:
                distances.append(d)
                print(f"[{idx + 1}/{samples}] {d:.2f} cm")
            time.sleep(interval_s)
    finally:
        try:
            lgpio.gpio_free(h, trig)
        except Exception:
            pass
        try:
            lgpio.gpio_free(h, echo)
        except Exception:
            pass
        lgpio.gpiochip_close(h)

    return distances, timeouts


def run_gpiozero(trig: int, echo: int, samples: int, interval_s: float):
    from gpiozero import DistanceSensor

    sensor = DistanceSensor(echo=echo, trigger=trig, max_distance=4.0)
    distances = []
    try:
        for idx in range(samples):
            d = round(sensor.distance * 100.0, 2)
            distances.append(d)
            print(f"[{idx + 1}/{samples}] {d:.2f} cm")
            time.sleep(interval_s)
    finally:
        sensor.close()

    return distances, 0


def main():
    parser = argparse.ArgumentParser(description="Ultrasonic wiring/hardware test")
    parser.add_argument("--samples", type=int, default=20, help="Number of readings")
    parser.add_argument("--interval", type=float, default=0.25, help="Seconds between readings")
    parser.add_argument("--trig", type=int, default=DEFAULT_TRIG, help="TRIG pin (BCM numbering)")
    parser.add_argument("--echo", type=int, default=DEFAULT_ECHO, help="ECHO pin (BCM numbering)")
    parser.add_argument(
        "--backend",
        choices=["auto", "lgpio", "gpiozero", "rpigpio"],
        default="auto",
        help="GPIO backend to use",
    )
    args = parser.parse_args()
    trig = int(args.trig)
    echo = int(args.echo)

    print("Ultrasonic test (HC-SR04 style)")
    print(f"TRIG BCM {trig}, ECHO BCM {echo}")
    print("Important: ECHO must be level-shifted to 3.3V before Raspberry Pi GPIO.")

    backends = [args.backend] if args.backend != "auto" else ["lgpio", "gpiozero", "rpigpio"]
    last_exc = None
    distances = []
    timeouts = 0

    for backend in backends:
        try:
            print(f"Using backend: {backend}")
            if backend == "lgpio":
                distances, timeouts = run_lgpio(trig, echo, args.samples, args.interval)
            elif backend == "gpiozero":
                distances, timeouts = run_gpiozero(trig, echo, args.samples, args.interval)
            else:
                distances, timeouts = run_rpigpio(trig, echo, args.samples, args.interval)
            break
        except Exception as exc:
            last_exc = exc
            print(f"Backend '{backend}' failed: {exc}")
            distances = []
            timeouts = 0
    else:
        print("All GPIO backends failed.")
        print("If running over SSH/container, run directly on the Pi OS host.")
        if last_exc is not None:
            print(f"Last error: {last_exc}")
        raise SystemExit(1)

    if not distances:
        print("No valid echo readings captured.")
        print("Check: VCC 5V, GND, TRIG/ECHO pins, and ECHO level shifting.")
        if timeouts:
            print(f"Observed {timeouts} echo timeouts.")
        raise SystemExit(2)

    mn = min(distances)
    mx = max(distances)
    avg = statistics.mean(distances)
    print("\nSummary")
    print(f"Readings: {len(distances)} valid")
    print(f"Min/Avg/Max: {mn:.2f} / {avg:.2f} / {mx:.2f} cm")
    if timeouts:
        print(f"Timeouts: {timeouts}")


if __name__ == "__main__":
    main()
