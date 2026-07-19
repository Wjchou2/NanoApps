#!/usr/bin/env python3
"""Convert a video to NanoApps' streamed HBVID1 video + WAV-chunk audio.

Usage:
  tools/make_hbv.py movie.mp4 movie.hbv
  tools/make_hbv.py movie.mp4 movie.hbv --fps 15
  tools/make_hbv.py --demo demo.hbv

The converter uses ffmpeg for normal input. Video is RGB565, letterboxed to at
most 240x320, with delta COPY/SKIP compression and periodic keyframes. If the
input has audio, a matching ``<name>.audio`` directory is written beside the
.hbv file. Copy both outputs to /Apps/Data/Videos on the iPod.
"""

from __future__ import annotations

import argparse
import math
import os
from pathlib import Path
import shutil
import struct
import subprocess
import sys
import tempfile
import wave
from typing import Iterable, Iterator

MAGIC = b"HBVID1\0\0"
HEADER = struct.Struct("<8sIIIIII")
AUDIO_MAGIC = b"HBAUD1\0\0"
AUDIO_HEADER = struct.Struct("<8sIIIIII")
WIDTH = 240
HEIGHT = 320
MAX_FPS = 30
MAX_TOKEN = 0x7FFF
AUDIO_RATE = 22050
AUDIO_CHUNK_MS = 1000


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


def audio_dir_for(output: Path) -> Path:
    return output.with_suffix(".audio")


def source_has_audio(source: Path) -> bool:
    ffprobe = shutil.which("ffprobe")
    if not ffprobe:
        raise RuntimeError("ffprobe is required (it is installed with ffmpeg)")
    result = subprocess.run([
        ffprobe, "-v", "error", "-select_streams", "a:0",
        "-show_entries", "stream=index", "-of", "csv=p=0", str(source),
    ], capture_output=True, text=True)
    if result.returncode:
        raise RuntimeError(result.stderr.strip() or "ffprobe could not inspect the input")
    return bool(result.stdout.strip())


def write_wav(path: Path, pcm: bytes, sample_rate: int = AUDIO_RATE) -> None:
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(sample_rate)
        wav.writeframes(pcm)


