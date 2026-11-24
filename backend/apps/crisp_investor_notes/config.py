# Supported audio and video containers for Crisp Notes.
# Video audio extraction uses PyAV's bundled libraries; no external ffmpeg.exe.
ALLOWED_AUDIO_EXTENSIONS = {
    ".mp3", ".mp2", ".mpga", ".m4a", ".wav", ".wave", ".webm",
    ".ogg", ".oga", ".opus", ".flac", ".aac", ".amr", ".aif",
    ".aiff", ".caf", ".wma",
}
ALLOWED_VIDEO_EXTENSIONS = {
    ".webm", ".mp4", ".m4v", ".mov", ".mkv", ".avi", ".mpeg", ".mpg",
    ".wmv", ".3gp",
}
