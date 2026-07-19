#!/usr/bin/env python3
"""Convert a video to NanoApps' streamed HBVID1 format.

Usage:
  tools/make_hbv.py movie.mp4 movie.hbv
  tools/make_hbv.py movie.mp4 movie.hbv --fps 15
  tools/make_hbv.py --demo demo.hbv

The converter uses ffmpeg for normal input. Output is silent RGB565 video,
letterboxed to at most 240x320, with delta COPY/SKIP compression and periodic
keyframes. Copy the result to /Apps/Data/Videos on the iPod.
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import shutil
import struct
import subprocess
import sys
import tempfile
from typing import Iterable, Iterator

MAGIC = b"HBVID1\0\0"
HEADER = struct.Struct("<8sIIIIII")
WIDTH = 240
HEIGHT = 320
MAX_FPS = 30
MAX_TOKEN = 0x7FFF


def read_exact(pipe, size: int) -> bytes:
    chunks: list[bytes] = []
    remaining = size
    while remaining:
        chunk = pipe.read(remaining)
        if not chunk:
            break
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def ffmpeg_frames(source: Path, fps: int, width: int, height: int) -> Iterator[bytes]:
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise RuntimeError("ffmpeg is required (macOS: brew install ffmpeg)")

    vf = (
        f"fps={fps},"
        f"scale={width}:{height}:force_original_aspect_ratio=decrease,"
        f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:black"
    )
    cmd = [
        ffmpeg, "-hide_banner", "-loglevel", "error", "-i", str(source),
        "-an", "-sn", "-dn", "-vf", vf,
        "-f", "rawvideo", "-pix_fmt", "rgb565le", "pipe:1",
    ]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    assert proc.stdout is not None and proc.stderr is not None
    frame_bytes = width * height * 2
    try:
        while True:
            frame = read_exact(proc.stdout, frame_bytes)
            if not frame:
                break
            if len(frame) != frame_bytes:
                raise RuntimeError("ffmpeg produced a truncated final frame")
            yield frame
        error = proc.stderr.read().decode("utf-8", "replace").strip()
        status = proc.wait()
        if status:
            raise RuntimeError(error or f"ffmpeg exited with status {status}")
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()


def rgb565(r: int, g: int, b: int) -> bytes:
    value = ((r & 0xF8) << 8) | ((g & 0xFC) << 3) | (b >> 3)
    return struct.pack("<H", value)


def fill_rect(frame: bytearray, width: int, height: int,
              x: int, y: int, w: int, h: int, color: bytes) -> None:
    x0, y0 = max(0, x), max(0, y)
    x1, y1 = min(width, x + w), min(height, y + h)
    if x0 >= x1 or y0 >= y1:
        return
    row = color * (x1 - x0)
    for yy in range(y0, y1):
        start = (yy * width + x0) * 2
        frame[start:start + len(row)] = row


def demo_frames(fps: int, width: int, height: int) -> Iterator[bytes]:
    """Small dependency-free animation used as installed demo/test media."""
    colors = [
        rgb565(255, 55, 95), rgb565(255, 159, 10), rgb565(48, 209, 88),
        rgb565(10, 132, 255), rgb565(191, 90, 242),
    ]
    dark = rgb565(7, 10, 18)
    white = rgb565(238, 241, 248)
    total = fps * 4
    for n in range(total):
        frame = bytearray(dark * (width * height))
        # Static cinema-frame border and five color bars.
        fill_rect(frame, width, height, 5, 5, width - 10, 3, white)
        fill_rect(frame, width, height, 5, height - 8, width - 10, 3, white)
        for i, color in enumerate(colors):
            fill_rect(frame, width, height, 16 + i * 43, 16, 28, 10, color)
        # A bouncing "playhead" and a progress strip make frame timing obvious.
        span_x, span_y = width - 44, height - 82
        phase = n if n < total // 2 else total - 1 - n
        x = 10 + (phase * span_x * 2) // max(1, total)
        y = 34 + ((n * 7) % max(1, span_y))
        fill_rect(frame, width, height, x, y, 28, 28, colors[(n // max(1, fps)) % len(colors)])
        fill_rect(frame, width, height, 10, height - 24,
                  (width - 20) * (n + 1) // total, 8, rgb565(10, 132, 255))
        yield bytes(frame)


def same_pixel(frame: bytes, previous: bytes, pixel: int) -> bool:
    pos = pixel * 2
    return frame[pos] == previous[pos] and frame[pos + 1] == previous[pos + 1]


def encode_delta(frame: bytes, previous: bytes) -> bytes:
    """Encode a complete frame as COPY/SKIP runs against ``previous``."""
    pixels = len(frame) // 2
    out = bytearray()
    i = 0
    while i < pixels:
        equal = same_pixel(frame, previous, i)
        start = i
        i += 1
        while i < pixels and i - start < MAX_TOKEN and same_pixel(frame, previous, i) == equal:
            i += 1
        count = i - start
        if equal:
            out += struct.pack("<H", count)
        else:
            out += struct.pack("<H", 0x8000 | count)
            out += frame[start * 2:i * 2]
    return bytes(out)


def write_hbv(output: Path, frames: Iterable[bytes], width: int, height: int,
              fps: int, key_seconds: int) -> tuple[int, int, int]:
    frame_size = width * height * 2
    frame_ms = max(16, (1000 + fps // 2) // fps)
    key_every = max(1, fps * key_seconds)
    black = bytes(frame_size)
    previous = black
    frame_count = 0
    max_packet = 0

    output.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=output.name + ".", suffix=".tmp", dir=output.parent)
    try:
        with os.fdopen(fd, "w+b") as fp:
            fp.write(b"\0" * HEADER.size)
            for frame in frames:
                if len(frame) != frame_size:
                    raise RuntimeError(f"frame {frame_count} has {len(frame)} bytes, expected {frame_size}")
                keyframe = frame_count % key_every == 0
                packet = encode_delta(frame, black if keyframe else previous)
                max_packet = max(max_packet, len(packet))
                length = len(packet) | (0x80000000 if keyframe else 0)
                fp.write(struct.pack("<I", length))
                fp.write(packet)
                previous = frame
                frame_count += 1
            if frame_count == 0:
                raise RuntimeError("the input contained no video frames")
            fp.seek(0)
            fp.write(HEADER.pack(MAGIC, width, height, frame_count,
                                 frame_ms, 2, max_packet))
            fp.flush()
            os.fsync(fp.fileno())
        os.replace(temp_name, output)
        os.chmod(output, 0o644)
    except Exception:
        try:
            os.unlink(temp_name)
        except FileNotFoundError:
            pass
        raise
    return frame_count, frame_ms, output.stat().st_size


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("paths", nargs="+", metavar="PATH")
    parser.add_argument("--demo", action="store_true",
                        help="generate the built-in animated demo; PATH is only the output")
    parser.add_argument("--fps", type=int, default=12, help="output FPS (default: 12, max: 30)")
    parser.add_argument("--width", type=int, default=WIDTH, help="frame width (max: 240)")
    parser.add_argument("--height", type=int, default=HEIGHT, help="frame height (max: 320)")
    parser.add_argument("--key-seconds", type=int, default=5,
                        help="keyframe interval in seconds (default: 5)")
    args = parser.parse_args(argv)

    if not 1 <= args.fps <= MAX_FPS:
        parser.error(f"--fps must be between 1 and {MAX_FPS}")
    if not 1 <= args.width <= WIDTH or not 1 <= args.height <= HEIGHT:
        parser.error(f"dimensions must fit within {WIDTH}x{HEIGHT}")
    if args.key_seconds < 1:
        parser.error("--key-seconds must be positive")

    if args.demo:
        if len(args.paths) != 1:
            parser.error("--demo expects one output path")
        output = Path(args.paths[0])
        frames = demo_frames(args.fps, args.width, args.height)
    else:
        if len(args.paths) != 2:
            parser.error("conversion expects INPUT OUTPUT")
        source, output = Path(args.paths[0]), Path(args.paths[1])
        if not source.is_file():
            parser.error(f"input does not exist: {source}")
        frames = ffmpeg_frames(source, args.fps, args.width, args.height)

    try:
        count, frame_ms, size = write_hbv(output, frames, args.width, args.height,
                                          args.fps, args.key_seconds)
    except (OSError, RuntimeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    seconds = count * frame_ms / 1000.0
    print(f"wrote {output}: {args.width}x{args.height}, {count} frames, "
          f"{seconds:.1f}s, {size / (1024 * 1024):.2f} MiB, silent")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
