# Datadog dashboard assets

`homey-airwave.gif` is a deterministic, eight-frame status illustration for
the Homey thermostat dashboard. It communicates the specific Airwave state:
the compressor is stopped while the blower continues moving residual cool air.

Regenerate it on macOS with FFmpeg installed:

```sh
bash assets/datadog/render-homey-airwave.sh
```

The source is deliberately procedural so the visual remains reviewable,
reproducible, and independent of a proprietary design file.
