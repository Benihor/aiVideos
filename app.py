"""AutoVideo Studio: write a script, find licensed stock media, build a video."""

from __future__ import annotations

import json
import os
import re
import threading
import uuid
import webbrowser
from pathlib import Path

from flask import Flask, jsonify, render_template, request, send_file
from werkzeug.utils import secure_filename

from stock_media import (
    build_stock_plan,
    configured_providers,
    download_asset,
    find_asset,
    load_env_file,
    write_attribution_record,
)
from video_maker import make_video

ROOT = Path(__file__).resolve().parent
# Vercel’s filesystem is read-only except /tmp — writing outputs/ next to the app crashes the function.
ON_VERCEL = bool(os.environ.get("VERCEL") or os.environ.get("VERCEL_ENV"))
OUTPUT = Path(os.environ.get("TMPDIR") or os.environ.get("TEMP") or "/tmp") / "autovideo-outputs" if ON_VERCEL else ROOT / "outputs"
try:
    OUTPUT.mkdir(parents=True, exist_ok=True)
except OSError:
    OUTPUT = Path("/tmp") / "autovideo-outputs"
    OUTPUT.mkdir(parents=True, exist_ok=True)

load_env_file(ROOT / ".env")

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 48 * 1024 * 1024
app.config["TEMPLATES_AUTO_RELOAD"] = True
app.jinja_env.auto_reload = True

JOBS: dict[str, dict] = {}
DRAFTS: dict[str, dict] = {}
LOCK = threading.Lock()
ALLOWED = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp"}


def _job_status_path(job_id: str) -> Path:
    return OUTPUT / job_id / "status.json"


def _write_job(job_id: str, data: dict) -> None:
    folder = OUTPUT / job_id
    folder.mkdir(parents=True, exist_ok=True)
    _job_status_path(job_id).write_text(json.dumps(data), encoding="utf-8")


def _read_job(job_id: str) -> dict | None:
    path = _job_status_path(job_id)
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _update(job_id: str, **fields) -> None:
    with LOCK:
        if job_id not in JOBS:
            JOBS[job_id] = _read_job(job_id) or {
                "status": "working",
                "message": "Starting…",
                "error": None,
                "attributions": [],
            }
        JOBS[job_id].update(fields)
        snapshot = dict(JOBS[job_id])
    try:
        _write_job(job_id, snapshot)
    except OSError:
        pass


def _start_job(*args, **kwargs) -> None:
    """Run video build in a background thread locally; on Vercel run inline (no workers)."""
    if ON_VERCEL:
        _run(*args, **kwargs)
        return
    threading.Thread(target=_run, args=args, kwargs=kwargs, daemon=True).start()


def _public_draft(draft: dict) -> dict:
    return {
        "id": draft["id"],
        "title": draft["title"],
        "text": draft["text"],
        "voice": draft["voice"],
        "providers": draft["plan"]["providers"],
        "notes": draft["plan"]["notes"],
        "scenes": draft["plan"]["scenes"],
    }


def _prepare_stock(job_id: str, text: str, on_progress) -> tuple[list[dict], list]:
    """Search stock, auto-pick the best asset per scene, download into the job folder."""
    folder = OUTPUT / job_id
    on_progress("Finding the best stock photos for each line…")
    plan = build_stock_plan(text)
    scene_media: list[dict] = []
    attribution_assets = []
    for scene in plan["scenes"]:
        selected = scene.get("selected")
        if not selected:
            scene_media.append({})
            continue
        asset = find_asset(plan, selected["id"])
        if asset is None:
            scene_media.append({})
            continue
        on_progress(f"Downloading shot {scene['index'] + 1}…")
        suffix = ".mp4" if asset.media_type == "video" else ".jpg"
        dest = folder / f"stock_{scene['index']}_{asset.provider}_{asset.media_type}{suffix}"
        dest = download_asset(asset, dest)
        (dest.with_suffix(dest.suffix + ".json")).write_text(
            json.dumps(asset.public_dict(), indent=2),
            encoding="utf-8",
        )
        attribution_assets.append(asset)
        scene_media.append(
            {
                "path": str(dest),
                "media_type": asset.media_type,
                "attribution": asset.attribution,
                "asset_id": asset.id,
                "scene_text": scene["text"],
                "page_url": asset.page_url,
                "creator": asset.creator,
                "license": asset.license,
            }
        )
    with LOCK:
        JOBS[job_id]["plan"] = plan
    return scene_media, attribution_assets


