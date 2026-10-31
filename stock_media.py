"""Search, score, download and attribute stock photos/videos from Pexels and Pixabay."""

from __future__ import annotations

import hashlib
import json
import os
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from keywords import scene_plan

PEXELS_PHOTO_URL = "https://api.pexels.com/v1/search"
PEXELS_VIDEO_URL = "https://api.pexels.com/v1/videos/search"
PIXABAY_PHOTO_URL = "https://pixabay.com/api/"
PIXABAY_VIDEO_URL = "https://pixabay.com/api/videos/"

CACHE_TTL = 24 * 60 * 60
MAX_CANDIDATES = 4
USER_AGENT = "AutoVideoStudio/1.0 (local; stock media for video editing)"

_CACHE: dict[str, tuple[float, list["StockAsset"]]] = {}
_CACHE_LOCK = threading.Lock()
_RATE_LOCK = threading.Lock()
_LAST_CALL: dict[str, float] = {"pexels": 0.0, "pixabay": 0.0}


@dataclass
class StockAsset:
    id: str
    provider: str
    media_type: str
    title: str
    preview_url: str
    download_url: str
    page_url: str
    creator: str
    creator_url: str
    license: str
    attribution: str
    width: int = 0
    height: int = 0
    duration: float | None = None
    tags: list[str] = field(default_factory=list)
    score: float = 0.0
    query: str = ""

    def public_dict(self) -> dict[str, Any]:
        data = asdict(self)
        # Never send download URLs that embed keys; these are CDN URLs only.
        return data


def load_env_file(path: Path) -> None:
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        # .env is the source of truth for local secrets.
        if key:
            os.environ[key] = value


def configured_providers() -> dict[str, bool]:
    return {
        "pexels": bool(os.environ.get("PEXELS_API_KEY", "").strip()),
        "pixabay": bool(os.environ.get("PIXABAY_API_KEY", "").strip()),
    }


def _throttle(provider: str, minimum_gap: float = 0.35) -> None:
    with _RATE_LOCK:
        now = time.monotonic()
        wait = minimum_gap - (now - _LAST_CALL.get(provider, 0.0))
        if wait > 0:
            time.sleep(wait)
        _LAST_CALL[provider] = time.monotonic()


def _http_json(url: str, headers: dict[str, str] | None = None, timeout: int = 30) -> dict:
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "application/json",
            **(headers or {}),
        },
        method="GET",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = response.read()
    except urllib.error.HTTPError as error:
        detail = error.read().decode("utf-8", errors="replace")[:300]
        raise RuntimeError(f"Stock API error {error.code}: {detail or error.reason}") from error
    except urllib.error.URLError as error:
        raise RuntimeError(f"Stock API unreachable: {error.reason}") from error
    return json.loads(payload.decode("utf-8"))


def _cache_get(key: str) -> list[StockAsset] | None:
    with _CACHE_LOCK:
        hit = _CACHE.get(key)
        if not hit:
            return None
        stamp, assets = hit
        if time.time() - stamp > CACHE_TTL:
            _CACHE.pop(key, None)
            return None
        return [StockAsset(**asdict(asset)) for asset in assets]


def _cache_set(key: str, assets: list[StockAsset]) -> None:
    with _CACHE_LOCK:
        _CACHE[key] = (time.time(), [StockAsset(**asdict(asset)) for asset in assets])


def _asset_id(provider: str, media_type: str, raw_id: str | int) -> str:
    return f"{provider}:{media_type}:{raw_id}"


def _pexels_photo(item: dict, query: str) -> StockAsset:
    photographer = item.get("photographer") or "Unknown"
    page = item.get("url") or "https://www.pexels.com"
    photographer_url = item.get("photographer_url") or page
    src = item.get("src") or {}
    download = src.get("large2x") or src.get("large") or src.get("original") or ""
    preview = src.get("medium") or src.get("small") or download
    return StockAsset(
        id=_asset_id("pexels", "photo", item["id"]),
        provider="pexels",
        media_type="photo",
        title=f"Photo by {photographer}",
        preview_url=preview,
        download_url=download,
        page_url=page,
        creator=photographer,
        creator_url=photographer_url,
        license="Pexels License (free for commercial use; attribution required for API use)",
        attribution=f"Photo by {photographer} on Pexels",
        width=int(item.get("width") or 0),
        height=int(item.get("height") or 0),
        tags=[query],
        query=query,
    )


