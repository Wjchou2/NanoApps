Video Player data directory

Put NanoApps .hbv video files and their matching .audio directories here.
Convert a regular video from the repository root with:

  tools/make_hbv.py input.mp4 data/Videos/MyVideo.hbv

For example, MyVideo.hbv uses audio from MyVideo.audio/. The converter creates
one-second, mono WAV chunks automatically when the input contains audio. Use
--no-audio for silent output. The default video is 240x320 at 12 fps.