def _run(
    job_id: str,
    text: str,
    title: str,
    voice: bool,
    images: list[Path],
    scene_media: list[dict] | None,
    attribution_assets: list | None,
    auto_stock: bool = False,
    captions: bool = True,
) -> None:
    folder = OUTPUT / job_id
    dest = folder / "film.mp4"

    def on_progress(message: str) -> None:
        _update(job_id, status="working", message=message)

    try:
        media = list(scene_media or [])
        assets = list(attribution_assets or [])
        if auto_stock and any(configured_providers().values()) and not images:
            media, assets = _prepare_stock(job_id, text, on_progress)

        if assets:
            write_attribution_record(
                folder / "attributions.json",
                assets,
                [
                    {
                        "scene_index": index,
                        "text": media[index].get("scene_text") if index < len(media) else "",
                        "asset_id": media[index].get("asset_id") if index < len(media) else None,
                        "attribution": media[index].get("attribution") if index < len(media) else None,
                    }
                    for index in range(len(media))
                ],
            )
        info = make_video(
            text=text,
            title=title,
            image_paths=images,
            scene_media=media,
            voice=voice,
            captions=captions,
            dest_mp4=dest,
            on_progress=on_progress,
        )
        _update(
            job_id,
            status="done",
            message="Your video is ready.",
            attributions=info.get("attributions") or [],
        )
    except Exception as error:
        _update(job_id, status="error", message="The video didn't finish.", error=str(error))


@app.get("/")
def home():
    return render_template("index.html", providers=configured_providers())


@app.get("/google5695cb5f1210e8c6.html")
def google_site_verification():
    """Google Search Console HTML-file ownership verification."""
    path = ROOT / "google5695cb5f1210e8c6.html"
    return send_file(path, mimetype="text/html")


@app.get("/ads.txt")
def ads_txt():
    """IAB ads.txt — authorizes Google AdSense to sell inventory on this site."""
    path = ROOT / "ads.txt"
    return send_file(path, mimetype="text/plain")


@app.get("/api/providers")
def providers():
    return jsonify(configured_providers())


@app.post("/api/stock/search")
def stock_search():
    payload = request.get_json(silent=True) or {}
    text = (payload.get("text") or "").strip()
    if not text:
        return jsonify(error="Write what the video should say."), 400
    if len(text) > 4000:
        return jsonify(error="Keep the text under 4000 characters."), 400
    title = (payload.get("title") or "").strip()[:80]
    voice = bool(payload.get("voice", True))
    captions = bool(payload.get("captions", True))

    if not any(configured_providers().values()):
        return jsonify(
            error="Add PIXABAY_API_KEY to the .env file (Pixabay alone is enough; new Pexels keys are paused), then restart the app."
        ), 400

    try:
        plan = build_stock_plan(text)
    except RuntimeError as error:
        return jsonify(error=str(error)), 502

    draft_id = uuid.uuid4().hex[:12]
    draft = {
        "id": draft_id,
        "title": title,
        "text": text,
        "voice": voice,
        "captions": captions,
        "plan": plan,
    }
    with LOCK:
        DRAFTS[draft_id] = draft
    return jsonify(_public_draft(draft))


@app.get("/api/stock/<draft_id>")
def stock_get(draft_id: str):
    if not re.fullmatch(r"[a-f0-9]{12}", draft_id):
        return jsonify(error="Unknown draft."), 404
    with LOCK:
        draft = DRAFTS.get(draft_id)
    if draft is None:
        return jsonify(error="Unknown draft."), 404
    return jsonify(_public_draft(draft))


