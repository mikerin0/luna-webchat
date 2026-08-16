#!/usr/bin/env python3
"""Run one bounded Hailo YOLO detection pass on a Pi CSI camera."""

import argparse
import json
import os
import threading
import time

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


def run_detection(camera_index: int, frames: int, confidence_threshold: float, closeup: bool) -> dict:
    if camera_index not in CAMERAS:
        raise ValueError(f"Unsupported camera index: {camera_index}")

    os.environ.setdefault("hailort_device_type", "hailo8")
    Gst.init(None)
    crop = "videocrop left=384 right=384 top=120 bottom=0 ! " if closeup else ""
    pipeline_text = (
        f'libcamerasrc camera-name="{CAMERAS[camera_index]}" ! '
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
        return {"camera": camera_index, "closeup": closeup, "frames": frame_count, "detections": results}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--camera", type=int, choices=(0, 1), default=0)
    parser.add_argument("--frames", type=int, default=8)
    parser.add_argument("--confidence", type=float, default=0.35)
    parser.add_argument("--closeup", action="store_true")
    args = parser.parse_args()
    try:
        print(json.dumps(run_detection(args.camera, max(1, args.frames), args.confidence, args.closeup)))
    except Exception as exc:
        print(json.dumps({"error": str(exc), "camera": args.camera}))
        raise SystemExit(1)


if __name__ == "__main__":
    main()
