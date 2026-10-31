# On-demand video uploader — run by GitHub Actions only when Cloudflare Worker dispatches it.
# Verified build: multi-account (numbered env vars), run_id job claiming (no duplicate pickup),
# Meta Graph version via env, YouTube privacy status logged (unverified-lock visibility),
# TikTok inbox mode default, per-platform usage reporting back to D1 for daily quota accounting.
# Secrets come ONLY from GitHub Secrets (environment variables) — never from code or chat.

import os
import re
import math
import time
import requests
import gdown
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

WORKER_URL = os.getenv("WORKER_URL", "").rstrip("/")
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "")
RUN_ID = os.getenv("RUN_ID", "")
GRAPH_VERSION = os.getenv("GRAPH_VERSION", "v23.0")
YOUTUBE_PRIVACY = os.getenv("YOUTUBE_PRIVACY", "public")
TIKTOK_MODE = os.getenv("TIKTOK_MODE", "INBOX").upper()
TIKTOK_PRIVACY = os.getenv("TIKTOK_PRIVACY", "SELF_ONLY")
TMP_DIR = "/tmp/videos"
os.makedirs(TMP_DIR, exist_ok=True)


def load_accounts(kind, fields):
    """Load numbered accounts KIND_1..KIND_10 from env. fields maps short name -> env suffix."""
    accounts = {}
    for i in range(1, 11):
        acct = {}
        missing = False
        for short, suffix in fields.items():
            val = os.getenv(f"{kind}_{i}_{suffix}", "")
            if not val:
                missing = True
            acct[short] = val
        if not missing:
            accounts[str(i)] = acct
    return accounts


YT_ACCOUNTS = load_accounts("YT", {"cid": "CLIENT_ID", "csecret": "CLIENT_SECRET", "rtoken": "REFRESH_TOKEN"})
FB_ACCOUNTS = load_accounts("FB", {"page_id": "PAGE_ID", "token": "TOKEN"})
IG_ACCOUNTS = load_accounts("IG", {"user_id": "USER_ID", "token": "TOKEN"})
TT_ACCOUNTS = load_accounts("TT", {"token": "TOKEN"})


def extract_drive_id(url: str) -> str:
    m = re.search(r"/d/([a-zA-Z0-9_-]+)", url)
    if m:
        return m.group(1)
    m2 = re.search(r"id=([a-zA-Z0-9_-]+)", url)
    return m2.group(1) if m2 else url.strip()


def verify_valid_mp4(file_path: str):
    if not os.path.exists(file_path):
        raise Exception("Drive download failed: File not created.")
    size = os.path.getsize(file_path)
    if size < 50_000:
        raise Exception(f"Invalid video ({size} bytes). Check Drive 'Anyone with link' permission!")
    with open(file_path, "rb") as f:
        header = f.read(64)
    if b"<!DOCTYPE html" in header or b"<html" in header.lower():
        raise Exception("Google Drive returned HTML permission page instead of MP4!")


def parse_targets(targets_str: str):
    out = []
    for part in (targets_str or "").split(","):
        p = part.strip()
        if not p or ":" not in p:
            continue
        platform, account = p.split(":", 1)
        out.append((platform.strip(), account.strip()))
    return out


def report_usage(platform: str, account: str):
    """Best-effort: tell the Worker one upload succeeded, for daily quota accounting."""
    try:
        requests.post(
            f"{WORKER_URL}/api/usage",
            headers={"X-Admin-Password": ADMIN_PASSWORD, "Content-Type": "application/json"},
            json={"platform": platform, "account": account},
            timeout=30,
        )
    except Exception:
        pass


# 1. YOUTUBE DATA API v3
def upload_youtube(acct, idx, video_path, title, description):
    if not acct:
        return f"YT{idx}: Missing Secrets", False
    creds = Credentials(
        None,
        refresh_token=acct["rtoken"],
        token_uri="https://oauth2.googleapis.com/token",
        client_id=acct["cid"],
        client_secret=acct["csecret"],
    )
    youtube = build("youtube", "v3", credentials=creds)
    body = {
        "snippet": {"title": title[:100], "description": description, "categoryId": "22"},
        "status": {"privacyStatus": YOUTUBE_PRIVACY, "selfDeclaredMadeForKids": False},
    }
    media = MediaFileUpload(video_path, chunksize=8 * 1024 * 1024, resumable=True)
    req = youtube.videos().insert(part="snippet,status", body=body, media_body=media)
    response = None
    while response is None:
        _, response = req.next_chunk()
    privacy = response.get("status", {}).get("privacyStatus", "?")
    return f"YT{idx} OK ({response.get('id')}, privacy={privacy})", True


