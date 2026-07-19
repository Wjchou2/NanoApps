

All code belongs to 
https://github.com/nfzerox/NanoApps


Small test/changes to make NanoApps work with a Mac partitioned HFS+ iPod.

After flashing ipod_sun_untethered, disable journalling:

```
diskutil list
sudo diskutil disableJournal /dev/disk4s2
diskutil eject /dev/disk4
```

Everything else should work the same, using this repo.

## Video Player

The Video Player app plays streamed `.hbv` files from `/Apps/Data/Videos`,
with audio when the converter finds a soundtrack. Convert MP4/MOV/etc. on the
host with FFmpeg:

```sh
tools/make_hbv.py input.mp4 data/Videos/MyVideo.hbv
```

The default is letterboxed 240x320 video at 12 fps. You can change the rate up
to 30 fps with `--fps`, trading file size and decode load for smoother motion.
The converter also creates `MyVideo.audio/` beside `MyVideo.hbv`; copy both to
the iPod's `/Apps/Data/Videos` directory. Audio is mono, 22.05 kHz PCM split
into one-second WAV chunks so it can stream through the firmware's sound-effect
player. Use `--no-audio` for a silent conversion.

The firmware does not expose a proven stop/seek call for these WAVs. Pause,
restart, loop, and leaving the player therefore let the current audio chunk
finish first (at most about one second), then playback resumes in sync at a
chunk boundary.
