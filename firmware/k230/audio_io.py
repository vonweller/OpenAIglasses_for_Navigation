"""CanMV v1.8 stereo microphone capture and independent mono playback."""


def extract_channel(pcm, channel=1):
    if channel not in (0, 1) or len(pcm) % 4:
        raise ValueError("Expected interleaved stereo PCM16 and channel 0 or 1")
    output = bytearray(len(pcm) // 2)
    offset = channel * 2
    # MicroPython arrays and memoryviews do not support extended slices.
    for dest in range(0, len(output), 2):
        source = dest * 2 + offset
        output[dest] = pcm[source]
        output[dest + 1] = pcm[source + 1]
    return output


def upsample_8k_to_16k(pcm):
    if len(pcm) % 2:
        raise ValueError("Expected aligned PCM16")
    output = bytearray(len(pcm) * 2)
    for source in range(0, len(pcm), 2):
        dest = source * 2
        output[dest] = output[dest + 2] = pcm[source]
        output[dest + 1] = output[dest + 3] = pcm[source + 1]
    return output


class AudioIO:
    def __init__(self, config):
        self.config = config
        self.p = None
        self.input = None
        self.output = None
        self.discard = 25
        self.input_channels = getattr(config, "MIC_CHANNELS", 1)

    def open(self):
        from media.pyaudio import PyAudio, paInt16, RIGHT, AUDIO_3A_ENABLE_ANS

        try:
            self.p = PyAudio()
            self.input = self.p.open(
                format=paInt16, channels=self.input_channels, rate=16000, input=True,
                frames_per_buffer=320,
            )
            self.input.swap_left_right(False)
            self.input.volume(self.config.MIC_VOLUME, RIGHT)
            if self.config.MIC_ENABLE_ANS:
                self.input.enable_audio3a(AUDIO_3A_ENABLE_ANS)
            if self.config.SPEAKER_ENABLED:
                self.output = self.p.open(
                    # The codec shares its clock with capture; both must use 16 kHz.
                    format=paInt16, channels=1, rate=16000, output=True,
                    frames_per_buffer=320,
                )
                self.output.volume(self.config.SPK_VOLUME)
        except BaseException:
            self.close()
            raise

    def read_mic(self):
        data = self.input.read(block=False)
        if not data:
            return None
        if self.discard:
            self.discard -= 1
            return None
        if len(data) != 640 * self.input_channels:
            raise ValueError("Unexpected microphone frame size: %d" % len(data))
        # This firmware's mono input selects the right mic before its ANS stage.
        return data if self.input_channels == 1 else extract_channel(data, self.config.MIC_CHANNEL)

    def play(self, pcm):
        if self.output is None or not pcm:
            return
        if len(pcm) % 2 or len(pcm) > 320:
            raise ValueError("Playback expects at most 20 ms of PCM16")
        if len(pcm) < 320:
            pcm = pcm + bytes(320 - len(pcm))
        result = self.output.write(upsample_8k_to_16k(pcm))
        if result not in (None, 0):
            raise OSError("Audio output rejected a frame: %s" % result)

    def close(self):
        for stream in (self.input, self.output):
            if stream is not None:
                try:
                    stream.stop_stream()
                except Exception:
                    pass
                try:
                    stream.close()
                except Exception:
                    pass
        self.input = self.output = None
        if self.p is not None:
            self.p.terminate()
            self.p = None
