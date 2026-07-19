

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