# AV1 smoke fixture

`av1.mp4` is a synthetic 64×48 red video (six frames, 1.2 seconds), used to test decoding without adding an AV1 encoder to the runtime image. The smoke checks also remux it into WebM.

Generate it with an FFmpeg build that includes SVT-AV1:

```sh
ffmpeg -f lavfi -i color=red:size=64x48:rate=5 -t 1.2 -c:v libsvtav1 -preset 12 -crf 50 docker/fixtures/av1.mp4
```
