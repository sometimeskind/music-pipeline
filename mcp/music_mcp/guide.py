"""The domain guide: markdown in mcp/guide/, one topic per file, served by get_guide.

The Claude Code music-library skill reads the same files, so this is the only copy.
"""

from __future__ import annotations

from pathlib import Path

# mcp/guide/, next to the package both in the source tree and in the image (/app).
GUIDE_DIR = Path(__file__).resolve().parent.parent / "guide"


class Guide:
    def __init__(self, root: Path = GUIDE_DIR) -> None:
        # Read once at start: the topics are fixed by the image, and a name never reaches open().
        self._topics = {p.stem: p.read_text(encoding="utf-8") for p in sorted(root.glob("*.md"))}
        if "index" not in self._topics:
            raise RuntimeError(f"no index.md in {root}")

    @property
    def topics(self) -> list[str]:
        return list(self._topics)

    def get(self, topic: str) -> dict:
        key = (topic or "index").strip().lower().removesuffix(".md")
        if key not in self._topics:
            return {"status": "not_found", "topic": topic, "topics": self.topics}
        return {"status": "ok", "topic": key, "topics": self.topics, "markdown": self._topics[key]}
