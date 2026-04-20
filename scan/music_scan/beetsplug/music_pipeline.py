# Re-export the plugin class into the beetsplug namespace so beets can load it
# via pluginpath: /app/music_scan (which adds /app/music_scan to sys.path, and
# Python's namespace-package machinery then finds beetsplug.music_pipeline here).
from music_scan.music_pipeline import MusicPipelinePlugin  # noqa: F401
