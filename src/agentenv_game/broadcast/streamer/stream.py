"""Streams a game env's match to RTMP servers such as Twitch and X, or records it, or both. The overlay (serve.py,
overlay/) frames the match's spectator view with the broadcast's furniture; Chromium shows it full screen on a virtual
display and plays its sound into a PulseAudio null sink; ffmpeg encodes the display and the sink once, to each RTMP
URL in STREAM_URL (one a line) and, with --record, to FOLDER/stream-<UTC time>.mkv, which becomes an .mp4 once the
stream ends. FOLDER/config.json is the presentation (the start_broadcast step writes it). It waits for the env to
answer, and stops `--linger` seconds after the match is over (or replaced by a new lobby's), or once the env has been
gone for a minute. STREAM_URL holds the stream keys: nothing it prints shows them. Stdlib only."""

from __future__ import annotations

import argparse
import datetime
import json
import os
import re
import signal
import socket
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from pathlib import Path

import serve

SINK = "broadcast"
POLL_SECONDS = 5
GONE_SECONDS = 60
OVER = ("finished", "cancelled", "failed")


def redacted(line: str, secrets: dict[str, str]) -> str:
    """`line` with each secret replaced by its name, e.g. <stream key>."""
    for secret, name in secrets.items():
        if secret:
            line = line.replace(secret, f"<{name}>")
    return line


def secrets_of(targets: list[str]) -> dict[str, str]:
    """What must not show in the output: each RTMP URL's last path segment, its stream key."""
    return {target.rsplit("/", 1)[-1].split("?")[0]: "stream key" for target in targets}


def relay(stream, secrets: dict[str, str]) -> None:
    """Prints a child's output a line at a time as it comes, redacted: ffmpeg ends each progress line with \\r."""
    rest = b""
    while True:
        chunk = stream.read1(65536)
        *lines, rest = re.split(rb"[\r\n]", rest + chunk) if chunk else (rest, b"")
        for line in filter(bytes.strip, lines):
            print(redacted(line.decode(errors="replace").rstrip(), secrets), flush=True)
        if not chunk:
            return


def ended(state: dict, lobby_id: str | None) -> str | None:
    """Why the match being broadcast is over, if it is: its status, or a new lobby in its place."""
    match = state.get("match") or {}
    if lobby_id is not None and match.get("lobby_id") not in (None, lobby_id):
        return "replaced by a new lobby's"
    return match.get("status") if match.get("status") in OVER else None


def follow(watch, linger: float, running: Callable[[], bool], now: Callable[[], float] = time.monotonic,
           sleep: Callable[[float], None] = time.sleep) -> bool:
    """Watches the match while `running()`, until the stream should end: `linger` seconds after the match is over,
    or once the env has been gone GONE_SECONDS. Whether the match ended."""
    lobby_id = (watch.snapshot().get("match") or {}).get("lobby_id")
    over_since = None
    while running():
        sleep(POLL_SECONDS)
        t, state = now(), watch.snapshot()
        lobby_id = lobby_id or (state.get("match") or {}).get("lobby_id")
        if over_since is None and (why := ended(state, lobby_id)):
            over_since = t
            print(f"The match is {why}; the overlay stays on for {linger:.0f} s", flush=True)
        if over_since is not None and t - over_since >= linger:
            print("Ending the stream", flush=True)
            break
        if watch.answered is None or t - watch.answered >= GONE_SECONDS:
            print(f"The env has not answered for {GONE_SECONDS} s; ending the stream", flush=True)
            break
    return over_since is not None


def free_port() -> int:
    """A loopback port nothing listens on: with --network host, two streams on one machine share the host's ports."""
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def ffmpeg_command(display: str, size: str, fps: int, bitrate: str, targets: list[str],
                   record: Path | None) -> list[str]:
    """ffmpeg encoding the display and the sink's sound once, for the RTMP targets, the recording, or both: with
    more than one, the tee muxer keeps the others going when an RTMP leg fails."""
    rate = int(bitrate.rstrip("k"))
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "warning", "-stats", "-stats_period", "60",
           "-thread_queue_size", "512", "-f", "x11grab", "-video_size", size, "-framerate", str(fps),
           "-draw_mouse", "0", "-i", display,
           "-thread_queue_size", "512", "-f", "pulse", "-i", f"{SINK}.monitor", "-map", "0:v", "-map", "1:a",
           "-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p",
           "-b:v", bitrate, "-maxrate", bitrate, "-bufsize", f"{2 * rate}k", "-g", str(2 * fps),
           "-c:a", "aac", "-b:a", "128k", "-ar", "44100"]
    legs = [f"[f=flv:onfail=ignore]{target}" for target in targets] + ([f"[f=matroska]{record}"] if record else [])
    if len(legs) > 1:
        return [*cmd, "-flags", "+global_header", "-f", "tee", "|".join(legs)]
    return [*cmd, "-f", "flv", targets[0]] if targets else [*cmd, "-f", "matroska", str(record)]


def remux_command(mkv: Path) -> list[str]:
    """The recording copied, not re-encoded, into an MP4 whose index comes first, so it plays before it has loaded."""
    return ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", str(mkv), "-map", "0", "-c", "copy",
            "-movflags", "+faststart", str(mkv.with_suffix(".mp4"))]


