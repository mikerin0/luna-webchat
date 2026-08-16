import os
import time
import cv2
import numpy as np
from pathlib import Path

ROOT = Path(__file__).resolve().parent
OUT = ROOT / "latest.jpg"


def main():
    import gi
    gi.require_version("Gst", "1.0")
    from gi.repository import Gst

    Gst.init(None)
    cam_name = "/base/axi/pcie@1000120000/rp1/i2c@80000/imx708@1a"
    width = 1536
    height = 864
    pipeline = (
        f'libcamerasrc camera-name="{cam_name}" ! '
        f'video/x-raw,format=NV12,width={width},height={height} ! '
        f'videoconvert ! tee name=t '
        f't. ! queue leaky=downstream max-size-buffers=1 ! '
        f'videoconvert ! videoscale ! video/x-raw,format=RGB,width={width},height={height} ! '
        f'appsink name=finger_sink emit-signals=false sync=false drop=true max-buffers=1 '
        f't. ! queue leaky=downstream max-size-buffers=10 ! '
        f'videoscale ! video/x-raw,width=640,height=640 ! '
        f'hailonet hef-path=/usr/local/hailo/resources/models/hailo8/yolov8m_pose.hef force-writable=true ! '
        f'hailofilter name=pose_filter so-path=/usr/local/hailo/resources/so/libyolov8pose_postprocess.so ! '
        f'hailotracker ! hailooverlay ! '
        f'videoconvert ! videoscale ! video/x-raw,format=RGB,width={width},height={height} ! '
        f'videoconvert ! appsink name=preview_sink emit-signals=false sync=false drop=true max-buffers=1'
    )
    pipe = Gst.parse_launch(pipeline)
    preview_sink = pipe.get_by_name("preview_sink")
    pipe.set_state(Gst.State.PLAYING)
    try:
        for _ in range(20):
            sample = preview_sink.emit("try-pull-sample", int(0.05 * Gst.SECOND))
            if sample is None:
                continue
            buf = sample.get_buffer()
            caps = sample.get_caps()
            struct = caps.get_structure(0)
            w = int(struct.get_value("width"))
            h = int(struct.get_value("height"))
            ok, mapinfo = buf.map(Gst.MapFlags.READ)
            if not ok:
                continue
            try:
                frame_rgb = np.frombuffer(mapinfo.data, dtype=np.uint8).reshape((h, w, 3))
                frame_bgr = cv2.cvtColor(frame_rgb, cv2.COLOR_RGB2BGR)
                cv2.imwrite(str(OUT), frame_bgr)
                print("wrote", OUT)
                break
            finally:
                buf.unmap(mapinfo)
    finally:
        pipe.set_state(Gst.State.NULL)


if __name__ == "__main__":
    main()
