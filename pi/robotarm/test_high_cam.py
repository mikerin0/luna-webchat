import gi
gi.require_version('Gst', '1.0')
from gi.repository import Gst, GLib

Gst.init(None)

pipeline_str = 'libcamerasrc camera-name="/base/axi/pcie@1000120000/rp1/i2c@80000/imx708@1a" ! autovideosink'
print(f"Launching pipeline: {pipeline_str}")
pipeline = Gst.parse_launch(pipeline_str)

pipeline.set_state(Gst.State.PLAYING)

# Run the main loop to keep the pipeline alive
loop = GLib.MainLoop()
try:
    loop.run()
except KeyboardInterrupt:
    pass

pipeline.set_state(Gst.State.NULL)
