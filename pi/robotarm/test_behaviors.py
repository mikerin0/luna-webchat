"""Lightweight tests for the social behavior + object handoff layer in brain.py.

These tests run without real hardware by monkeypatching the low-level
primitives (servo power, ultrasonic, pose execution, gripper) that the
behavior layer calls. Run directly with:

    python3 test_behaviors.py

or under unittest/pytest discovery.
"""

import unittest
from unittest.mock import patch

import brain


class GestureMappingTests(unittest.TestCase):
    def setUp(self):
        brain._behavior_state = "idle"
        if brain._behavior_lock.locked():
            brain._behavior_lock.release()

    @patch("brain.send_multi_servo_command", return_value=True)
    @patch("brain.read_ultrasonic_cm", return_value=None)
    @patch("brain.get_servo_power_status", return_value=True)
    def test_known_intent_returns_valid_clip(self, _power, _dist, _send):
        result = brain.gesture("greet", energy=0.5, duration=1.0)
        self.assertTrue(result["ok"])
        self.assertEqual(result["details"]["clip"], "GESTURE_WAVE_SMALL")
        self.assertIn(result["details"]["clip"], brain.gesture_map.GESTURE_CLIPS)

    @patch("brain.send_multi_servo_command", return_value=True)
    @patch("brain.read_ultrasonic_cm", return_value=None)
    @patch("brain.get_servo_power_status", return_value=True)
    def test_unknown_intent_rejected(self, _power, _dist, _send):
        result = brain.gesture("juggle")
        self.assertFalse(result["ok"])
        self.assertIn("unknown intent", result["error"])

    @patch("brain.send_multi_servo_command", return_value=True)
    @patch("brain.read_ultrasonic_cm", return_value=None)
    @patch("brain.get_servo_power_status", return_value=True)
    def test_explain_alternates_between_clips(self, _power, _dist, _send):
        first = brain.gesture("explain")["details"]["clip"]
        second = brain.gesture("explain")["details"]["clip"]
        self.assertNotEqual(first, second)
        self.assertEqual({first, second}, {"GESTURE_EXPLAIN_A", "GESTURE_EXPLAIN_B"})


class BehaviorDispatchTests(unittest.TestCase):
    def setUp(self):
        brain._behavior_state = "idle"
        if brain._behavior_lock.locked():
            brain._behavior_lock.release()

    @patch("brain.run_pose", return_value=True)
    @patch("brain.read_ultrasonic_cm", return_value=None)
    @patch("brain.get_servo_power_status", return_value=True)
    def test_valid_behavior_runs_pose(self, _power, _dist, mock_run_pose):
        result = brain.run_behavior("offer_near")
        self.assertTrue(result["ok"])
        mock_run_pose.assert_called_once_with("offer_near")

    def test_unknown_behavior_rejected(self):
        result = brain.run_behavior("fly_away")
        self.assertFalse(result["ok"])
        self.assertIn("unknown behavior", result["error"])

    @patch("brain.run_pose", return_value=True)
    @patch("brain.read_ultrasonic_cm", return_value=1.0)
    @patch("brain.get_servo_power_status", return_value=True)
    def test_obstacle_too_close_blocks_motion(self, _power, _dist, mock_run_pose):
        result = brain.run_behavior("idle_safe")
        self.assertFalse(result["ok"])
        self.assertIn("obstacle", result["error"])
        mock_run_pose.assert_not_called()

    @patch("brain.get_servo_power_status", return_value=False)
    def test_power_off_blocks_motion(self, _power):
        result = brain.run_behavior("idle_safe")
        self.assertFalse(result["ok"])
        self.assertIn("power", result["error"])


class ReceiveObjectTests(unittest.TestCase):
    def setUp(self):
        brain._behavior_state = "idle"
        if brain._behavior_lock.locked():
            brain._behavior_lock.release()

    @patch("brain.run_pose", return_value=True)
    @patch("brain.open_claw", return_value=True)
    @patch("brain.read_ultrasonic_cm", return_value=None)
    @patch("brain.get_servo_power_status", return_value=True)
    def test_receive_object_times_out_when_no_candidate(self, _power, _dist, _open, _pose):
        result = brain.receive_object(timeout_s=0.2, retries=0)
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "grasp not confirmed")
        self.assertEqual(result["details"].get("reason"), "no object candidate detected")

    @patch("brain.run_pose", return_value=True)
    @patch("brain.did_last_close_stop_on_switch", return_value=True)
    @patch("brain.close_claw", return_value=True)
    @patch("brain.open_claw", return_value=True)
    @patch("brain.read_ultrasonic_cm", return_value=5.0)
    @patch("brain.get_servo_power_status", return_value=True)
    def test_receive_object_succeeds_on_switch_confirmation(self, _power, _dist, _open, _close, _switch, _pose):
        result = brain.receive_object(timeout_s=1.0, retries=0)
        self.assertTrue(result["ok"])
        self.assertTrue(result["details"]["switch_confirmed"])
        self.assertEqual(brain.get_behavior_state(), "holding_object")


class BusyArbitrationTests(unittest.TestCase):
    def setUp(self):
        brain._behavior_state = "idle"
        if brain._behavior_lock.locked():
            brain._behavior_lock.release()

    def test_run_behavior_rejects_when_locked(self):
        brain._behavior_lock.acquire()
        try:
            result = brain.run_behavior("idle_safe")
            self.assertFalse(result["ok"])
            self.assertEqual(result["error"], "busy")
        finally:
            brain._behavior_lock.release()

    def test_gesture_rejects_when_locked(self):
        brain._behavior_lock.acquire()
        try:
            result = brain.gesture("greet")
            self.assertFalse(result["ok"])
            self.assertEqual(result["error"], "busy")
        finally:
            brain._behavior_lock.release()

    def test_receive_object_rejects_when_locked(self):
        brain._behavior_lock.acquire()
        try:
            result = brain.receive_object(timeout_s=0.1)
            self.assertFalse(result["ok"])
            self.assertEqual(result["error"], "busy")
        finally:
            brain._behavior_lock.release()


if __name__ == "__main__":
    unittest.main()
