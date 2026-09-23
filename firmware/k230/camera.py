"""GC2093 to hardware JPEG on CanMV v1.8 (explicit encoder channel API)."""

import gc


class Camera:
    def __init__(self):
        self.sensor = None
        self.encoder = None
        self.link = None
        self.stream = None
        self.created = False
        self.started = False
        self.width = 0
        self.height = 0
        self.quality = 0

    def open(self, width=640, height=480, quality=80, fps=30):
        from media.sensor import Sensor
        from media.vencoder import Encoder, ChnAttrStr, StreamData
        from media.media import MediaManager, VIDEO_ENCODE_MOD_ID, VENC_DEV_ID

        self.close()
        try:
            self.sensor = Sensor()
            self.sensor.reset()
            self.sensor.set_framesize(width=width, height=height, alignment=12)
            self.sensor.set_pixformat(Sensor.YUV420SP)
            # v1.8's public set_framerate is a no-op; the channel rate is effective.
            self.sensor._set_chn_fps(chn=0, fps=max(5, min(60, fps or 60)))
            self.encoder = Encoder()
            self.encoder.SetOutBufs(0, 2, width, height)
            self.link = MediaManager.link(
                self.sensor.bind_info()["src"], (VIDEO_ENCODE_MOD_ID, VENC_DEV_ID, 0)
            )
            attr = ChnAttrStr(
                self.encoder.PAYLOAD_TYPE_JPEG, 0, width, height,
                mjpeg_quality_factor=max(1, min(99, quality)),
            )
            self.encoder.Create(0, attr)
            self.created = True
            self.encoder.Start(0)
            self.started = True
            self.sensor.run()
            self.stream = StreamData()
            self.width, self.height, self.quality = width, height, quality
        except BaseException:
            self.close()
            raise

    def capture(self, timeout_ms=20):
        import uctypes

        if self.encoder.GetStream(0, self.stream, timeout=timeout_ms) != 0:
            return None
        try:
            # Copy payload before network I/O; hardware buffers become invalid at release.
            return b"".join(
                uctypes.bytes_at(self.stream.data[i], self.stream.data_size[i])
                for i in range(self.stream.pack_cnt)
            )
        finally:
            self.encoder.ReleaseStream(0, self.stream)

    def close(self):
        if self.sensor is not None:
            try:
                self.sensor.stop()
            except Exception:
                pass
        unlink_error = None
        if self.link is not None:
            # v1.8 roots links in C; deleting a Python reference does not unbind.
            link = self.link
            self.link = None
            try:
                if not link.destroy():
                    unlink_error = RuntimeError("Camera encoder link could not be released")
            except Exception as exc:
                unlink_error = exc
        if self.encoder is not None:
            if self.started:
                try:
                    self.encoder.Stop(0)
                except Exception:
                    pass
            if self.created:
                try:
                    self.encoder.Destroy(0)
                except Exception:
                    pass
            # SetOutBufs allocates even when channel creation later fails.
            pool = getattr(self.encoder, "private_poolid", -1)
            if pool != -1:
                from mpp.vb import kd_mpi_vb_destory_pool
                kd_mpi_vb_destory_pool(pool)
                self.encoder.private_poolid = -1
        self.sensor = None
        self.encoder = None
        self.stream = None
        self.started = self.created = False
        gc.collect()
        if unlink_error is not None:
            raise unlink_error