# 2. FACEBOOK GRAPH API
def upload_facebook(acct, idx, video_path, title, description):
    if not acct:
        return f"FB{idx}: Missing Secrets", False
    url = f"https://graph-video.facebook.com/{GRAPH_VERSION}/{acct['page_id']}/videos"
    with open(video_path, "rb") as f:
        res = requests.post(
            url,
            data={"title": title, "description": description, "access_token": acct["token"]},
            files={"source": f},
            timeout=600,
        ).json()
    if "id" in res:
        return f"FB{idx} OK ({res['id']})", True
    raise Exception(f"FB{idx} Error: {res.get('error', {}).get('message', res)}")


# 3. INSTAGRAM REELS API (resumable container + strict FINISHED poll + publish)
def upload_instagram(acct, idx, video_path, caption):
    if not acct:
        return f"IG{idx}: Missing Secrets", False
    init_res = requests.post(
        f"https://graph.facebook.com/{GRAPH_VERSION}/{acct['user_id']}/media",
        data={
            "media_type": "REELS",
            "upload_type": "resumable",
            "caption": caption,
            "access_token": acct["token"],
        },
    ).json()
    if "id" not in init_res:
        raise Exception(f"IG{idx} Container Init Error: {init_res}")
    container_id = init_res["id"]
    upload_uri = init_res.get("uri") or f"https://rupload.facebook.com/ig-api-upload/{GRAPH_VERSION}/{container_id}"
    file_size = os.path.getsize(video_path)
    with open(video_path, "rb") as f:
        up_res = requests.post(
            upload_uri,
            headers={"Authorization": f"OAuth {acct['token']}", "offset": "0", "file_size": str(file_size)},
            data=f,
            timeout=600,
        ).json()
    if up_res.get("error"):
        raise Exception(f"IG{idx} Upload Error: {up_res}")
    is_finished = False
    for _ in range(24):  # up to 6 minutes (24 x 15s)
        time.sleep(15)
        stat = requests.get(
            f"https://graph.facebook.com/{GRAPH_VERSION}/{container_id}",
            params={"fields": "status_code,status", "access_token": acct["token"]},
        ).json()
        code = stat.get("status_code")
        if code == "FINISHED":
            is_finished = True
            break
        if code in ("ERROR", "EXPIRED"):
            raise Exception(f"IG{idx} Processing Failed ({code}): {stat.get('status')}")
    if not is_finished:
        raise Exception(f"IG{idx} Timeout: video did not reach FINISHED within 6 mins.")
    pub_res = requests.post(
        f"https://graph.facebook.com/{GRAPH_VERSION}/{acct['user_id']}/media_publish",
        data={"creation_id": container_id, "access_token": acct["token"]},
    ).json()
    if "id" in pub_res:
        return f"IG{idx} OK ({pub_res['id']})", True
    raise Exception(f"IG{idx} Publish Failed: {pub_res}")


# 4. TIKTOK CONTENT POSTING API (INBOX default = no audit needed; DIRECT defaults to SELF_ONLY)
def upload_tiktok(acct, idx, video_path, title):
    if not acct:
        return f"TT{idx}: Missing Secrets", False
    token = acct["token"]
    file_size = os.path.getsize(video_path)
    chunk_size = 10 * 1024 * 1024 if file_size > 10 * 1024 * 1024 else file_size
    total_chunks = math.ceil(file_size / chunk_size)
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json; charset=UTF-8"}
    if TIKTOK_MODE == "INBOX":
        init_url = "https://open.tiktokapis.com/v2/post/publish/inbox/video/init/"
        body = {"source_info": {"source": "FILE_UPLOAD", "video_size": file_size, "chunk_size": chunk_size, "total_chunk_count": total_chunks}}
    else:
        init_url = "https://open.tiktokapis.com/v2/post/publish/video/init/"
        body = {
            "post_info": {"title": title[:150], "privacy_level": TIKTOK_PRIVACY},
            "source_info": {"source": "FILE_UPLOAD", "video_size": file_size, "chunk_size": chunk_size, "total_chunk_count": total_chunks},
        }
    init_res = requests.post(init_url, headers=headers, json=body).json()
    if init_res.get("error", {}).get("code") != "ok":
        raise Exception(f"TT{idx} Init Error: {init_res.get('error')}")
    publish_id = init_res["data"]["publish_id"]
    upload_url = init_res["data"]["upload_url"]
    with open(video_path, "rb") as f:
        for i in range(total_chunks):
            chunk_data = f.read(chunk_size)
            start_byte = i * chunk_size
            end_byte = start_byte + len(chunk_data) - 1
            up_r = requests.put(
                upload_url,
                headers={"Content-Range": f"bytes {start_byte}-{end_byte}/{file_size}", "Content-Type": "video/mp4"},
                data=chunk_data,
                timeout=300,
            )
            if up_r.status_code not in (200, 201, 206):
                raise Exception(f"TT{idx} Chunk {i + 1} HTTP {up_r.status_code}")
    for _ in range(18):  # up to 3 minutes (18 x 10s)
        time.sleep(10)
        st_res = requests.post(
            "https://open.tiktokapis.com/v2/post/publish/status/fetch/",
            headers=headers,
            json={"publish_id": publish_id},
        ).json()
        status = st_res.get("data", {}).get("status")
        if status in ("PUBLISH_COMPLETE", "SEND_TO_USER_INBOX"):
            return f"TT{idx} OK ({status})", True
        if status == "FAILED":
            raise Exception(f"TT{idx} Publish Failed: {st_res.get('data', {}).get('fail_reason', 'Unknown')}")
    return f"TT{idx} Queued ({publish_id})", True