@app.post("/api/stock/<draft_id>/select")
def stock_select(draft_id: str):
    if not re.fullmatch(r"[a-f0-9]{12}", draft_id):
        return jsonify(error="Unknown draft."), 404
    payload = request.get_json(silent=True) or {}
    try:
        scene_index = int(payload.get("scene_index"))
    except (TypeError, ValueError):
        return jsonify(error="Pick a scene."), 400
    asset_id = (payload.get("asset_id") or "").strip()
    if not asset_id:
        return jsonify(error="Pick a stock asset."), 400

    with LOCK:
        draft = DRAFTS.get(draft_id)
        if draft is None:
            return jsonify(error="Unknown draft."), 404
        scenes = draft["plan"]["scenes"]
        if scene_index < 0 or scene_index >= len(scenes):
            return jsonify(error="That scene does not exist."), 400
        asset = find_asset(draft["plan"], asset_id)
        if asset is None:
            return jsonify(error="That asset is not in this draft."), 404
        scenes[scene_index]["selected_id"] = asset.id
        scenes[scene_index]["selected"] = asset.public_dict()
        result = _public_draft(draft)
    return jsonify(result)


@app.post("/api/generate")
def generate():
    # JSON path: one-click auto video, or continue from an optional draft.
    if request.is_json:
        payload = request.get_json(silent=True) or {}
        draft_id = (payload.get("draft_id") or "").strip()
        if draft_id:
            return _generate_from_draft(draft_id, payload)

        text = (payload.get("text") or "").strip()
        if not text:
            return jsonify(error="Write what the video should say."), 400
        if len(text) > 4000:
            return jsonify(error="Keep the text under 4000 characters."), 400
        title = (payload.get("title") or "").strip()[:80]
        voice = bool(payload.get("voice", True))
        captions = bool(payload.get("captions", True))
        use_stock = bool(payload.get("use_stock", True))
        job_id = uuid.uuid4().hex[:12]
        (OUTPUT / job_id).mkdir(parents=True, exist_ok=True)
        initial = {
            "status": "working",
            "message": "Starting…",
            "error": None,
            "attributions": [],
        }
        with LOCK:
            JOBS[job_id] = dict(initial)
        _write_job(job_id, initial)
        _start_job(
            job_id,
            text,
            title,
            voice,
            [],
            [],
            [],
            auto_stock=use_stock and any(configured_providers().values()),
            captions=captions,
        )
        job = _read_job(job_id) or JOBS.get(job_id) or initial
        return jsonify(id=job_id, status=job.get("status"), message=job.get("message"), error=job.get("error"))

    text = (request.form.get("text") or "").strip()
    if not text:
        return jsonify(error="Write what the video should say."), 400
    if len(text) > 4000:
        return jsonify(error="Keep the text under 4000 characters."), 400

    title = (request.form.get("title") or "").strip()[:80]
    voice = request.form.get("voice") == "on"
    captions = request.form.get("captions") == "on"
    use_stock = request.form.get("use_stock", "on") == "on"
    job_id = uuid.uuid4().hex[:12]
    folder = OUTPUT / job_id
    folder.mkdir(parents=True, exist_ok=True)

    images: list[Path] = []
    for upload in request.files.getlist("images")[:8]:
        if not upload or not upload.filename:
            continue
        suffix = Path(secure_filename(upload.filename)).suffix.lower()
        if suffix not in ALLOWED:
            continue
        dest = folder / f"img_{len(images)}{suffix}"
        upload.save(dest)
        images.append(dest)

    initial = {
        "status": "working",
        "message": "Starting…",
        "error": None,
        "attributions": [],
    }
    with LOCK:
        JOBS[job_id] = dict(initial)
    _write_job(job_id, initial)

    _start_job(
        job_id,
        text,
        title,
        voice,
        images,
        [],
        [],
        auto_stock=use_stock and not images and any(configured_providers().values()),
        captions=captions,
    )
    job = _read_job(job_id) or JOBS.get(job_id) or initial
    return jsonify(id=job_id, status=job.get("status"), message=job.get("message"), error=job.get("error"))


