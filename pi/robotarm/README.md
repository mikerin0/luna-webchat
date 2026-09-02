# Robot Arm Project

- main.py: Main entry point
- brain.py: Controls robot arm movement
- poses.py: Stores absolute poses and direct servo commands
- gesture_map.py: Gesture clip definitions and intent-to-gesture mapping
- ai.py: Runs video and Hailo overlays (high cam only)
- gui.py: GUI with two windows (sliders, buttons)
- params.py: Global settable parameters
- headless_control_server.py: HTTP control surface (mute, LCD, arm power, social behaviors)
- test_behaviors.py: Mocked-hardware tests for the social behavior layer

## Initial Setup
- High cam video comes on by default (upper right)
- GUI windows on the left (title + exit button to start)
- Uses Hailo AI overlays for pose

Refer to the backups directory for video/pipeline setup examples.

## Social behavior + object handoff system

AI callers (Luna, voice assistant, etc.) control the arm only at the
**behavior level** — no raw per-servo commands are exposed over HTTP. All
motion goes through `brain.py`'s behavior layer, which enforces safety checks
(servo power on, no obstacle too close) before moving, and serializes
behaviors so only one runs at a time (`409 busy` if another is in progress —
requests are rejected outright rather than queued, so a caller always gets an
immediate, unambiguous answer instead of waiting on an unknown queue).

States: `idle`, `converse_gesture`, `receive_object`, `holding_object`,
`return_object`, `recover`.

### `POST /behavior/run`

Move to a named safe pose: `IDLE_SAFE`, `OFFER_NEAR`, `OFFER_FAR`,
`HOLD_CENTER`, `RETRACT_SAFE` (case-insensitive).

```bash
curl -X POST http://<pi-host>:8004/behavior/run \
  -H "Content-Type: application/json" \
  -d '{"name": "OFFER_NEAR"}'
```

```json
{"ok": true, "state": "idle", "details": {"pose": "offer_near"}, "error": null}
```

### `POST /gesture`

Play a short conversational gesture mapped from an intent: `greet`,
`explain` (alternates between two clips), `think`, `confirm`, `deny`,
`apology`. `energy` (default `0.4`) scales amplitude, clamped to
`BEHAVIOR_GESTURE_MIN_ENERGY`/`BEHAVIOR_GESTURE_MAX_ENERGY` in `config.py`.
`duration` (default `1.5`s) scales playback speed.

```bash
curl -X POST http://<pi-host>:8004/gesture \
  -H "Content-Type: application/json" \
  -d '{"intent": "greet", "energy": 0.5, "duration": 1.2}'
```

```json
{"ok": true, "state": "idle", "details": {"clip": "GESTURE_WAVE_SMALL", "energy": 0.5}, "error": null}
```

### `POST /behavior/receive_object`

Offer the gripper, wait for an object candidate, then grasp it with a
two-stage close. Grasp success requires either a confirmed gripper
microswitch trigger, or at least 2-of-3 signals (switch, ultrasonic
distance delta, optional vision flag from `table_detect` if that module is
available). On failure the arm reopens, retries up to `retries` more times,
then recovers to `IDLE_SAFE`.

```bash
curl -X POST http://<pi-host>:8004/behavior/receive_object \
  -H "Content-Type: application/json" \
  -d '{"timeout_s": 8, "retries": 1}'
```

```json
{"ok": true, "state": "holding_object", "details": {"switch_confirmed": true, "ultrasonic_delta_signal": false, "vision_signal": false, "votes": 1}, "error": null}
```

### `POST /behavior/return_object`

Offer a held object back toward the user, open the gripper, wait briefly for
a release signal (ultrasonic distance increase), then retract to
`RETRACT_SAFE` and settle to `IDLE_SAFE`.

```bash
curl -X POST http://<pi-host>:8004/behavior/return_object \
  -H "Content-Type: application/json" \
  -d '{"timeout_s": 5}'
```

```json
{"ok": true, "state": "idle", "details": {"release_confirmed": true, "offer_pose": "offer_near"}, "error": null}
```

### Response contract

All four endpoints return:

```json
{ "ok": true|false, "state": "...", "details": {...}, "error": null | "message" }
```

HTTP status is `409` only when another behavior is currently running
(`error: "busy"`); other validation/domain failures (unknown behavior name,
unknown gesture intent, grasp not confirmed, etc.) are returned as `200`
with `ok: false` so the caller can inspect `details`/`error` directly rather
than branching on HTTP status codes.

### Safety notes

- Every behavior checks servo power is on and that the ultrasonic sensor
  does not read an obstacle closer than `BEHAVIOR_OBSTACLE_STOP_CM` before
  starting to move; if either check fails, the arm does not move.
- Gesture amplitude is clamped to `BEHAVIOR_GESTURE_MIN_ENERGY`/`BEHAVIOR_GESTURE_MAX_ENERGY`
  and per-frame timing is clamped to 80-2000ms regardless of the requested
  `duration`.
- Behaviors are serialized with a single lock; overlapping requests are
  rejected with `busy` rather than queued.
- **TODO (hardware calibration):** `OFFER_NEAR`/`OFFER_FAR`/`HOLD_CENTER`/
  `RETRACT_SAFE` servo positions in `poses.py`, and `BEHAVIOR_HANDOFF_NEAR_CM`/
  `BEHAVIOR_HANDOFF_DELTA_CM`/`BEHAVIOR_OBSTACLE_STOP_CM` in `config.py`, are
  conservative starting points and have not been tuned against the physical
  arm's real mounting height, reach envelope, or handoff zone. Gesture clip
  amplitudes/timings in `gesture_map.py` are similarly untuned starting
  points, not measured choreography.
- Continuous mid-motion obstacle aborts are not implemented — the obstacle
  check runs once at the start of each behavior, not throughout a pose's
  multi-second motion. A full interrupt-capable motion loop would be a
  larger change to `run_pose`/`send_servo_command` outside this change's
  scope.

### Tests

```bash
python3 -m unittest test_behaviors -v
```

Tests mock the low-level primitives (`get_servo_power_status`,
`read_ultrasonic_cm`, `run_pose`, `open_claw`, `close_claw`,
`send_multi_servo_command`) so they run without real hardware, covering
gesture-mapping validity, behavior dispatch, the `receive_object` timeout
path, and busy-state arbitration.

