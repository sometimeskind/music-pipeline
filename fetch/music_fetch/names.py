"""File-name length (#225).

ext4 caps one path component at 255 *bytes*, not characters.  spotdl caps its output
names at 255 characters, so a title of combining glyphs (three bytes a character, the
Wingdings/Four Tet album) gets past it and the download dies with ``OSError: [Errno 36]
File name too long``.  beets caps library names at 200 bytes itself
(``beets.util.MAX_FILENAME_LENGTH``), so library, replace and merge paths are safe.
"""

MAX_NAME_BYTES = 255
# What a spotdl download may use.  The guard writes a ``<file>.reason`` sidecar next to
# a rejected download, so leave it room.
SPOTDL_NAME_BYTES = MAX_NAME_BYTES - 16


def truncate_utf8(text: str, limit: int) -> str:
    """*text* cut to at most *limit* UTF-8 bytes on a character boundary."""
    return text.encode("utf-8")[:limit].decode("utf-8", "ignore")