def install_audio_dir(staging: Path, output: Path, chunk_ms: int,
                      chunks: int, total_samples: int,
                      sample_rate: int = AUDIO_RATE) -> None:
    total_ms = max(1, (total_samples * 1000 + sample_rate // 2) // sample_rate)
    (staging / "meta.hba").write_bytes(AUDIO_HEADER.pack(
        AUDIO_MAGIC, chunk_ms, chunks, total_ms, sample_rate, 1, 16))
    remove_stale_audio(output)
    os.replace(staging, output)


def remove_stale_audio(output: Path) -> None:
    if not output.exists():
        return
    meta = output / "meta.hba" if output.is_dir() else None
    if meta is None or not meta.is_file():
        raise RuntimeError(f"refusing to replace unmanaged audio path: {output}")
    with meta.open("rb") as fp:
        if fp.read(8) != AUDIO_MAGIC:
            raise RuntimeError(f"refusing to replace unmanaged audio path: {output}")
    shutil.rmtree(output)


def write_source_audio(source: Path, output: Path, video_ms: int,
                       chunk_ms: int) -> tuple[int, int]:
    """Extract mono PCM and store it as independently playable WAV chunks."""
    if not source_has_audio(source):
        remove_stale_audio(output)
        return 0, 0

    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise RuntimeError("ffmpeg is required (macOS: brew install ffmpeg)")
    output.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=output.name + ".", dir=output.parent))
    samples_per_chunk = max(1, AUDIO_RATE * chunk_ms // 1000)
    bytes_per_chunk = samples_per_chunk * 2
    duration = f"{video_ms / 1000.0:.3f}"
    cmd = [
        ffmpeg, "-hide_banner", "-loglevel", "error", "-i", str(source),
        "-map", "0:a:0", "-vn", "-sn", "-dn", "-t", duration,
        "-ac", "1", "-ar", str(AUDIO_RATE), "-c:a", "pcm_s16le",
        "-f", "s16le", "pipe:1",
    ]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    assert proc.stdout is not None and proc.stderr is not None
    chunks = 0
    total_samples = 0
    try:
        while True:
            pcm = read_exact(proc.stdout, bytes_per_chunk)
            if not pcm:
                break
            if len(pcm) & 1:
                pcm = pcm[:-1]
            if not pcm:
                break
            write_wav(staging / f"{chunks:05d}.wav", pcm)
            total_samples += len(pcm) // 2
            chunks += 1
        error = proc.stderr.read().decode("utf-8", "replace").strip()
        status = proc.wait()
        if status:
            raise RuntimeError(error or f"ffmpeg audio extraction exited with status {status}")
        if chunks == 0:
            shutil.rmtree(staging)
            remove_stale_audio(output)
            return 0, 0
        install_audio_dir(staging, output, chunk_ms, chunks, total_samples)
        return chunks, total_samples
    except Exception:
        if proc.poll() is None:
            proc.kill()
            proc.wait()
        if staging.exists():
            shutil.rmtree(staging)
        raise


def write_demo_audio(output: Path, duration_ms: int,
                     chunk_ms: int) -> tuple[int, int]:
    output.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=output.name + ".", dir=output.parent))
    total_samples = AUDIO_RATE * duration_ms // 1000
    samples_per_chunk = max(1, AUDIO_RATE * chunk_ms // 1000)
    chunks = 0
    try:
        for start in range(0, total_samples, samples_per_chunk):
            count = min(samples_per_chunk, total_samples - start)
            pcm = bytearray(count * 2)
            for i in range(count):
                t = (start + i) / AUDIO_RATE
                beat = int(t * 4.0) % 8
                frequency = (262, 330, 392, 523)[(beat // 2) % 4]
                envelope = min(1.0, (t * 4.0) % 1.0 * 8.0) * 0.22
                sample = int(32767 * envelope * math.sin(2.0 * math.pi * frequency * t))
                struct.pack_into("<h", pcm, i * 2, sample)
            write_wav(staging / f"{chunks:05d}.wav", bytes(pcm))
            chunks += 1
        install_audio_dir(staging, output, chunk_ms, chunks, total_samples)
        return chunks, total_samples
    except Exception:
        if staging.exists():
            shutil.rmtree(staging)
        raise


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
    parser.add_argument("--no-audio", action="store_true",
                        help="do not extract a soundtrack")
    parser.add_argument("--audio-chunk-ms", type=int, default=AUDIO_CHUNK_MS,
                        help="WAV chunk length in milliseconds (default: 1000)")
    args = parser.parse_args(argv)

    if not 1 <= args.fps <= MAX_FPS:
        parser.error(f"--fps must be between 1 and {MAX_FPS}")
    if not 1 <= args.width <= WIDTH or not 1 <= args.height <= HEIGHT:
        parser.error(f"dimensions must fit within {WIDTH}x{HEIGHT}")
    if args.key_seconds < 1:
        parser.error("--key-seconds must be positive")
    if not 500 <= args.audio_chunk_ms <= 5000:
        parser.error("--audio-chunk-ms must be between 500 and 5000")

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
        video_ms = count * frame_ms
        audio_dir = audio_dir_for(output)
        if args.no_audio:
            remove_stale_audio(audio_dir)
            audio_chunks = audio_samples = 0
        elif args.demo:
            audio_chunks, audio_samples = write_demo_audio(
                audio_dir, video_ms, args.audio_chunk_ms)
        else:
            audio_chunks, audio_samples = write_source_audio(
                source, audio_dir, video_ms, args.audio_chunk_ms)
    except (OSError, RuntimeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    seconds = count * frame_ms / 1000.0
    if audio_chunks:
        audio_seconds = audio_samples / AUDIO_RATE
        audio = f", audio {audio_seconds:.1f}s in {audio_chunks} WAV chunks"
    else:
        audio = ", silent"
    print(f"wrote {output}: {args.width}x{args.height}, {count} frames, "
          f"{seconds:.1f}s, {size / (1024 * 1024):.2f} MiB{audio}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