def _best_pexels_video_file(item: dict) -> tuple[str, int, int, str]:
    files = sorted(
        item.get("video_files") or [],
        key=lambda entry: abs(int(entry.get("width") or 0) - 1280),
    )
    chosen = None
    for entry in files:
        width = int(entry.get("width") or 0)
        link = entry.get("link") or ""
        if width >= 640 and link:
            chosen = entry
            break
    if chosen is None and files:
        chosen = files[0]
    if not chosen:
        return "", 0, 0, ""
    pictures = item.get("video_pictures") or []
    preview = (pictures[0].get("picture") if pictures else "") or ""
    return (
        chosen.get("link") or "",
        int(chosen.get("width") or 0),
        int(chosen.get("height") or 0),
        preview,
    )


def _pexels_video(item: dict, query: str) -> StockAsset | None:
    download, width, height, preview = _best_pexels_video_file(item)
    if not download:
        return None
    user = item.get("user") or {}
    creator = user.get("name") or "Unknown"
    page = item.get("url") or "https://www.pexels.com"
    creator_url = user.get("url") or page
    image = item.get("image") or preview
    return StockAsset(
        id=_asset_id("pexels", "video", item["id"]),
        provider="pexels",
        media_type="video",
        title=f"Video by {creator}",
        preview_url=image or preview,
        download_url=download,
        page_url=page,
        creator=creator,
        creator_url=creator_url,
        license="Pexels License (free for commercial use; attribution required for API use)",
        attribution=f"Video by {creator} on Pexels",
        width=width,
        height=height,
        duration=float(item.get("duration") or 0) or None,
        tags=[query],
        query=query,
    )


def _pixabay_photo(item: dict, query: str) -> StockAsset:
    user = item.get("user") or "Unknown"
    user_id = item.get("user_id")
    page = item.get("pageURL") or "https://pixabay.com"
    creator_url = f"https://pixabay.com/users/{urllib.parse.quote(str(user))}-{user_id}/" if user_id else page
    download = item.get("largeImageURL") or item.get("webformatURL") or ""
    preview = item.get("webformatURL") or item.get("previewURL") or download
    tags = [tag.strip() for tag in str(item.get("tags") or "").split(",") if tag.strip()]
    return StockAsset(
        id=_asset_id("pixabay", "photo", item["id"]),
        provider="pixabay",
        media_type="photo",
        title=f"Photo by {user}",
        preview_url=preview,
        download_url=download,
        page_url=page,
        creator=user,
        creator_url=creator_url,
        license="Pixabay Content License (free for commercial use; show source in search UI)",
        attribution=f"Image by {user} via Pixabay",
        width=int(item.get("imageWidth") or item.get("webformatWidth") or 0),
        height=int(item.get("imageHeight") or item.get("webformatHeight") or 0),
        tags=tags or [query],
        query=query,
    )


def _pixabay_video(item: dict, query: str) -> StockAsset | None:
    videos = item.get("videos") or {}
    chosen = videos.get("medium") or videos.get("small") or videos.get("large") or videos.get("tiny")
    if not chosen or not chosen.get("url"):
        return None
    user = item.get("user") or "Unknown"
    user_id = item.get("user_id")
    page = item.get("pageURL") or "https://pixabay.com"
    creator_url = f"https://pixabay.com/users/{urllib.parse.quote(str(user))}-{user_id}/" if user_id else page
    preview = chosen.get("thumbnail") or ""
    tags = [tag.strip() for tag in str(item.get("tags") or "").split(",") if tag.strip()]
    return StockAsset(
        id=_asset_id("pixabay", "video", item["id"]),
        provider="pixabay",
        media_type="video",
        title=f"Video by {user}",
        preview_url=preview,
        download_url=chosen["url"],
        page_url=page,
        creator=user,
        creator_url=creator_url,
        license="Pixabay Content License (free for commercial use; show source in search UI)",
        attribution=f"Video by {user} via Pixabay",
        width=int(chosen.get("width") or 0),
        height=int(chosen.get("height") or 0),
        duration=float(item.get("duration") or 0) or None,
        tags=tags or [query],
        query=query,
    )