def _generate_from_draft(draft_id: str, payload: dict):
    if not re.fullmatch(r"[a-f0-9]{12}", draft_id):
        return jsonify(error="Unknown draft."), 404
    with LOCK:
        draft = DRAFTS.get(draft_id)
    if draft is None:
        return jsonify(error="Unknown draft."), 404

    overrides = payload.get("selections") or {}
    voice = bool(payload.get("voice", draft["voice"]))
    captions = bool(payload.get("captions", draft.get("captions", True)))
    title = (payload.get("title") or draft["title"] or "").strip()[:80]
    text = draft["text"]
    job_id = uuid.uuid4().hex[:12]
    folder = OUTPUT / job_id
    folder.mkdir(parents=True, exist_ok=True)

    scene_media: list[dict] = []
    attribution_assets = []
    for scene in draft["plan"]["scenes"]:
        asset_id = overrides.get(str(scene["index"])) or scene.get("selected_id")
        if not asset_id:
            scene_media.append({})
            continue
        asset = find_asset(draft["plan"], asset_id)
        if asset is None:
            scene_media.append({})
            continue
        suffix = ".mp4" if asset.media_type == "video" else ".jpg"
        dest = folder / f"stock_{scene['index']}_{asset.provider}_{asset.media_type}{suffix}"
        try:
            dest = download_asset(asset, dest)
        except Exception as error:
            return jsonify(error=f"Could not download stock media: {error}"), 502
        meta_path = dest.with_suffix(dest.suffix + ".json")
        meta_path.write_text(json.dumps(asset.public_dict(), indent=2), encoding="utf-8")
        attribution_assets.append(asset)
        scene_media.append(
            {
                "path": str(dest),
                "media_type": asset.media_type,
                "attribution": asset.attribution,
                "asset_id": asset.id,
                "scene_text": scene["text"],
                "page_url": asset.page_url,
                "creator": asset.creator,
                "license": asset.license,
            }
        )

    initial = {
        "status": "working",
        "message": "Downloading stock media into the timeline…",
        "error": None,
        "attributions": [],
    }
    with LOCK:
        JOBS[job_id] = dict(initial)
    _write_job(job_id, initial)

    _start_job(
        job_id,
        text,
        title,
        voice,
        [],
        scene_media,
        attribution_assets,
        captions=captions,
    )
    job = _read_job(job_id) or JOBS.get(job_id) or initial
    return jsonify(
        id=job_id,
        draft_id=draft_id,
        status=job.get("status"),
        message=job.get("message"),
        error=job.get("error"),
    )


@app.get("/api/jobs/<job_id>")
def job_status(job_id: str):
    if not re.fullmatch(r"[a-f0-9]{12}", job_id):
        return jsonify(error="Unknown video."), 404
    with LOCK:
        job = JOBS.get(job_id)
    if job is None:
        job = _read_job(job_id)
    if job is None:
        film = OUTPUT / job_id / "film.mp4"
        if film.exists():
            return jsonify(status="done", message="Your video is ready.", error=None, attributions=[])
        return jsonify(error="Unknown video."), 404
    return jsonify(
        status=job["status"],
        message=job["message"],
        error=job["error"],
        attributions=job.get("attributions") or [],
    )


@app.get("/videos/<job_id>")
def video(job_id: str):
    if not re.fullmatch(r"[a-f0-9]{12}", job_id):
        return jsonify(error="Unknown video."), 404
    film = OUTPUT / job_id / "film.mp4"
    if not film.exists():
        return jsonify(error="That video isn't ready yet."), 404
    return send_file(film, mimetype="video/mp4", as_attachment=False, download_name="video.mp4")


@app.get("/videos/<job_id>/attributions")
def attributions(job_id: str):
    if not re.fullmatch(r"[a-f0-9]{12}", job_id):
        return jsonify(error="Unknown video."), 404
    record = OUTPUT / job_id / "attributions.json"
    if not record.exists():
        return jsonify(error="No attribution record for that video."), 404
    return send_file(record, mimetype="application/json", as_attachment=False)


def main() -> None:
    port = 7860
    url = f"http://127.0.0.1:{port}"
    print(f"AutoVideo Studio is running at {url}")
    providers = configured_providers()
    print(
        "Stock providers:",
        ", ".join(name for name, ok in providers.items() if ok) or "none configured (.env)",
    )
    threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    app.run(host="127.0.0.1", port=port, threaded=True, debug=False)


if __name__ == "__main__":
    main()
