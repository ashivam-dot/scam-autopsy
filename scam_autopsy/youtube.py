"""Identity-pinned YouTube operations. No browser session or local credential is required."""
from __future__ import annotations

import json
import os
import time
from datetime import date, timedelta
from pathlib import Path
from urllib.parse import urlparse

from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

CHANNEL_ID = "UCz0W-lSVvEeWYufkUVaVUKA"
SOURCE_HOSTS = {"www.ic3.gov", "ic3.gov", "www.fbi.gov", "fbi.gov", "consumer.ftc.gov", "www.ftc.gov", "ftc.gov", "www.cisa.gov", "cisa.gov"}


def client(service="youtube", version="v3"):
    raw = os.environ.get("YOUTUBE_OAUTH_JSON")
    if not raw:
        raise RuntimeError("YOUTUBE_OAUTH_JSON secret is missing")
    info = json.loads(raw)
    creds = Credentials.from_authorized_user_info(info)
    if not creds.valid:
        creds.refresh(Request())
    return build(service, version, credentials=creds, cache_discovery=False)


def identity(api):
    items = api.channels().list(part="snippet,statistics,contentDetails", mine=True).execute().get("items", [])
    if len(items) != 1 or items[0]["id"] != CHANNEL_ID:
        raise RuntimeError("Channel mismatch: refusing all writes")
    return items[0]


def validate(case):
    if case.get("approved") is not True or not case.get("review", {}).get("reviewer"):
        raise ValueError("Only reviewed case files can publish")
    if not case.get("source_urls"):
        raise ValueError("Primary sources are required")
    for url in case["source_urls"]:
        u = urlparse(url)
        if u.scheme != "https" or u.hostname not in SOURCE_HOSTS or u.username or u.password or u.port not in (None, 443):
            raise ValueError("Unsupported primary-source URL")
    if not case.get("id") or len(case.get("title", "")) > 100:
        raise ValueError("Invalid identity or title")
    if case.get("format") == "short" and len(case.get("scenes", [])) != 7:
        raise ValueError("Shorts require seven reviewed scenes")
    for scene in case.get("scenes", []):
        if not scene.get("narration") or not scene.get("label"):
            raise ValueError("Every scene needs narration and a disclosure label")
    return case


def marker(case):
    return "scam-autopsy:" + case["id"]


def recent(api, channel):
    playlist = channel["contentDetails"]["relatedPlaylists"]["uploads"]
    rows = api.playlistItems().list(part="contentDetails", playlistId=playlist, maxResults=50).execute().get("items", [])
    ids = [r["contentDetails"]["videoId"] for r in rows]
    if not ids:
        return []
    return api.videos().list(part="snippet,status,statistics,contentDetails,processingDetails", id=",".join(ids)).execute().get("items", [])


def find_case(api, channel, case):
    found = [v for v in recent(api, channel) if marker(case) in v["snippet"].get("tags", [])]
    if len(found) > 1:
        raise RuntimeError("Duplicate case marker: investigate before publishing")
    return found[0] if found else None


def description(case):
    text = case["description"].strip()
    text += "\n\nPrimary sources:\n" + "\n".join(case["source_urls"])
    text += "\n\nOriginal illustrated explanation with synthetic narration. Simulated messages are illustrations; no real victim is depicted.\nNever post passwords, verification codes, or payment details in comments.\n\nCase file: " + case["id"]
    if case.get("format") == "short":
        text += "\n#ScamAwareness #ScamAutopsy"
    if len(text) > 5000:
        raise ValueError("Description exceeds YouTube limit")
    return text


def upload_private(api, case, media: Path):
    validate(case)
    identity(api)
    body = {
        "snippet": {"title": case["title"], "description": description(case), "tags": ["Scam Autopsy", "scam awareness", marker(case)], "categoryId": "27", "defaultLanguage": "en", "defaultAudioLanguage": "en"},
        "status": {"privacyStatus": "private", "selfDeclaredMadeForKids": False, "containsSyntheticMedia": True},
    }
    request = api.videos().insert(part="snippet,status", body=body, notifySubscribers=False, media_body=MediaFileUpload(str(media), mimetype="video/mp4", chunksize=4 * 1024 * 1024, resumable=True))
    response = None
    while response is None:
        _, response = request.next_chunk(num_retries=3)
    return response["id"]


def finalize(api, case, video_id, publish=True, max_wait=420):
    identity(api)
    deadline = time.monotonic() + max_wait
    while True:
        videos = api.videos().list(part="snippet,status,processingDetails,contentDetails", id=video_id).execute().get("items", [])
        if len(videos) != 1:
            raise RuntimeError("Uploaded video cannot be verified")
        video = videos[0]
        if video["snippet"]["channelId"] != CHANNEL_ID or marker(case) not in video["snippet"].get("tags", []):
            raise RuntimeError("Video identity/marker mismatch")
        processing = video.get("processingDetails", {}).get("processingStatus")
        upload_status = video["status"].get("uploadStatus")
        if processing in ("failed", "terminated") or upload_status in ("failed", "rejected", "deleted"):
            raise RuntimeError("YouTube processing rejected the video")
        if processing == "succeeded" or upload_status == "processed":
            break
        if time.monotonic() >= deadline:
            raise RuntimeError("Video still processing; next cloud run will reconcile")
        time.sleep(10)
    if publish and video["status"]["privacyStatus"] != "public":
        status = {"privacyStatus": "public", "selfDeclaredMadeForKids": False, "containsSyntheticMedia": True}
        api.videos().update(part="status", body={"id": video_id, "status": status}).execute()
    verified = api.videos().list(part="status,snippet,contentDetails", id=video_id).execute()["items"][0]
    expected = "public" if publish else "private"
    if verified["status"]["privacyStatus"] != expected:
        raise RuntimeError("Privacy verification failed")
    return {"video_id": video_id, "url": "https://www.youtube.com/watch?v=" + video_id, "channel_id": CHANNEL_ID, "privacy": expected, "processing": "succeeded", "duration": verified["contentDetails"]["duration"], "title": verified["snippet"]["title"]}


def snapshot(days=28):
    api = client()
    channel = identity(api)
    rows = recent(api, channel)
    end = date.today() - timedelta(days=2)  # Analytics reporting lags real-time views.
    start = end - timedelta(days=days - 1)
    analytics = client("youtubeAnalytics", "v2")
    result = analytics.reports().query(ids="channel==MINE", startDate=start.isoformat(), endDate=end.isoformat(), metrics="views,engagedViews,averageViewDuration,averageViewPercentage,likes,comments,shares,subscribersGained,subscribersLost", dimensions="video", sort="-views", maxResults=200).execute()
    headers = [h["name"] for h in result.get("columnHeaders", [])]
    metrics = [dict(zip(headers, r)) for r in result.get("rows", [])]
    return {"as_of": date.today().isoformat(), "window": {"start": start.isoformat(), "end": end.isoformat()}, "channel": {"id": channel["id"], "title": channel["snippet"]["title"], **channel.get("statistics", {})}, "videos": [{"id": v["id"], "title": v["snippet"]["title"], "published_at": v["snippet"]["publishedAt"], "privacy": v["status"]["privacyStatus"], "duration": v["contentDetails"]["duration"], "statistics": v.get("statistics", {})} for v in rows], "analytics": metrics, "limits": ["Analytics lags; small samples do not establish winners.", "Engaged views divided by views is not Studio's stayed-to-watch rate."]}
