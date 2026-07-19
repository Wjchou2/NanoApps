

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

The Video Player app plays streamed, silent `.hbv` files from
`/Apps/Data/Videos`. Convert MP4/MOV/etc. on the host with FFmpeg:

```sh
tools/make_hbv.py input.mp4 data/Videos/MyVideo.hbv
```

The default is letterboxed 240x320 video at 12 fps. You can change the rate up
to 30 fps with `--fps`, trading file size and decode load for smoother motion.
Install/copy the resulting `.hbv` file to the iPod's
`/Apps/Data/Videos` directory. Audio is not yet supported because the current
homebrew SDK has no synchronized streaming-audio API.