def remux(mkv: Path) -> Path | None:
    """The finished recording as an MP4, the Matroska file (which survives a crash) removed once it has one."""
    if not mkv.is_file() or not mkv.stat().st_size:
        return None
    mp4 = mkv.with_suffix(".mp4")
    if subprocess.run(remux_command(mkv), stdin=subprocess.DEVNULL).returncode or not mp4.is_file():
        print(f"Could not make an MP4 of {mkv}; it stays as it is", flush=True)
        return None
    mkv.unlink()
    return mp4


def start_xvfb(size: str) -> tuple[subprocess.Popen, str]:
    """A virtual display on the first free display number, and that number: with --network host, X's sockets are the
    host's, so another streamer on the machine may hold one already."""
    width, height = size.split("x")
    read, write = os.pipe()
    proc = subprocess.Popen(["Xvfb", "-displayfd", str(write), "-screen", "0", f"{width}x{height}x24",
                             "-nolisten", "tcp"], pass_fds=(write,))
    os.close(write)
    with os.fdopen(read) as chosen:
        number = chosen.readline().strip()
    if not number:
        proc.kill()
        raise RuntimeError("Xvfb did not start")
    return proc, f":{number}"


def start_pulseaudio(env: dict) -> subprocess.Popen:
    """PulseAudio with one null sink as the default output: Chromium plays into it and ffmpeg records its monitor,
    which is silence while nothing plays."""
    Path(env["XDG_RUNTIME_DIR"]).mkdir(mode=0o700, exist_ok=True)
    proc = subprocess.Popen(
        ["pulseaudio", "--daemonize=no", "--exit-idle-time=-1", "--disallow-exit", "-n",
         "--load=module-native-protocol-unix",
         f"--load=module-null-sink sink_name={SINK} sink_properties=device.description=Broadcast"],
        env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(50):
        if not subprocess.run(["pactl", "set-default-sink", SINK], env=env, capture_output=True).returncode:
            return proc
        time.sleep(0.2)
    proc.kill()
    raise RuntimeError("PulseAudio did not start")


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--env", required=True, help="the game env's address, e.g. http://127.0.0.1:41589")
    p.add_argument("--folder", required=True, type=Path, help="holds config.json; gets the recording")
    p.add_argument("--size", default="1920x1080")
    p.add_argument("--fps", type=int, default=30)
    p.add_argument("--bitrate", default="4500k")
    p.add_argument("--linger", type=float, default=60)
    p.add_argument("--record", action="store_true", help="also write the stream to FOLDER/stream-<UTC time>.mkv")
    args = p.parse_args()
    signal.signal(signal.SIGTERM, signal.default_int_handler)   # docker stop ends the stream as Ctrl-C does
    targets = os.environ.get("STREAM_URL", "").split()
    if not targets and not args.record:
        print("Nothing to do: set STREAM_URL to stream, or --record to record", flush=True)
        return 2
    if not os.access(args.folder, os.W_OK):
        print(f"Cannot write to {args.folder} as this container's user", flush=True)
        return 2
    secrets = secrets_of(targets)
    width, height = args.size.split("x")
    watch = serve.Watch(args.env, json.loads((args.folder / "config.json").read_text()))

    print(f"Waiting for {args.env}", flush=True)
    while not watch.poll():
        time.sleep(POLL_SECONDS)
    stop = threading.Event()
    threading.Thread(target=watch.run, args=(1.0, stop), daemon=True).start()
    port = free_port()
    serve.serve(watch, port)

    xvfb, display = start_xvfb(args.size)
    env = {**os.environ, "DISPLAY": display, "XDG_RUNTIME_DIR": "/tmp/pulse-runtime"}
    procs = [xvfb, start_pulseaudio(env)]
    procs.append(subprocess.Popen(
        ["chromium", "--no-sandbox", "--disable-gpu", "--disable-dev-shm-usage", "--no-first-run", "--noerrdialogs",
         "--disable-infobars", "--hide-scrollbars", "--kiosk", "--window-position=0,0",
         "--autoplay-policy=no-user-gesture-required", f"--window-size={width},{height}",
         f"--app=http://127.0.0.1:{port}/"],
        env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL))
    time.sleep(8)
    stamp = f"{datetime.datetime.now(datetime.UTC):%Y%m%dT%H%M%SZ}"
    record = args.folder / f"stream-{stamp}.mkv" if args.record else None
    ffmpeg = subprocess.Popen(ffmpeg_command(display, args.size, args.fps, args.bitrate, targets, record), env=env,
                              stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    threading.Thread(target=relay, args=(ffmpeg.stderr, secrets), daemon=True).start()
    procs.append(ffmpeg)
    servers = f"to {len(targets)} RTMP server{'s' * (len(targets) > 1)}"
    where = " and ".join([*([servers] if targets else []), *([f"to {record}"] if record else [])])
    print(f"Streaming {args.env} at {args.size}, {args.fps} fps, {args.bitrate} {where}", flush=True)

    over = False
    try:
        over = follow(watch, args.linger, lambda: ffmpeg.poll() is None)
    except KeyboardInterrupt:
        print("Interrupted; ending the stream", flush=True)
    finally:
        stop.set()
        for proc in reversed(procs):
            proc.terminate()
        for proc in procs:
            try:
                proc.wait(10)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
    if record and (mp4 := remux(record)):
        print(f"Recorded {mp4}", flush=True)
    code = ffmpeg.returncode
    return 0 if code in (0, 255, -15) or over else code


if __name__ == "__main__":
    sys.exit(main())
