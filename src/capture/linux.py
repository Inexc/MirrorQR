"""UNVERIFIED portal/PipeWire adapter; no synthetic or X11 fallback."""
import asyncio
from fractions import Fraction
import time

from aiortc import VideoStreamTrack
from aiortc.mediastreams import MediaStreamError
from av import VideoFrame

from .portal import PortalSession


class LinuxCaptureTrack(VideoStreamTrack):
    def __init__(self, portal, pipeline, sink, Gst, GstVideo):
        super().__init__()
        self.portal = portal
        self.pipeline = pipeline
        self.sink = sink
        self.Gst = Gst
        self.GstVideo = GstVideo
        self.loop = asyncio.get_running_loop()
        self.started = time.monotonic()
        self.last_pts = -1
        self.cleanup_task = None

    async def recv(self):
        if self.readyState != 'live':
            raise MediaStreamError
        sample = await asyncio.to_thread(self.sink.emit, 'try-pull-sample', self.Gst.SECOND)
        if sample is None or self.readyState != 'live':
            self.stop()
            raise MediaStreamError
        info = self.GstVideo.VideoInfo.new_from_caps(sample.get_caps())
        buffer = sample.get_buffer()
        raw = buffer.extract_dup(0, buffer.get_size())
        frame = VideoFrame(info.width, info.height, 'bgra')
        plane = frame.planes[0]
        data = bytearray(plane.buffer_size)
        row_bytes = info.width * 4
        for row in range(info.height):
            source = info.offset[0] + row * info.stride[0]
            target = row * plane.line_size
            data[target:target + row_bytes] = raw[source:source + row_bytes]
        plane.update(data)
        self.last_pts = max(self.last_pts + 1, int((time.monotonic() - self.started) * 90000))
        frame.pts = self.last_pts
        frame.time_base = Fraction(1, 90000)
        return frame

    def stop(self):
        if self.readyState == 'ended':
            return
        super().stop()
        self.pipeline.set_state(self.Gst.State.NULL)
        if not self.loop.is_closed():
            # Backend owns this loop; cleanup must finish before it closes.
            self.cleanup_task = self.loop.create_task(self.portal.close())


async def create_linux_track():
    try:
        import gi
        gi.require_version('Gst', '1.0')
        gi.require_version('GstVideo', '1.0')
        from gi.repository import Gst, GstVideo
    except (ImportError, ValueError) as exc:
        raise RuntimeError('Нет PyGObject/GStreamer 1.0 с GstVideo; установите системные пакеты Linux.') from exc
    Gst.init(None)
    for name in ('pipewiresrc', 'videoconvert', 'appsink'):
        if Gst.ElementFactory.find(name) is None:
            raise RuntimeError('В GStreamer отсутствует системный компонент: ' + name)
    portal = PortalSession()
    pipeline = None
    try:
        fd, node = await portal.open()
        pipeline = Gst.parse_launch(
            f'pipewiresrc fd={fd} path={node} do-timestamp=true ! '
            'videoconvert ! video/x-raw,format=BGRA ! '
            'appsink name=frames max-buffers=1 drop=true sync=false')
        sink = pipeline.get_by_name('frames')
        if pipeline.set_state(Gst.State.PLAYING) == Gst.StateChangeReturn.FAILURE:
            raise RuntimeError('PipeWire не смог открыть выбранный экран.')
        return LinuxCaptureTrack(portal, pipeline, sink, Gst, GstVideo)
    except BaseException:
        if pipeline is not None:
            pipeline.set_state(Gst.State.NULL)
        await portal.close()
        raise
