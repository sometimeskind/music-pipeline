"""Flask application factory and route definitions."""

from __future__ import annotations

import io
import os
import zipfile
from datetime import datetime, timezone
from pathlib import Path

from flask import Flask, jsonify, request, send_file

from music_service.auth import setup_auth


def _list_dir(root: Path) -> list[dict]:
    """Return a JSON-serialisable listing of all files under *root*."""
    files = []
    if root.exists():
        for f in sorted(root.rglob("*")):
            if f.is_file():
                stat = f.stat()
                files.append(
                    {
                        "name": str(f.relative_to(root)),
                        "size": stat.st_size,
                        "modified": datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).isoformat(),
                    }
                )
    return files


def create_app(orchestrator) -> Flask:
    """Application factory.  Inject an Orchestrator instance for testability."""
    app = Flask(__name__)
    setup_auth(app)

    inbox = Path(os.environ.get("MUSIC_INBOX", "/root/Music/inbox"))
    quarantine = Path(os.environ.get("MUSIC_QUARANTINE", "/root/Music/quarantine"))

    # ------------------------------------------------------------------
    # Health
    # ------------------------------------------------------------------

    @app.get("/health")
    def health():
        return jsonify({"status": "ok"})

    # ------------------------------------------------------------------
    # Inbox
    # ------------------------------------------------------------------

    @app.get("/inbox")
    def inbox_list():
        return jsonify(_list_dir(inbox))

    @app.post("/inbox/upload")
    def inbox_upload():
        data = request.get_data()
        if not data:
            return jsonify({"error": "empty body"}), 400
        try:
            with zipfile.ZipFile(io.BytesIO(data)) as zf:
                zf.extractall(inbox)
        except zipfile.BadZipFile:
            return jsonify({"error": "invalid zip"}), 400
        orchestrator.schedule_scan()
        return jsonify({}), 200

    # ------------------------------------------------------------------
    # Quarantine
    # ------------------------------------------------------------------

    @app.get("/quarantine")
    def quarantine_list():
        return jsonify(_list_dir(quarantine))

    @app.get("/quarantine/download/<path:name>")
    def quarantine_download(name: str):
        # Guard against path traversal
        try:
            target = (quarantine / name).resolve()
        except Exception:
            return jsonify({"error": "invalid path"}), 400

        quarantine_resolved = quarantine.resolve()
        if not str(target).startswith(str(quarantine_resolved) + os.sep) and target != quarantine_resolved:
            return jsonify({"error": "forbidden"}), 403

        if not target.exists():
            return jsonify({"error": "not found"}), 404

        if target.is_file():
            return send_file(target)

        # Directory: zip on-the-fly
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            for f in sorted(target.rglob("*")):
                if f.is_file():
                    zf.write(f, f.relative_to(target.parent))
        buf.seek(0)
        return send_file(
            buf,
            as_attachment=True,
            download_name=f"{name}.zip",
            mimetype="application/zip",
        )

    # ------------------------------------------------------------------
    # Triggers
    # ------------------------------------------------------------------

    @app.post("/fetch/trigger")
    def fetch_trigger():
        if not orchestrator.try_run_fetch():
            return jsonify({"error": "busy"}), 409
        return jsonify({}), 202

    @app.post("/scan/trigger")
    def scan_trigger():
        if not orchestrator.try_run_scan():
            return jsonify({"error": "busy"}), 409
        return jsonify({}), 202

    return app