def search_pexels(query: str, *, photos: int = 5, videos: int = 0) -> list[StockAsset]:
    key = os.environ.get("PEXELS_API_KEY", "").strip()
    if not key:
        return []
    cache_key = f"pexels|{query}|{photos}|{videos}"
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached

    headers = {"Authorization": key}
    assets: list[StockAsset] = []

    if photos:
        _throttle("pexels")
        params = urllib.parse.urlencode(
            {"query": query, "orientation": "landscape", "per_page": photos, "page": 1}
        )
        data = _http_json(f"{PEXELS_PHOTO_URL}?{params}", headers=headers)
        for item in data.get("photos") or []:
            assets.append(_pexels_photo(item, query))

    if videos:
        _throttle("pexels")
        params = urllib.parse.urlencode(
            {"query": query, "orientation": "landscape", "per_page": videos, "page": 1}
        )
        data = _http_json(f"{PEXELS_VIDEO_URL}?{params}", headers=headers)
        for item in data.get("videos") or []:
            asset = _pexels_video(item, query)
            if asset:
                assets.append(asset)

    _cache_set(cache_key, assets)
    return assets


def search_pixabay(query: str, *, photos: int = 5, videos: int = 0) -> list[StockAsset]:
    key = os.environ.get("PIXABAY_API_KEY", "").strip()
    if not key:
        return []
    cache_key = f"pixabay|{query}|{photos}|{videos}"
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached

    assets: list[StockAsset] = []
    if photos:
        _throttle("pixabay")
        params = urllib.parse.urlencode(
            {
                "key": key,
                "q": query[:100],
                "image_type": "photo",
                "orientation": "horizontal",
                "safesearch": "true",
                "per_page": max(3, photos),
                "page": 1,
            }
        )
        data = _http_json(f"{PIXABAY_PHOTO_URL}?{params}")
        for item in data.get("hits") or []:
            assets.append(_pixabay_photo(item, query))

    if videos:
        _throttle("pixabay")
        params = urllib.parse.urlencode(
            {
                "key": key,
                "q": query[:100],
                "video_type": "film",
                "safesearch": "true",
                "per_page": max(3, videos),
                "page": 1,
            }
        )
        data = _http_json(f"{PIXABAY_VIDEO_URL}?{params}")
        for item in data.get("hits") or []:
            asset = _pixabay_video(item, query)
            if asset:
                assets.append(asset)

    _cache_set(cache_key, assets)
    return assets


def _score_asset(asset: StockAsset, keywords: list[str], scene_text: str) -> float:
    haystack = " ".join(
        [
            asset.query,
            asset.title,
            asset.creator,
            " ".join(asset.tags),
            scene_text,
        ]
    ).lower()
    score = 0.0
    for index, keyword in enumerate(keywords):
        weight = 1.0 / (index + 1)
        for token in re.findall(r"[a-z0-9]+", keyword.lower()):
            if token in haystack:
                score += 2.2 * weight
    # Prefer photos: they download and encode much faster than stock video.
    if asset.media_type == "photo":
        score += 1.8
    elif asset.media_type == "video":
        score += 0.4
    if asset.width >= 1280 and asset.height >= 720:
        score += 0.8
    if asset.provider == "pexels":
        score += 0.15
    # Slight preference for landscape.
    if asset.width and asset.height and asset.width >= asset.height:
        score += 0.4
    asset.score = round(score, 3)
    return asset.score


def _dedupe(assets: list[StockAsset]) -> list[StockAsset]:
    seen: set[str] = set()
    unique: list[StockAsset] = []
    for asset in assets:
        fingerprint = hashlib.sha1(
            f"{asset.provider}|{asset.media_type}|{asset.page_url}|{asset.download_url}".encode()
        ).hexdigest()
        if fingerprint in seen:
            continue
        seen.add(fingerprint)
        unique.append(asset)
    return unique


