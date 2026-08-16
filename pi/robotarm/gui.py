# gui.py
# Controls sliders and buttons (two windows: one for sliders, one for buttons)

import tkinter as tk
import json
import os
import time

# Integrate with ai.py camera switching
import ai
import brain
import config
import lcd
import voice_assistant
import threading

# Track the display thread globally
_display_thread = None
_display_thread_running = False
_current_mode = None
_camera_switch_lock = threading.Lock()
_slider_window_ref = None
_button_window_ref = None
_poses_window_ref = None
_GUI_STATE_PATH = os.path.join(os.path.dirname(__file__), "window_state.json")
# Delegate all ultrasonic GPIO access to brain.py which holds the shared
# _ultrasonic_hw_lock so gui polling and smart_take never claim pins at the same time.
def _get_ultrasonic_cache(max_age_s: float = 1.5):
    return brain.get_ultrasonic_cache(max_age_s=max_age_s)


def _read_ultrasonic_once():
    return brain.read_ultrasonic_cm()

def _load_gui_state():
    try:
        with open(_GUI_STATE_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            return data
    except Exception:
        pass
    return {}


def _save_gui_state(button_win=None, slider_win=None, poses_win=None):
    state = _load_gui_state()
    try:
        if button_win is not None:
            state["button_geometry"] = str(button_win.geometry())
    except Exception:
        pass
    try:
        if slider_win is not None:
            state["slider_geometry"] = str(slider_win.geometry())
    except Exception:
        pass
    try:
        if poses_win is not None:
            state["poses_geometry"] = str(poses_win.geometry())
    except Exception:
        pass
    try:
        with open(_GUI_STATE_PATH, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2)
    except Exception as e:
        print(f"[GUI] Failed saving window state: {e}")


def _ensure_display_thread():
    global _display_thread, _display_thread_running
    if _display_thread_running and _display_thread is not None and _display_thread.is_alive():
        return

    ai.clear_display_stop()

    def display_loop():
        global _display_thread_running, _display_thread
        _display_thread_running = True
        try:
            ai.run_high_cam_window_loop("Camera Preview")
        finally:
            _display_thread_running = False
            _display_thread = None

    _display_thread = threading.Thread(target=display_loop, daemon=True)
    _display_thread.start()

def switch_camera(mode):
    global _display_thread, _display_thread_running, _current_mode
    with _camera_switch_lock:
        print(f"[GUI] Switching camera to: {mode}")
        if mode == _current_mode:
            print(f"[GUI] Camera already in mode: {mode}")
            try:
                lcd.set_camera_mode(mode)
            except Exception:
                pass
            return True
        if mode == "HIGH_CAM":
            if not ai.start_high_cam():
                print("[GUI] Failed to start HIGH_CAM safely; previous pipeline still stopping")
                return False
            ai.reset_auto_switch_state_for_mode("HIGH_CAM")
            _current_mode = mode
            try:
                brain.say("Top camera")
            except Exception:
                pass
            try:
                lcd.set_camera_mode(mode)
            except Exception:
                pass
            _ensure_display_thread()
            return True
        elif mode == "TABLE_CAM":
            if not ai.start_table_cam():
                print("[GUI] Failed to start TABLE_CAM safely; previous pipeline still stopping")
                return False
            ai.reset_auto_switch_state_for_mode("TABLE_CAM")
            _current_mode = mode
            try:
                brain.say("Table camera")
            except Exception:
                pass
            try:
                lcd.set_camera_mode(mode)
            except Exception:
                pass
            _ensure_display_thread()
            return True
        return False

class ButtonWindow(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Robot Arm Controls")
        state = _load_gui_state()
        self.geometry(state.get("button_geometry", "360x760+0+0"))
        self.minsize(360, 560)
        self.attributes("-topmost", True)
        self.after(250, lambda: self.attributes("-topmost", False))
        self._tracking_active = False
        self._tracking_transition_in_progress = False
        self._preview_visible = True
        self._claw_cycle_active = False
        self._claw_cycle_stop = threading.Event()
        self._claw_cycle_thread = None
        self._face_before_claw = None
        self._smart_take_running = False
        self._auto_take_active = False
        self._auto_take_stop = threading.Event()
        self._auto_take_thread = None
        self._auto_take_sensor = None
        self._auto_take_busy = False
        self._gesture_enabled = bool(getattr(config, "GESTURE_EVENTS_ENABLED", True))
        self._gesture_hold_started_t = None
        self._gesture_top_lock_until_t = 0.0
        self._gesture_done_pending = False
        self._gesture_done_end_t = 0.0
        self._gesture_followup_triggered = False
        self._gesture_engine_logged = False

        self._high_btn = tk.Button(
            self,
            text="Top Cam",
            command=lambda: self._set_camera_mode("HIGH_CAM"),
            width=20,
        )
        self._high_btn.pack(pady=5)

        self._table_btn = tk.Button(
            self,
            text="Table Cam",
            command=lambda: self._set_camera_mode("TABLE_CAM"),
            width=20,
        )
        self._table_btn.pack(pady=5)

        self._track_btn = tk.Button(
            self,
            text="Track Wrist",
            command=self._toggle_tracking,
            width=20,
        )
        self._track_btn.pack(pady=5)

        self._wake_btn = tk.Button(
            self,
            text="Wake Up",
            command=self._wake_up_conversation_mode,
            width=20,
            bg="#81c784",
        )
        self._wake_btn.pack(pady=5)

        self._sleep_btn = tk.Button(
            self,
            text="Sleep",
            command=self._sleep_arm,
            width=20,
            bg="#90a4ae",
            fg="white",
        )
        self._sleep_btn.pack(pady=5)

        self._preview_btn = tk.Button(
            self,
            text="Hide Preview",
            command=self._toggle_preview,
            width=20,
        )
        self._preview_btn.pack(pady=5)

        self._mic_btn = tk.Button(
            self,
            text="Mute Mic",
            command=self._toggle_mic_mute,
            width=20,
        )
        self._mic_btn.pack(pady=5)

        self._refresh_camera_buttons()
        self._refresh_mic_button()
        self.after(200, self._poll_auto_camera_switch)

        # Animated face is now the only display mode (no toggle button needed)

        self._poses_btn = tk.Button(
            self,
            text="Poses",
            command=self._open_poses,
            width=20,
        )
        self._poses_btn.pack(pady=5)

        self._reload_poses_btn = tk.Button(
            self,
            text="Reload Poses",
            command=self._reload_poses,
            width=20,
        )
        self._reload_poses_btn.pack(pady=5)

        self._open_claw_btn = tk.Button(
            self,
            text="Open Claw",
            command=self._open_claw,
            width=20,
        )
        self._open_claw_btn.pack(pady=5)

        self._close_claw_btn = tk.Button(
            self,
            text="Close Claw",
            command=self._close_claw,
            width=20,
        )
        self._close_claw_btn.pack(pady=5)

        self._take_btn = tk.Button(
            self,
            text="Take",
            command=lambda: self._run_named_pose_dialog("take"),
            width=20,
            bg="#ffd54f",
        )
        self._take_btn.pack(pady=5)

        self._table_take_btn = tk.Button(
            self,
            text="Table Take",
            command=lambda: self._run_named_pose_dialog("table-take"),
            width=20,
            bg="#ffb74d",
        )
        self._table_take_btn.pack(pady=5)

        self._smart_take_btn = tk.Button(
            self,
            text="Smart Take",
            command=self._run_smart_table_take,
            width=20,
            bg="#a5d6a7",
        )
        self._smart_take_btn.pack(pady=5)

        self._put_down_btn = tk.Button(
            self,
            text="Put Down",
            command=self._put_down,
            width=20,
            bg="#90caf9",
        )
        self._put_down_btn.pack(pady=5)

        self._claw_cycle_btn = tk.Button(
            self,
            text="Claw Cycle (OFF)",
            command=self._toggle_claw_cycle,
            width=20,
            bg="#d9d9d9",
            fg="black",
        )
        self._claw_cycle_btn.pack(pady=5)

        self._auto_take_btn = tk.Button(
            self,
            text="Auto-Take (OFF)",
            command=self._toggle_auto_take,
            width=20,
            bg="#d9d9d9",
            fg="black",
        )
        self._auto_take_btn.pack(pady=5)

        self._ai_tools_btn = tk.Button(
            self,
            text="AI Tools (OFF)",
            command=self._toggle_ai_tools,
            width=20,
            bg="#d9d9d9",
            fg="black",
        )
        self._ai_tools_btn.pack(pady=5)

        self._exit_btn = tk.Button(self, text="Exit Program", command=self._on_exit)
        self._exit_btn.pack(pady=10)

        self._poses_win = None

        # Start animated face mode
        try:
            import lcd
            if not lcd.is_animated_face_mode():
                lcd.start_animated_face_mode()
        except Exception as e:
            print(f"[GUI] Failed to start animated face mode: {e}")

        self.after(10, self._ensure_controls_visible)
        if bool(getattr(config, "AUTO_TAKE_ENABLED_STARTUP", False)):
            self.after(50, self._toggle_auto_take)

    def _ensure_controls_visible(self):
        # Expand the window if needed so all packed controls are visible.
        self.update_idletasks()
        req_w = int(self.winfo_reqwidth()) + 16
        req_h = int(self.winfo_reqheight()) + 16
        min_w = max(360, req_w)
        min_h = max(560, req_h)
        self.minsize(min_w, min_h)

        cur_w = int(self.winfo_width())
        cur_h = int(self.winfo_height())
        if cur_w < min_w or cur_h < min_h:
            x = int(self.winfo_x())
            y = int(self.winfo_y())
            self.geometry(f"{max(cur_w, min_w)}x{max(cur_h, min_h)}+{x}+{y}")

    def _set_camera_mode(self, mode):
        if switch_camera(mode):
            self._refresh_camera_buttons()

    def _refresh_camera_buttons(self):
        active_bg = "#4caf50"
        idle_bg = "#d9d9d9"
        active_fg = "white"
        idle_fg = "black"
        if _current_mode == "HIGH_CAM":
            self._high_btn.config(bg=active_bg, fg=active_fg)
            self._table_btn.config(bg=idle_bg, fg=idle_fg)
        elif _current_mode == "TABLE_CAM":
            self._high_btn.config(bg=idle_bg, fg=idle_fg)
            self._table_btn.config(bg=active_bg, fg=active_fg)
        else:
            self._high_btn.config(bg=idle_bg, fg=idle_fg)
            self._table_btn.config(bg=idle_bg, fg=idle_fg)

    def _toggle_tracking(self):
        if self._tracking_transition_in_progress:
            print("[GUI] Track transition already in progress")
            return
        self._tracking_transition_in_progress = True
        self._track_btn.config(state=tk.DISABLED)
        self._sleep_btn.config(state=tk.DISABLED)
        self._wake_btn.config(state=tk.DISABLED)
        self._tracking_active = not self._tracking_active
        if self._tracking_active:
            self._track_btn.config(text="Stop Tracking ●", bg="#4caf50", fg="white")
            target = brain.enable_wrist_tracking_mode
        else:
            self._track_btn.config(text="Track Wrist", bg="#d9d9d9", fg="black")
            target = brain.stop_wrist_tracking_to_home
        # Run mode transition in background so GUI stays responsive.
        threading.Thread(target=self._run_track_transition, args=(target,), daemon=True).start()

    def _run_track_transition(self, target):
        ok = False
        try:
            ok = bool(target())
        except Exception as e:
            print(f"[GUI] Track transition failed: {e}")
            ok = False

        def _finish():
            if not ok:
                # Revert button state if transition failed.
                self._tracking_active = not self._tracking_active
                if self._tracking_active:
                    self._track_btn.config(text="Stop Tracking ●", bg="#4caf50", fg="white")
                else:
                    self._track_btn.config(text="Track Wrist", bg="#d9d9d9", fg="black")
            elif not self._tracking_active:
                # When tracking is stopped, show sleeping face.
                lcd.show_face("sleeping")
            else:
                # When tracking is enabled, animated face continues
                # (no face change needed, animated face already active)
                if _current_mode in ("HIGH_CAM", "TABLE_CAM"):
                    ai.reset_auto_switch_state_for_mode(_current_mode)
            self._track_btn.config(state=tk.NORMAL)
            self._sleep_btn.config(state=tk.NORMAL)
            self._wake_btn.config(state=tk.NORMAL)
            self._tracking_transition_in_progress = False

        self.after(0, _finish)

    def _on_exit(self):
        # Move to sleep and cut servo power before anything else.
        try:
            self._stop_auto_take_ui()
            self._stop_claw_cycle_ui()
            print("[GUI] Starting sleep sequence on exit...")
            ok = bool(brain.sleep_arm())
            if not ok:
                print("[GUI] sleep_arm reported failure; forcing sleep fallback")
                try:
                    brain.set_wrist_tracking_enabled(False)
                    brain.set_live_servo_follow_enabled(False)
                except Exception:
                    pass
                try:
                    brain.set_servo_power(True)
                    time.sleep(1.5)  # Extra time for relay + controller boot on fallback
                    result = brain.run_pose("sleep")
                    print(f"[GUI] Sleep fallback pose result: {result}")
                    time.sleep(0.4)
                except Exception as e:
                    print(f"[GUI] Sleep fallback failed: {e}")
                finally:
                    try:
                        brain.set_servo_power(False)
                    except Exception:
                        pass
            else:
                print("[GUI] Sleep sequence completed successfully")
            self._tracking_active = False
            try:
                lcd.stop_animated_face_mode()
            except Exception:
                pass
            lcd.show_face("sleeping")
            lcd.set_camera_mode("")
        except Exception as e:
            print(f"[GUI] Error while disabling tracking on exit: {e}")
            try:
                brain.set_servo_power(False)
            except Exception:
                pass

        # Stop preview/camera so native pipeline teardown is orderly.
        ai.request_display_stop()
        try:
            ai.stop_high_cam()
        except Exception as e:
            print(f"[GUI] Error while stopping camera on exit: {e}")

        global _display_thread
        try:
            if _display_thread is not None and _display_thread.is_alive():
                _display_thread.join(timeout=2.0)
        except Exception:
            pass

        try:
            brain.stop_crestron_server()
        except Exception as e:
            print(f"[GUI] Error while stopping Crestron server on exit: {e}")

        _save_gui_state(self, _slider_window_ref, self._poses_win)
        self.destroy()

    def _open_poses(self):
        if self._poses_win is not None:
            try:
                self._poses_win.lift()
                self._poses_win.focus_force()
                return
            except Exception:
                pass
        self._poses_win = PosesWindow(self, owner=self)



    def _run_named_pose_dialog(self, pose_name: str):
        def _run():
            ok = False
            try:
                brain.set_servo_power(True)
                time.sleep(0.5)
                ok = bool(brain.run_pose(pose_name))
            except Exception as e:
                print(f"[GUI] Pose '{pose_name}' failed: {e}")
                ok = False
            print(f"[GUI] Pose '{pose_name}' {'completed' if ok else 'failed'}")

        threading.Thread(target=_run, daemon=True).start()

    def _run_smart_table_take(self):
        """Switch to table cam, enable detection, run smart_table_take, restore state."""
        if self._smart_take_running:
            print("[GUI] Smart Take already running")
            return
        self._smart_take_running = True
        btn = getattr(self, "_smart_take_btn", None)

        def _status(msg: str):
            print(f"[GUI] SmartTake: {msg}")
            if btn is not None:
                try:
                    btn.config(text=f"Smart Take: {msg[:18]}")
                except Exception:
                    pass

        def _run():
            try:
                if btn is not None:
                    try:
                        btn.config(state=tk.DISABLED)
                    except Exception:
                        pass
                # Switch to table cam so table_detect gets frames
                if not switch_camera("TABLE_CAM"):
                    raise RuntimeError("Failed to switch to TABLE_CAM")
                try:
                    lcd.set_camera_mode("SMART TAKE")
                except Exception:
                    pass
                time.sleep(0.8)   # give pipeline time to warm up
                result = brain.smart_table_take(status_cb=_status)
                if btn is not None:
                    try:
                        lbl = "Smart Take ✓" if result else "Smart Take ✗"
                        btn.config(text=lbl)
                        # Restore after 3 s
                        btn.after(3000, lambda: btn.config(text="Smart Take"))
                    except Exception:
                        pass
            except Exception as e:
                print(f"[GUI] smart_table_take error: {e}")
                if btn is not None:
                    try:
                        btn.config(text="Smart Take")
                    except Exception:
                        pass
            finally:
                try:
                    lcd.set_camera_mode(_current_mode or "")
                except Exception:
                    pass
                self._smart_take_running = False
                if btn is not None:
                    try:
                        btn.config(state=tk.NORMAL)
                    except Exception:
                        pass

        threading.Thread(target=_run, daemon=True).start()

    def _reload_poses(self):
        ok = False
        try:
            ok = bool(brain.reload_poses())
        except Exception as e:
            print(f"[GUI] Reload poses failed: {e}")
            ok = False
        print(f"[GUI] Reload poses {'completed' if ok else 'failed'}")

    def _toggle_preview(self):
        global _display_thread, _display_thread_running
        if self._preview_visible:
            ai.set_preview_enabled(False)
            print("[GUI] Preview hidden")
            self._preview_visible = False
            self._preview_btn.config(text="Show Preview")
        else:
            self._preview_visible = True
            self._preview_btn.config(text="Hide Preview")
            ai.set_preview_enabled(True)
            print("[GUI] Preview shown")
            # Ensure preview loop thread exists if a camera mode is active.
            if _current_mode is not None:
                _ensure_display_thread()

    def _toggle_mic_mute(self):
        try:
            muted = voice_assistant.toggle_muted()
            print(f"[GUI] Microphone {'muted' if muted else 'unmuted'}")
        except Exception as e:
            print(f"[GUI] Mic toggle failed: {e}")
        self._refresh_mic_button()

    def _refresh_mic_button(self):
        try:
            muted = bool(voice_assistant.is_muted())
        except Exception:
            muted = False
        if muted:
            self._mic_btn.config(text="Unmute Mic", bg="#d32f2f", fg="white")
        else:
            self._mic_btn.config(text="Mute Mic", bg="#4caf50", fg="white")

    def _sleep_arm(self):
        if self._tracking_transition_in_progress:
            print("[GUI] Transition already in progress")
            return
        self._tracking_transition_in_progress = True
        self._track_btn.config(state=tk.DISABLED)
        self._sleep_btn.config(state=tk.DISABLED)

        def _run_sleep():
            ok = False
            try:
                ok = bool(brain.sleep_arm())
            except Exception as e:
                print(f"[GUI] Sleep action failed: {e}")
                ok = False

            def _finish():
                if ok:
                    self._tracking_active = False
                    self._track_btn.config(text="Track Wrist", bg="#d9d9d9", fg="black")
                    lcd.show_face("sleeping")
                self._track_btn.config(state=tk.NORMAL)
                self._sleep_btn.config(state=tk.NORMAL)
                self._tracking_transition_in_progress = False

            self.after(0, _finish)

        threading.Thread(target=_run_sleep, daemon=True).start()

    def _wake_up_conversation_mode(self):
        """Bring robot home for voice interaction: happy face, tracking off, mic on."""
        if self._tracking_transition_in_progress:
            print("[GUI] Transition already in progress")
            return
        self._tracking_transition_in_progress = True
        self._track_btn.config(state=tk.DISABLED)
        self._sleep_btn.config(state=tk.DISABLED)
        self._wake_btn.config(state=tk.DISABLED)

        def _run_wake():
            ok = False
            try:
                # Ensure controller is powered so HOME pose command is effective.
                brain.set_servo_power(True)
                time.sleep(1.5)  # Extra time for relay + controller boot
                ok = bool(brain.stop_wrist_tracking_to_home())
                if ok:
                    print("[GUI] Wake Up pose completed successfully")
                else:
                    print("[GUI] Wake Up pose failed")
            except Exception as e:
                print(f"[GUI] Wake Up failed: {e}")
                ok = False

            def _finish():
                if ok:
                    self._tracking_active = False
                    self._track_btn.config(text="Track Wrist", bg="#d9d9d9", fg="black")
                    lcd.show_face("happy")
                    try:
                        voice_assistant.set_muted(True)
                    except Exception:
                        pass
                    self._refresh_mic_button()
                    print("[GUI] Conversation mode ready (home, happy, tracking off, mic on)")
                self._track_btn.config(state=tk.NORMAL)
                self._sleep_btn.config(state=tk.NORMAL)
                self._wake_btn.config(state=tk.NORMAL)
                self._tracking_transition_in_progress = False

            self.after(0, _finish)

        threading.Thread(target=_run_wake, daemon=True).start()

    def _open_claw(self):
        threading.Thread(target=brain.open_claw, daemon=True).start()

    def _close_claw(self):
        threading.Thread(target=brain.close_claw, daemon=True).start()

    def _put_down(self):
        def _run_put_down():
            try:
                brain.set_servo_power(True)
                time.sleep(0.2)
                # Put down sequence: rotate base first, then lower shoulder.
                brain.send_servo_command(6, 800, 900)
                time.sleep(0.95)
                brain.send_servo_command(5, 1936, 900)
                time.sleep(0.95)
                brain.open_claw(time_ms=350)
                time.sleep(0.45)
                # Return sequence: base first, then shoulder to home height.
                brain.send_servo_command(6, 1500, 900)
                time.sleep(0.95)
                brain.send_servo_command(5, 1121, 1200)
                time.sleep(1.25)
                print("[GUI] Put Down completed")
            except Exception as e:
                print(f"[GUI] Put Down failed: {e}")

        threading.Thread(target=_run_put_down, daemon=True).start()



    def _toggle_claw_cycle(self):
        if self._claw_cycle_active:
            self._stop_claw_cycle_ui()
            return
        self._remember_face_before_claw()
        self._claw_cycle_active = True
        self._claw_cycle_stop.clear()
        self._claw_cycle_btn.config(text="Claw Cycle (ON)", bg="#d9534f", fg="white")
        self._claw_cycle_thread = threading.Thread(target=self._run_claw_cycle, daemon=True)
        self._claw_cycle_thread.start()

    def _stop_claw_cycle_ui(self):
        self._claw_cycle_active = False
        self._claw_cycle_stop.set()
        self._claw_cycle_btn.config(text="Claw Cycle (OFF)", bg="#d9d9d9", fg="black")
        self._restore_idle_face()

    def _remember_face_before_claw(self):
        try:
            self._face_before_claw = lcd.get_current_face_name()
        except Exception:
            self._face_before_claw = None

    def _restore_idle_face(self):
        if self._face_before_claw:
            lcd.show_face(self._face_before_claw)
            return
        if self._tracking_active:
            lcd.show_face("thinking")
        else:
            lcd.show_face("sleeping")

    def _run_claw_cycle(self):
        while not self._claw_cycle_stop.is_set():
            cycle_start = time.time()
            lcd.show_face("mad")

            cycle_open_ms = int(getattr(config, "CLAW_CYCLE_OPEN_TIME_MS", 300))
            cycle_close_step_us = int(getattr(config, "CLAW_CYCLE_CLOSE_STEP_US", 60))
            cycle_close_step_time_ms = int(getattr(config, "CLAW_CYCLE_CLOSE_STEP_TIME_MS", 35))
            cycle_pause_s = float(getattr(config, "CLAW_CYCLE_PAUSE_S", 0.08))

            for _ in range(2):
                if self._claw_cycle_stop.is_set():
                    break
                brain.close_claw(
                    step_us=cycle_close_step_us,
                    step_time_ms=cycle_close_step_time_ms,
                )
                if self._claw_cycle_stop.is_set():
                    break
                time.sleep(cycle_pause_s)
                brain.open_claw(time_ms=cycle_open_ms)
                if self._claw_cycle_stop.is_set():
                    break
                time.sleep(cycle_pause_s)

            # Mad face is only for the active close/open action period.
            if not self._claw_cycle_stop.is_set():
                self._restore_idle_face()

            while (time.time() - cycle_start) < 60.0 and not self._claw_cycle_stop.is_set():
                time.sleep(0.2)

        # Ensure UI and face are reset when loop exits naturally.
        self.after(0, self._stop_claw_cycle_ui)

    def _toggle_ai_tools(self):
        """Toggle AI tools (poses and faces) on/off."""
        try:
            import voice_assistant
            current_state = voice_assistant.get_ai_tools_enabled()
            new_state = not current_state
            voice_assistant.set_ai_tools_enabled(new_state)
            if new_state:
                self._ai_tools_btn.config(text="AI Tools (ON)", bg="#4caf50", fg="white")
            else:
                self._ai_tools_btn.config(text="AI Tools (OFF)", bg="#d9d9d9", fg="black")
            status = "enabled" if new_state else "disabled"
            print(f"[GUI] AI Tools {status}")
        except Exception as e:
            print(f"[GUI] Error toggling AI Tools: {e}")

    def _toggle_auto_take(self):
        if not hasattr(self, "_auto_take_active"):
            # Defensive fallback in case callbacks fire before full init.
            self._auto_take_active = False
            self._auto_take_stop = threading.Event()
            self._auto_take_thread = None
            self._auto_take_sensor = None
            self._auto_take_busy = False
        if self._auto_take_active:
            self._stop_auto_take_ui()
            return

        sensor = self._create_auto_take_sensor()
        if sensor is None:
            return

        self._auto_take_sensor = sensor
        self._auto_take_active = True
        self._auto_take_stop.clear()
        self._auto_take_btn.config(text="Auto-Take (ON)", bg="#4caf50", fg="white")
        self._auto_take_thread = threading.Thread(target=self._run_auto_take_loop, daemon=True)
        self._auto_take_thread.start()
        print("[GUI] Auto-take enabled")

    def _stop_auto_take_ui(self):
        self._auto_take_active = False
        self._auto_take_stop.set()
        self._auto_take_btn.config(text="Auto-Take (OFF)", bg="#d9d9d9", fg="black")
        sensor = self._auto_take_sensor
        self._auto_take_sensor = None
        if sensor is not None:
            try:
                import lgpio
                h = sensor.get("h")
                if h is not None:
                    trig = sensor.get("trig")
                    echo = sensor.get("echo")
                    if trig is not None:
                        try:
                            lgpio.gpio_free(h, trig)
                        except Exception:
                            pass
                    if echo is not None:
                        try:
                            lgpio.gpio_free(h, echo)
                        except Exception:
                            pass
                    lgpio.gpiochip_close(h)
            except Exception:
                pass
        print("[GUI] Auto-take disabled")

    def _create_auto_take_sensor(self):
        trig_pin = int(getattr(config, "ULTRASONIC_TRIGGER_PIN_BCM", 23))
        echo_pin = int(getattr(config, "ULTRASONIC_ECHO_PIN_BCM", 24))
        try:
            import lgpio
            h = lgpio.gpiochip_open(0)
            lgpio.gpio_claim_output(h, trig_pin)
            lgpio.gpio_claim_input(h, echo_pin)
            lgpio.gpio_write(h, trig_pin, 0)
            print(f"[GUI] Auto-take sensor ready (TRIG BCM {trig_pin}, ECHO BCM {echo_pin})")
            return {"h": h, "trig": trig_pin, "echo": echo_pin}
        except Exception as e:
            print(f"[GUI] Auto-take sensor init failed: {e}")
            return None

    def _read_auto_take_distance(self, sensor):
        """Read distance in cm from lgpio-based ultrasonic sensor."""
        if sensor is None:
            return None
        try:
            import lgpio
            h = sensor["h"]
            trig = sensor["trig"]
            echo = sensor["echo"]
            
            lgpio.gpio_write(h, trig, 1)
            time.sleep(0.00001)
            lgpio.gpio_write(h, trig, 0)
            
            t0 = time.time()
            while lgpio.gpio_read(h, echo) == 0:
                if time.time() - t0 > 0.03:
                    return None
            pulse_start = time.time()
            
            while lgpio.gpio_read(h, echo) == 1:
                if time.time() - pulse_start > 0.03:
                    return None
            pulse_end = time.time()
            
            pulse_duration = pulse_end - pulse_start
            distance_cm = round(pulse_duration * 17150.0, 2)
            return distance_cm
        except Exception as e:
            print(f"[GUI] Distance read error: {e}")
            return None

    def _run_auto_take_loop(self):
        sensor = self._auto_take_sensor
        if sensor is None:
            self.after(0, self._stop_auto_take_ui)
            return

        trigger_cm = float(getattr(config, "AUTO_TAKE_TRIGGER_DISTANCE_CM", 14.0))
        clear_cm = max(trigger_cm + 1.0, float(getattr(config, "AUTO_TAKE_CLEAR_DISTANCE_CM", 20.0)))
        stable_s = max(0.05, float(getattr(config, "AUTO_TAKE_STABLE_TIME_S", 0.25)))
        poll_s = max(0.03, float(getattr(config, "AUTO_TAKE_POLL_INTERVAL_S", 0.08)))
        require_hand = bool(getattr(config, "AUTO_TAKE_REQUIRE_HAND_VISIBLE", True))
        hand_max_age_s = max(0.1, float(getattr(config, "AUTO_TAKE_HAND_MAX_AGE_S", 0.9)))
        detected_since = None
        waiting_for_clear = False
        read_fail_count = 0

        while not self._auto_take_stop.is_set():
            distance_cm = self._read_auto_take_distance(sensor)
            if distance_cm is None:
                read_fail_count += 1
                if read_fail_count == 1 or (read_fail_count % 30) == 0:
                    print("[GUI] Auto-take distance read timeout; continuing")
                time.sleep(poll_s)
                continue
            read_fail_count = 0

            if waiting_for_clear:
                if distance_cm >= clear_cm:
                    waiting_for_clear = False
                    detected_since = None
                time.sleep(poll_s)
                continue

            if self._tracking_active or self._tracking_transition_in_progress or self._claw_cycle_active or self._auto_take_busy:
                detected_since = None
                time.sleep(poll_s)
                continue

            if require_hand:
                try:
                    _, wrist_seen_t = ai.get_right_wrist_norm()
                    hand_visible = (time.time() - float(wrist_seen_t)) <= hand_max_age_s
                except Exception:
                    hand_visible = False
                if not hand_visible:
                    detected_since = None
                    time.sleep(poll_s)
                    continue

            if 0.0 < distance_cm <= trigger_cm:
                if detected_since is None:
                    detected_since = time.time()
                elif (time.time() - detected_since) >= stable_s:
                    self._auto_take_busy = True
                    try:
                        success = bool(self._perform_auto_take())
                    finally:
                        self._auto_take_busy = False
                    if success:
                        # One-shot behavior: disable auto-take after a successful grab.
                        self.after(0, self._stop_auto_take_ui)
                        return
                    waiting_for_clear = True
                    detected_since = None
            else:
                detected_since = None

            time.sleep(poll_s)

        self.after(0, self._stop_auto_take_ui)

    def _perform_auto_take(self):
        display_text = str(getattr(config, "AUTO_TAKE_DISPLAY_TEXT", "TAKE"))
        display_s = max(0.0, float(getattr(config, "AUTO_TAKE_DISPLAY_TIME_S", 1.0)))
        open_time_ms = int(getattr(config, "AUTO_TAKE_OPEN_TIME_MS", 250))
        extend_time_ms = int(getattr(config, "AUTO_TAKE_EXTEND_TIME_MS", 900))
        settle_s = max(0.0, float(getattr(config, "AUTO_TAKE_SETTLE_TIME_S", 0.6)))
        return_time_ms = int(getattr(config, "AUTO_TAKE_RETURN_TIME_MS", 1200))
        extend_positions = dict(getattr(config, "AUTO_TAKE_EXTEND_POSITIONS", {3: 1827, 4: 1568, 5: 1878}))

        print("[GUI] Auto-take triggered")
        prior_positions = brain.get_last_commanded_positions()
        return_positions = {
            int(sid): int(pos)
            for sid, pos in prior_positions.items()
            if int(sid) != 1 and pos is not None
        }

        try:
            brain.set_servo_power(True)
            time.sleep(0.2)
            lcd.show_text(display_text, duration_s=display_s)
            time.sleep(display_s)
            brain.open_claw(time_ms=open_time_ms)
            if extend_positions:
                brain.send_multi_servo_command(extend_positions, extend_time_ms)
                time.sleep(max(0.0, extend_time_ms / 1000.0))
            time.sleep(settle_s)
            brain.close_claw(step_time_ms=120)
            if return_positions:
                brain.send_multi_servo_command(return_positions, return_time_ms)
                time.sleep(max(0.0, return_time_ms / 1000.0))
            print("[GUI] Auto-take completed")
            return True
        except Exception as e:
            print(f"[GUI] Auto-take failed: {e}")
            return False

    def _poll_auto_camera_switch(self):
        try:
            self._poll_gesture_events()
            if self._smart_take_running:
                self.after(200, self._poll_auto_camera_switch)
                return
            now = time.time()
            if now < self._gesture_top_lock_until_t:
                # Hold TOP camera during gesture lock window.
                if _current_mode != "HIGH_CAM":
                    if switch_camera("HIGH_CAM"):
                        self._refresh_camera_buttons()
                self.after(200, self._poll_auto_camera_switch)
                return
            if _current_mode in ("HIGH_CAM", "TABLE_CAM"):
                ai.arm_auto_switch_if_needed()
            target_mode = ai.consume_auto_switch_request()
            if target_mode in ("HIGH_CAM", "TABLE_CAM"):
                print(f"[GUI] Auto-switching to {target_mode}")
                if switch_camera(target_mode):
                    self._refresh_camera_buttons()
        except Exception as e:
            print(f"[GUI] Auto camera switch poll error: {e}")
        self.after(200, self._poll_auto_camera_switch)

    def _poll_gesture_events(self):
        if not self._gesture_enabled:
            return
        now = time.time()

        hand_max_age_s = max(0.1, float(getattr(config, "GESTURE_HAND_MAX_AGE_S", 0.40)))
        hold_s = max(0.2, float(getattr(config, "GESTURE_CENTER_HOLD_S", 1.25)))
        allow_wrist_fallback = bool(getattr(config, "GESTURE_ALLOW_WRIST_PROXY_FALLBACK", True))

        centered = False
        try:
            status = ai.get_hand_gesture_status()
            if not self._gesture_engine_logged:
                self._gesture_engine_logged = True
                print(
                    "[GUI] Hand gesture engine: "
                    f"{status.get('engine')} "
                    f"(mp_available={status.get('mediapipe_available')}, "
                    f"model_complexity={status.get('model_complexity')})"
                )
            centered, _, _ = ai.get_closed_fist_centered_recent(max_age_s=hand_max_age_s)
        except Exception:
            centered = False

        if (not centered) and allow_wrist_fallback:
            try:
                (x_norm, y_norm), seen_t = ai.get_right_wrist_norm()
                center_x = float(getattr(config, "GESTURE_CENTER_X", 0.50))
                center_y = float(getattr(config, "GESTURE_CENTER_Y", 0.50))
                x_tol = max(0.01, float(getattr(config, "GESTURE_CENTER_X_TOL", 0.10)))
                y_tol = max(0.01, float(getattr(config, "GESTURE_CENTER_Y_TOL", 0.12)))
                hand_visible = (now - float(seen_t)) <= hand_max_age_s
                centered = (
                    hand_visible
                    and abs(float(x_norm) - center_x) <= x_tol
                    and abs(float(y_norm) - center_y) <= y_tol
                )
            except Exception:
                centered = False

        # End-of-window status overlay, rendered over current face via mode label.
        if self._gesture_done_pending and now >= self._gesture_top_lock_until_t:
            self._gesture_done_pending = False
            done_text = str(getattr(config, "GESTURE_DONE_TEXT", "done"))
            done_s = max(0.0, float(getattr(config, "GESTURE_DONE_OVERLAY_S", 10.0)))
            lcd.set_camera_mode(done_text)
            self._gesture_done_end_t = now + done_s
            print(f"[GUI] Gesture lock finished; showing '{done_text}' for {done_s:.1f}s")

        if self._gesture_done_end_t > 0.0 and now >= self._gesture_done_end_t:
            self._gesture_done_end_t = 0.0
            # Restore standard top label after done display period.
            lcd.set_camera_mode("HIGH_CAM")

        # During lock window, accept one follow-up command gesture.
        if now < self._gesture_top_lock_until_t:
            one_age_s = max(0.1, float(getattr(config, "GESTURE_COMMAND_ONE_MAX_AGE_S", 0.45)))
            two_age_s = max(0.1, float(getattr(config, "GESTURE_COMMAND_TWO_MAX_AGE_S", 0.45)))
            one_recent = False
            two_recent = False
            try:
                one_recent, _ = ai.get_one_finger_recent(max_age_s=one_age_s)
            except Exception:
                one_recent = False
            try:
                two_recent, _ = ai.get_two_finger_recent(max_age_s=two_age_s)
            except Exception:
                two_recent = False
            if one_recent and not self._gesture_followup_triggered:
                self._gesture_followup_triggered = True
                self._on_followup_gesture_event("LIGHT_ON")
            elif two_recent and not self._gesture_followup_triggered:
                self._gesture_followup_triggered = True
                self._on_followup_gesture_event("LIGHT_OFF")
            return

        if not centered:
            self._gesture_hold_started_t = None
            return

        if self._gesture_hold_started_t is None:
            self._gesture_hold_started_t = now
            return

        if (now - self._gesture_hold_started_t) < hold_s:
            return

        self._gesture_hold_started_t = None
        self._on_centered_closed_fist_hold()

    def _on_centered_closed_fist_hold(self):
        # Primary trigger is MediaPipe Hands closed-fist detection; optional
        # wrist-centered fallback can be enabled via config if needed.
        lock_s = max(0.5, float(getattr(config, "GESTURE_LOCK_TOP_CAM_S", 20.0)))
        self._gesture_top_lock_until_t = time.time() + lock_s
        self._gesture_done_pending = True
        self._gesture_followup_triggered = False
        try:
            print(f"[GUI] Gesture event: centered hold detected; lock TOP for {lock_s:.1f}s")
            if _current_mode != "HIGH_CAM":
                switch_camera("HIGH_CAM")
                self._refresh_camera_buttons()
            else:
                lcd.set_camera_mode("HIGH_CAM")
        except Exception as e:
            print(f"[GUI] Gesture camera lock failed: {e}")
        try:
            self._run_named_pose_dialog("nod")
        except Exception as e:
            print(f"[GUI] Gesture nod trigger failed: {e}")

    def _on_followup_gesture_event(self, command: str):
        cmd = str(command or "").strip().upper()
        if not cmd:
            return
        print(f"[GUI] Follow-up command gesture detected: {cmd}")
        sent = False
        try:
            sent = bool(brain.send_to_crestron(cmd))
        except Exception as e:
            print(f"[GUI] Crestron send failed for {cmd}: {e}")
        try:
            overlay = f"{cmd}{'' if sent else ' (NO LINK)'}"
            lcd.set_camera_mode(overlay[:16])
            self.after(1500, lambda: lcd.set_camera_mode("HIGH_CAM"))
        except Exception:
            pass

    def _faces_win_close(self):
        if self._faces_win:
            try:
                self._faces_win.destroy()
            except Exception:
                pass
            self._faces_win = None

class SliderWindow(tk.Toplevel):
    def __init__(self, master=None):
        super().__init__(master)
        self.title("Sliders")
        state = _load_gui_state()
        self.geometry(state.get("slider_geometry", "420x760+340+0"))
        self.minsize(420, 620)
        self.attributes("-topmost", True)
        self.after(250, lambda: self.attributes("-topmost", False))
        global _slider_window_ref
        _slider_window_ref = self

        tk.Label(self, text="High Cam Right Wrist").pack(pady=10)

        self.lr_label = tk.Label(self, text="Left/Right: 50%")
        self.lr_label.pack(pady=(10, 0))

        self.lr_slider = tk.Scale(
            self,
            from_=0,
            to=100,
            orient=tk.HORIZONTAL,
            length=240,
            label="Wrist Left/Right"
        )
        self.lr_slider.set(50)
        self.lr_slider.config(state=tk.DISABLED)
        self.lr_slider.pack(pady=5)

        self.ud_label = tk.Label(self, text="Up/Down: 50%")
        self.ud_label.pack(pady=(15, 0))

        self.ud_slider = tk.Scale(
            self,
            from_=0,
            to=100,
            orient=tk.HORIZONTAL,
            length=240,
            label="Wrist Up/Down"
        )
        self.ud_slider.set(50)
        self.ud_slider.config(state=tk.DISABLED)
        self.ud_slider.pack(pady=5)

        self.ultra_label = tk.Label(self, text="Ultrasonic Sensor: -- cm")
        self.ultra_label.pack(pady=(15, 0))

        self.ultra_slider = tk.Scale(
            self,
            from_=0,
            to=200,
            orient=tk.HORIZONTAL,
            length=240,
            label="Ultrasonic Sensor (cm)",
        )
        self.ultra_slider.set(0)
        self.ultra_slider.config(state=tk.DISABLED)
        self.ultra_slider.pack(pady=5)

        self._smart_x_title = tk.Label(self, text="Smart Take X Tuner", font=("TkDefaultFont", 10, "bold"))
        self._smart_x_title.pack(pady=(12, 2))

        self._smart_left_scale = tk.Scale(
            self,
            from_=1000,
            to=2500,
            orient=tk.HORIZONTAL,
            length=240,
            label="Base @ Frame Left (servo 6)",
            command=self._on_smart_left_changed,
        )
        self._smart_left_scale.set(int(getattr(config, "SMART_TAKE_BASE_LEFT", 2000)))
        self._smart_left_scale.pack(pady=2)

        self._smart_right_scale = tk.Scale(
            self,
            from_=1000,
            to=2500,
            orient=tk.HORIZONTAL,
            length=240,
            label="Base @ Frame Right (servo 6)",
            command=self._on_smart_right_changed,
        )
        self._smart_right_scale.set(int(getattr(config, "SMART_TAKE_BASE_RIGHT", 1000)))
        self._smart_right_scale.pack(pady=2)

        self._smart_x_detail = tk.Label(self, text="", anchor="w", justify=tk.LEFT)
        self._smart_x_detail.pack(padx=10, pady=(0, 6), fill=tk.X)
        self._refresh_smart_x_detail()

        self.switch_label = tk.Label(self, text="Gripper Switch: --", font=("TkDefaultFont", 10, "bold"))
        self.switch_label.pack(pady=(10, 2))
        self.switch_detail = tk.Label(self, text="", anchor="w", justify=tk.LEFT)
        self.switch_detail.pack(padx=10, pady=(0, 6), fill=tk.X)

        self._switch_status_title = tk.Label(self, text="Auto Switch Status", font=("TkDefaultFont", 10, "bold"))
        self._switch_status_title.pack(pady=(12, 4))

        self._switch_status_rows = {}
        for key, label in [
            ("enabled", "Enabled"),
            ("table_cam_active", "Table Cam Active"),
            ("tracking_enabled", "Tracking Enabled"),
            ("wrist_disappeared", "Wrist Missing Long Enough"),
            ("was_top_recently", "Top Wrist Seen Recently"),
            ("fallback_top_met", "Top Fallback Met"),
            ("not_already_requested", "No Pending Request"),
            ("computed_target_high", "Computed Target Is HIGH_CAM"),
            ("request_armed", "Request Armed"),
        ]:
            row = tk.Label(self, text=f"{label}: --", anchor="w", justify=tk.LEFT, width=34)
            row.pack(padx=10, pady=1, fill=tk.X)
            self._switch_status_rows[key] = row

        self._switch_status_detail = tk.Label(self, text="", anchor="w", justify=tk.LEFT)
        self._switch_status_detail.pack(padx=10, pady=(6, 0), fill=tk.X)

        # EMA smoothed values (start at 50%)
        self._lr_smooth = 50.0
        self._ud_smooth = 50.0
        self._ultra_smooth = None

        self.after(10, self._ensure_window_fits_content)

        self._poll_wrist()

    def _ensure_window_fits_content(self):
        self.update_idletasks()
        req_w = int(self.winfo_reqwidth()) + 16
        req_h = int(self.winfo_reqheight()) + 16
        min_w = max(420, req_w)
        min_h = max(620, req_h)
        self.minsize(min_w, min_h)

        cur_w = int(self.winfo_width())
        cur_h = int(self.winfo_height())
        if cur_w < min_w or cur_h < min_h:
            x = int(self.winfo_x())
            y = int(self.winfo_y())
            self.geometry(f"{max(cur_w, min_w)}x{max(cur_h, min_h)}+{x}+{y}")

    def _set_slider_value(self, slider, value):
        slider.config(state=tk.NORMAL)
        slider.set(value)
        slider.config(state=tk.DISABLED)

    def _poll_wrist(self):
        _EMA_ALPHA = 0.25  # lower = smoother but slower to respond
        wrist, seen_t = ai.get_right_wrist_norm()
        x_norm, y_norm = wrist
        # Apply EMA smoothing to reduce twitchiness.
        self._lr_smooth = _EMA_ALPHA * (x_norm * 100) + (1 - _EMA_ALPHA) * self._lr_smooth
        self._ud_smooth = _EMA_ALPHA * ((1.0 - y_norm) * 100) + (1 - _EMA_ALPHA) * self._ud_smooth
        lr_pct = int(max(0, min(100, round(self._lr_smooth))))
        ud_pct = int(max(0, min(100, round(self._ud_smooth))))

        self._set_slider_value(self.lr_slider, lr_pct)
        self._set_slider_value(self.ud_slider, ud_pct)
        self.lr_label.config(text=f"Left/Right: {lr_pct}%")
        self.ud_label.config(text=f"Up/Down: {ud_pct}%")

        self._poll_ultrasonic_value()
        self._poll_gripper_switch_status()

        try:
            status = ai.get_auto_switch_status()
            self._update_switch_status_rows(status)
        except Exception as e:
            self._switch_status_detail.config(text=f"Status unavailable: {e}", fg="#666666")

        self.after(150, self._poll_wrist)

    def _poll_ultrasonic_value(self):
        ultra_alpha = float(getattr(config, "ULTRASONIC_SLIDER_SMOOTH_ALPHA", 0.22))
        ultra_alpha = max(0.0, min(1.0, ultra_alpha))

        distance_cm = _get_ultrasonic_cache(max_age_s=1.2)
        if distance_cm is None and (_button_window_ref is None or not _button_window_ref._auto_take_active):
            distance_cm = _read_ultrasonic_once()

        if distance_cm is None:
            self.ultra_label.config(text="Ultrasonic Sensor: -- cm")
            self._set_slider_value(self.ultra_slider, 0)
            self._ultra_smooth = None
            return

        if self._ultra_smooth is None:
            self._ultra_smooth = float(distance_cm)
        else:
            self._ultra_smooth = (ultra_alpha * float(distance_cm)) + ((1.0 - ultra_alpha) * self._ultra_smooth)

        shown = max(0, min(200, int(round(self._ultra_smooth))))
        self._set_slider_value(self.ultra_slider, shown)
        self.ultra_label.config(text=f"Ultrasonic Sensor: {self._ultra_smooth:.2f} cm")

    def _refresh_smart_x_detail(self):
        left = int(getattr(config, "SMART_TAKE_BASE_LEFT", 2000))
        right = int(getattr(config, "SMART_TAKE_BASE_RIGHT", 1000))
        self._smart_x_detail.config(text=f"Current mapping: left={left}, right={right}", fg="#666666")

    def _on_smart_left_changed(self, value):
        try:
            config.SMART_TAKE_BASE_LEFT = int(float(value))
            self._refresh_smart_x_detail()
        except Exception:
            pass

    def _on_smart_right_changed(self, value):
        try:
            config.SMART_TAKE_BASE_RIGHT = int(float(value))
            self._refresh_smart_x_detail()
        except Exception:
            pass

    def _poll_gripper_switch_status(self):
        try:
            st = brain.get_gripper_switch_status()
            ready = bool(st.get("ready", False))
            pressed = bool(st.get("pressed", False))
            raw = st.get("raw", None)
            pstate = st.get("pressed_state", 0)
            last_stop = bool(st.get("last_close_switch_stop", False))

            if not ready:
                self.switch_label.config(text="Gripper Switch: NOT READY", fg="#d32f2f")
                self.switch_detail.config(text="GPIO not initialized", fg="#8a8a8a")
                return

            if pressed:
                self.switch_label.config(text="Gripper Switch: ACTIVE", fg="#2e7d32")
            else:
                self.switch_label.config(text="Gripper Switch: INACTIVE", fg="#8a8a8a")

            self.switch_detail.config(
                text=(
                    f"raw={raw}  pressed_state={pstate}  "
                    f"last_close_switch_stop={'True' if last_stop else 'False'}"
                ),
                fg="#666666",
            )
        except Exception as e:
            self.switch_label.config(text="Gripper Switch: ERROR", fg="#d32f2f")
            self.switch_detail.config(text=str(e), fg="#8a8a8a")

    def _update_switch_status_rows(self, status):
        def _set_row(key, text, value):
            row = self._switch_status_rows.get(key)
            if row is None:
                return
            color = "#2e7d32" if bool(value) else "#8a8a8a"
            row.config(text=f"{text}: {'True' if value else 'False'}", fg=color)

        _set_row("enabled", "Enabled", status.get("enabled", False))
        _set_row("table_cam_active", "Table Cam Active", status.get("table_cam_active", False))
        _set_row("tracking_enabled", "Tracking Enabled", status.get("tracking_enabled", False))
        _set_row("wrist_disappeared", "Wrist Missing Long Enough", status.get("wrist_disappeared", False))
        _set_row("was_top_recently", "Top Wrist Seen Recently", status.get("was_top_recently", False))
        _set_row("fallback_top_met", "Top Fallback Met", status.get("fallback_top_met", False))
        _set_row("not_already_requested", "No Pending Request", status.get("not_already_requested", False))
        _set_row("computed_target_high", "Computed Target Is HIGH_CAM", status.get("computed_target") == "HIGH_CAM")
        _set_row("request_armed", "Request Armed", status.get("request_armed", False))

        detail = (
            f"UD={status.get('last_ud_pct', 0.0):.1f}%  "
            f"missing={status.get('wrist_missing_s', 0.0):.2f}s/{status.get('disappear_threshold_s', 0.0):.2f}s  "
            f"topAge={status.get('top_recent_s', 0.0):.2f}s/{status.get('top_memory_s', 0.0):.2f}s  "
            f"top>= {status.get('top_threshold_pct', 0.0):.1f}%  fallback>= {status.get('top_fallback_pct', 0.0):.1f}%"
        )
        should_switch = bool(status.get("should_switch", False))
        self._switch_status_detail.config(text=detail, fg="#2e7d32" if should_switch else "#8a8a8a")

class PosesWindow(tk.Toplevel):
    """Popup window with named pose buttons and random timer execution checkboxes."""
    _POSES = [
        ("Home", "home", "#6d8591"),
        ("Sleep", "sleep", "#e0ccff"),
        ("Nod", "nod", "#ccffcc"),
        ("No", "no", "#ffe0cc"),
        ("Table Take", "table-take", "#ffb74d"),
        ("Roll", "roll", "#ffd54f"),
        ("Reach", "reach", "#cce0ff"),
        ("W", "w", "#ffcccc"),
    ]

    def __init__(self, master=None, owner=None):
        super().__init__(master)
        self._owner = owner
        self.title("Poses")
        state = _load_gui_state()
        self.geometry(state.get("poses_geometry", "360x500+840+0"))
        self.minsize(360, 500)
        self.attributes("-topmost", True)
        self.after(250, lambda: self.attributes("-topmost", False))
        self.transient(master)
        self.lift()
        self.focus_force()
        self.resizable(True, True)
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        
        # Track selected poses for random execution
        self._selected_random_poses = set()
        self._random_timer_thread = None
        self._random_timer_stop = threading.Event()
        
        tk.Label(self, text="Robot Poses", font=("TkDefaultFont", 10, "bold")).pack(pady=(10, 8))
        
        # Create frame for poses with checkbox column
        self._poses_frame = tk.Frame(self)
        self._poses_frame.pack(fill=tk.BOTH, expand=True, padx=5, pady=5)
        
        # Add header
        header_frame = tk.Frame(self._poses_frame)
        header_frame.pack(fill=tk.X, pady=(0, 5))
        tk.Label(header_frame, text="Pose", font=("TkDefaultFont", 9, "bold")).pack(side=tk.LEFT, expand=True)
        tk.Label(header_frame, text="Random", font=("TkDefaultFont", 9, "bold")).pack(side=tk.RIGHT, padx=(5, 0))

        for label, pose_name, colour in self._POSES:
            self._create_pose_row(label, pose_name, colour)

    def _create_pose_row(self, label, pose_name, colour):
        """Create a row with a button and checkbox for a pose."""
        row_frame = tk.Frame(self._poses_frame)
        row_frame.pack(fill=tk.X, pady=2)
        
        # Pose button
        tk.Button(
            row_frame,
            text=label,
            bg=colour,
            width=20,
            command=lambda p=pose_name: self._run_pose(p),
        ).pack(side=tk.LEFT, expand=True, fill=tk.BOTH)
        
        # Random timer checkbox
        var = tk.BooleanVar(value=False)
        checkbox = tk.Checkbutton(
            row_frame,
            variable=var,
            command=lambda p=pose_name, v=var: self._toggle_random_pose(p, v),
            width=8,
        )
        checkbox.pack(side=tk.RIGHT, padx=(5, 0))
        
        # Store reference to track the pose
        setattr(checkbox, "_pose_name", pose_name)

    def _toggle_random_pose(self, pose_name: str, var: tk.BooleanVar):
        """Handle checkbox toggle for random pose execution."""
        if var.get():
            self._selected_random_poses.add(pose_name)
            self._start_random_timer()
        else:
            self._selected_random_poses.discard(pose_name)
            if not self._selected_random_poses:
                self._stop_random_timer()

    def _start_random_timer(self):
        """Start the random pose execution timer if not already running."""
        if self._random_timer_thread is None or not self._random_timer_thread.is_alive():
            self._random_timer_stop.clear()
            self._random_timer_thread = threading.Thread(
                target=self._random_timer_loop,
                daemon=True
            )
            self._random_timer_thread.start()

    def _stop_random_timer(self):
        """Stop the random pose execution timer."""
        self._random_timer_stop.set()

    def _random_timer_loop(self):
        """Background thread that randomly executes selected poses."""
        import random
        while not self._random_timer_stop.is_set():
            try:
                # Wait for a random interval (10-30 seconds)
                wait_time = random.uniform(10, 30)
                if self._random_timer_stop.wait(wait_time):
                    break
                
                # Check if still have selected poses
                if not self._selected_random_poses:
                    break
                
                # Pick a random pose and execute it
                pose_name = random.choice(list(self._selected_random_poses))
                self._run_pose(pose_name)
                
            except Exception as e:
                print(f"[GUI] Random timer error: {e}")

    def _run_pose(self, pose_name: str):
        if self._owner is not None:
            try:
                self._owner._run_named_pose_dialog(pose_name)
                return
            except Exception:
                pass
        threading.Thread(target=brain.run_pose, args=(pose_name,), daemon=True).start()

    def _on_close(self):
        # Stop random timer
        self._stop_random_timer()
        self._selected_random_poses.clear()
        
        if self._owner is not None:
            try:
                self._owner._poses_win = None
            except Exception:
                pass
        try:
            self.grab_release()
        except Exception:
            pass
        self.destroy()


if __name__ == "__main__":
    btn_win = ButtonWindow()
    slider_win = SliderWindow(btn_win)
    btn_win.mainloop()
