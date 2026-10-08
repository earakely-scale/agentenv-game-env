# Streams or records a game env's match: Chromium shows the overlay, which frames the match's spectator view, on a
# virtual display and plays its sound into PulseAudio, and ffmpeg encodes both (stream.py).
# Build: the start_broadcast step builds it, tagged agentenv-game-streamer:<a digest of this directory's files>
FROM debian:bookworm-slim
RUN apt-get -o Acquire::Retries=5 update \
 && apt-get -o Acquire::Retries=5 install -y --no-install-recommends chromium xvfb ffmpeg python3 fonts-dejavu-core \
    fonts-noto-core ca-certificates pulseaudio pulseaudio-utils \
 && rm -rf /var/lib/apt/lists/* \
 && useradd -m streamer
COPY stream.py serve.py /app/
COPY overlay /app/overlay
USER streamer
ENTRYPOINT ["python3", "/app/stream.py"]