def search_for_scene(scene_text: str, keywords: list[str], query: str) -> list[StockAsset]:
    providers = configured_providers()
    if not any(providers.values()):
        raise RuntimeError(
            "Add PIXABAY_API_KEY to a .env file in the project folder. "
            "Pixabay alone is enough; new Pexels API keys are currently paused."
        )

    collected: list[StockAsset] = []
    errors: list[str] = []
    # One strong query first keeps API calls low and the pipeline fast.
    for attempt_query in [query, *keywords[1:2]]:
        if providers["pexels"]:
            try:
                collected.extend(search_pexels(attempt_query, photos=4, videos=0))
            except RuntimeError as error:
                errors.append(str(error))
        if providers["pixabay"]:
            try:
                collected.extend(search_pixabay(attempt_query, photos=4, videos=0))
            except RuntimeError as error:
                errors.append(str(error))
        if len(collected) >= MAX_CANDIDATES:
            break

    assets = _dedupe(collected)
    for asset in assets:
        _score_asset(asset, keywords, scene_text)
    assets.sort(key=lambda item: item.score, reverse=True)
    if not assets and errors:
        raise RuntimeError(errors[0])
    return assets[:MAX_CANDIDATES]


def build_stock_plan(text: str) -> dict[str, Any]:
    scenes = scene_plan(text)
    results = []
    for scene in scenes:
        candidates = search_for_scene(scene["text"], scene["keywords"], scene["query"])
        selected = candidates[0] if candidates else None
        results.append(
            {
                "index": scene["index"],
                "text": scene["text"],
                "keywords": scene["keywords"],
                "query": scene["query"],
                "selected_id": selected.id if selected else None,
                "selected": selected.public_dict() if selected else None,
                "candidates": [asset.public_dict() for asset in candidates],
            }
        )
    return {
        "providers": configured_providers(),
        "scenes": results,
        "notes": [
            "Pixabay alone is enough. New Pexels API keys are currently paused.",
            "Pixabay and Pexels licenses allow commercial use when terms are followed.",
            "API attribution is shown for every asset. Do not scrape Google Images.",
            "Pixabay search responses are cached for 24 hours per their API terms.",
        ],
    }


def find_asset(plan: dict[str, Any], asset_id: str) -> StockAsset | None:
    for scene in plan.get("scenes") or []:
        for raw in scene.get("candidates") or []:
            if raw.get("id") == asset_id:
                return StockAsset(**raw)
        selected = scene.get("selected")
        if selected and selected.get("id") == asset_id:
            return StockAsset(**selected)
    return None


def download_asset(asset: StockAsset, dest: Path) -> Path:
    dest.parent.mkdir(parents=True, exist_ok=True)
    request = urllib.request.Request(
        asset.download_url,
        headers={"User-Agent": USER_AGENT, "Accept": "*/*"},
        method="GET",
    )
    with urllib.request.urlopen(request, timeout=90) as response:
        data = response.read()
        content_type = (response.headers.get("Content-Type") or "").lower()
    suffix = dest.suffix
    if not suffix:
        if "video" in content_type or asset.media_type == "video":
            suffix = ".mp4"
        elif "png" in content_type:
            suffix = ".png"
        elif "webp" in content_type:
            suffix = ".webp"
        else:
            suffix = ".jpg"
        dest = dest.with_suffix(suffix)
    dest.write_bytes(data)
    if dest.stat().st_size < 256:
        dest.unlink(missing_ok=True)
        raise RuntimeError(f"Downloaded file for {asset.id} was empty.")
    return dest


def write_attribution_record(path: Path, assets: list[StockAsset], scenes: list[dict]) -> None:
    record = {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "policy": {
            "commercial_use": True,
            "providers": ["pexels", "pixabay"],
            "notes": [
                "Only Pexels and Pixabay API assets are used.",
                "Google Images and other scraped sources are forbidden.",
                "Modifying a copyrighted image does not make it legal to use.",
            ],
        },
        "assets": [
            {
                "id": asset.id,
                "provider": asset.provider,
                "media_type": asset.media_type,
                "creator": asset.creator,
                "creator_url": asset.creator_url,
                "page_url": asset.page_url,
                "license": asset.license,
                "attribution": asset.attribution,
                "download_url": asset.download_url,
                "query": asset.query,
            }
            for asset in assets
        ],
        "timeline": scenes,
    }
    path.write_text(json.dumps(record, indent=2), encoding="utf-8")
