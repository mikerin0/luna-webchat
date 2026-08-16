#!/usr/bin/env python3
"""Run one bounded Hailo YOLO detection pass on a Pi CSI camera."""

import argparse
import json
import os
import subprocess
import threading
import time
from pathlib import Path

import cv2
import gi

gi.require_version("Gst", "1.0")
from gi.repository import GLib, Gst

import hailo


CAMERAS = {
    0: "/base/axi/pcie@1000120000/rp1/i2c@88000/imx708@1a",
    1: "/base/axi/pcie@1000120000/rp1/i2c@80000/imx708@1a",
}
HEF_PATH = "/usr/local/hailo/resources/models/hailo8/yolov8m.hef"
POSTPROCESS_PATH = "/usr/local/hailo/resources/so/libyolo_hailortpp_postprocess.so"


def _bbox_dict(detection) -> dict[str, float] | None:
    try:
        bbox = detection.get_bbox()
        return {
            "x": float(bbox.xmin()),
            "y": float(bbox.ymin()),
            "w": float(bbox.width()),
            "h": float(bbox.height()),
        }
    except Exception:
        return None


def _write_native_annotated_frame(camera_index: int, closeup: bool, detections: list[dict], output_path: str | None) -> str | None:
    if not output_path:
        return None
    raw_path = f"/tmp/luna_hailo_native_{camera_index}.jpg"
    capture = subprocess.run(
        [
            "rpicam-jpeg", "--camera", str(camera_index), "--width", "1536", "--height", "864",
            "--autofocus-mode", "continuous", "--autofocus-range", "full",
            "--timeout", "1000", "--output", raw_path,
        ],
        capture_output=True, text=True, check=False,
    )
    frame = cv2.imread(raw_path) if capture.returncode == 0 else None
    if frame is None:
        return None

    frame_height, frame_width = frame.shape[:2]
    for item in detections:
        bbox = item.get("bbox")
        if not bbox:
            continue
        if closeup:
            x = (bbox["x"] * 768.0 + 384.0) / 1536.0
            y = (bbox["y"] * 744.0 + 120.0) / 864.0
            w = bbox["w"] * 768.0 / 1536.0
            h = bbox["h"] * 744.0 / 864.0
        else:
            x, w = bbox["x"], bbox["w"]
            y, h = bbox["y"] * (640.0 / 864.0), bbox["h"] * (640.0 / 864.0)
        x1 = max(0, min(frame_width - 1, int(x * frame_width)))
        y1 = max(0, min(frame_height - 1, int(y * frame_height)))
        x2 = max(x1 + 1, min(frame_width - 1, int((x + w) * frame_width)))
        y2 = max(y1 + 1, min(frame_height - 1, int((y + h) * frame_height)))
        label = f"{item['label']} {float(item['confidence']):.0%}"
        cv2.rectangle(frame, (x1, y1), (x2, y2), (255, 255, 0), 4)
        cv2.putText(frame, label, (x1, max(32, y1 - 10)), cv2.FONT_HERSHEY_SIMPLEX, 1.0, (255, 255, 0), 3)

    if cv2.imwrite(output_path, cv2.resize(frame, (640, 360), interpolation=cv2.INTER_AREA)):
        return output_path
    return None


def run_detection(camera_index: int, frames: int, confidence_threshold: float, closeup: bool, annotated_output: str | None) -> dict:
    if camera_index not in CAMERAS:
        raise ValueError(f"Unsupported camera index: {camera_index}")

    os.environ.setdefault("hailort_device_type", "hailo8")
    Gst.init(None)
    crop = "videocrop left=384 right=384 top=120 bottom=0 ! " if closeup else ""
    pipeline_text = (
        f'libcamerasrc camera-name="{CAMERAS[camera_index]}" af-mode=continuous af-range=full ! '
        "video/x-raw,format=NV12,width=1536,height=864 ! "
        f"videoconvert ! {crop}videoscale ! video/x-raw,format=RGB,width=640,height=640 ! "
        f"hailonet hef-path={HEF_PATH} force-writable=true ! "
        f"hailofilter so-path={POSTPROCESS_PATH} ! "
        "fakesink sync=false"
    )
    pipeline = Gst.parse_launch(pipeline_text)
    loop = GLib.MainLoop()
    detections_by_label: dict[str, dict[str, float | int]] = {}
    frame_count = 0
    lock = threading.Lock()

    def on_buffer(pad, info, _user_data):
        nonlocal frame_count
        buffer = info.get_buffer()
        if buffer is None:
            return Gst.PadProbeReturn.OK
        roi = hailo.get_roi_from_buffer(buffer)
        detections = roi.get_objects_typed(hailo.HAILO_DETECTION)
        with lock:
            frame_count += 1
            for detection in detections:
                label = str(detection.get_label()).strip()
                confidence = float(detection.get_confidence())
                if not label or confidence < confidence_threshold:
                    continue
                current = detections_by_label.get(label)
                if current is None or confidence > float(current["confidence"]):
                    detections_by_label[label] = {
                        "label": label,
                        "confidence": round(confidence, 3),
                        "frames": 1 if current is None else int(current["frames"]) + 1,
                        "bbox": _bbox_dict(detection),
                    }
                else:
                    current["frames"] = int(current["frames"]) + 1
            done = frame_count >= frames
        if done:
            GLib.idle_add(loop.quit)
        return Gst.PadProbeReturn.OK

    hailofilter = pipeline.get_by_name("hailofilter0")
    if hailofilter is None:
        hailofilter = pipeline.get_by_name("hailofilter")
    if hailofilter is None:
        raise RuntimeError("Hailo filter element was not created")
    hailofilter.get_static_pad("src").add_probe(Gst.PadProbeType.BUFFER, on_buffer, None)

    timeout_id = GLib.timeout_add(5000, loop.quit)
    try:
        pipeline.set_state(Gst.State.PLAYING)
        loop.run()
    finally:
        if timeout_id:
            GLib.source_remove(timeout_id)
        pipeline.set_state(Gst.State.NULL)
        pipeline.get_state(int(2 * Gst.SECOND))

    with lock:
        results = sorted(detections_by_label.values(), key=lambda item: float(item["confidence"]), reverse=True)

    annotated_path = _write_native_annotated_frame(camera_index, closeup, results, annotated_output)
    return {
        "camera": camera_index,
        "closeup": closeup,
        "frames": frame_count,
        "detections": results,
        "annotated_output": annotated_path,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--camera", type=int, choices=(0, 1), default=0)
    parser.add_argument("--frames", type=int, default=8)
    parser.add_argument("--confidence", type=float, default=0.35)
    parser.add_argument("--closeup", action="store_true")
    parser.add_argument("--annotated-output", default="")
    args = parser.parse_args()
    try:
        print(json.dumps(run_detection(
            args.camera,
            max(1, args.frames),
            args.confidence,
            args.closeup,
            args.annotated_output or None,
        )))
    except Exception as exc:
        print(json.dumps({"error": str(exc), "camera": args.camera}))
        raise SystemExit(1)


if __name__ == "__main__":
    main()