def post_status(job_id, status, log):
    requests.post(
        f"{WORKER_URL}/api/status",
        headers={"X-Admin-Password": ADMIN_PASSWORD, "Content-Type": "application/json"},
        json={"id": job_id, "status": status, "log": log},
        timeout=30,
    )


def main():
    if not WORKER_URL or not ADMIN_PASSWORD:
        raise SystemExit("WORKER_URL aur ADMIN_PASSWORD env zaroori hain.")
    if not RUN_ID:
        raise SystemExit("RUN_ID missing hai — workflow_dispatch input 'run_id' zaroori hai (duplicate rokne ke liye).")
    auth_headers = {"X-Admin-Password": ADMIN_PASSWORD, "Content-Type": "application/json"}
    res = requests.get(f"{WORKER_URL}/api/due?run_id={RUN_ID}", headers=auth_headers, timeout=30)
    res.raise_for_status()
    jobs = res.json().get("jobs", [])
    print(f"Run {RUN_ID}: {len(jobs)} job(s) mile.")
    for job in jobs:
        job_id = job["id"]
        vid_path = os.path.join(TMP_DIR, f"video_{job_id}.mp4")
        logs = []
        ok_count = 0
        fail_count = 0
        try:
            file_id = extract_drive_id(job["drive_link"])
            gdown.download(id=file_id, output=vid_path, quiet=False)
            verify_valid_mp4(vid_path)
            mb = round(os.path.getsize(vid_path) / (1024 * 1024), 1)
            logs.append(f"Drive({mb}MB)")
            targets = parse_targets(job.get("targets") or "")
            if not targets:
                # Fallback: purani rows jin mein targets na ho — sirf account 1 par try karo.
                plats = [p.strip() for p in (job.get("platforms") or "").split(",") if p.strip()]
                plat_map = {"YouTube": "yt", "Facebook": "fb", "Instagram": "ig", "TikTok": "tt"}
                targets = [(plat_map[p], "1") for p in plats if p in plat_map]
            title = job["title"]
            desc = job.get("description") or title
            for platform, account in targets:
                try:
                    if platform == "yt":
                        msg, ok = upload_youtube(YT_ACCOUNTS.get(account), account, vid_path, title, desc)
                    elif platform == "fb":
                        msg, ok = upload_facebook(FB_ACCOUNTS.get(account), account, vid_path, title, desc)
                    elif platform == "ig":
                        msg, ok = upload_instagram(IG_ACCOUNTS.get(account), account, vid_path, f"{title}\n\n{desc}")
                    elif platform == "tt":
                        msg, ok = upload_tiktok(TT_ACCOUNTS.get(account), account, vid_path, title)
                    else:
                        msg, ok = f"Unknown platform {platform}", False
                    logs.append(msg)
                    if ok:
                        ok_count += 1
                        report_usage(platform, account)
                    else:
                        fail_count += 1
                except Exception as e:
                    logs.append(f"{platform}{account} Error: {str(e)[:200]}")
                    fail_count += 1
            if ok_count and not fail_count:
                final = "Published"
            elif ok_count and fail_count:
                final = "Partial"
            else:
                final = "Failed"
            post_status(job_id, final, " | ".join(logs))
        except Exception as e:
            post_status(job_id, "Failed", str(e)[:300])
        finally:
            if os.path.exists(vid_path):
                os.remove(vid_path)


if __name__ == "__main__":
    main()
