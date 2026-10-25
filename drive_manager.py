import os
import io
import json
from typing import Optional, Dict, Any
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from google_auth_oauthlib.flow import Flow
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload, MediaIoBaseDownload

SCOPES = ["https://www.googleapis.com/auth/drive.file"]

CLIENT_ID = os.getenv("GMAIL_CLIENT_ID", "")
CLIENT_SECRET = os.getenv("GMAIL_CLIENT_SECRET", "")
FOLDER_ID = os.getenv("GOOGLE_DRIVE_FOLDER_ID", "")
TOKEN_PATH = os.getenv("GOOGLE_TOKEN_PATH", "/data/google_token.json")
REDIRECT_URI = os.getenv("GOOGLE_REDIRECT_URI", "https://pitih.haluih.id/api/drive/callback")

def _get_client_config() -> Dict[str, Any]:
    return {
        "web": {
            "client_id": CLIENT_ID,
            "client_secret": CLIENT_SECRET,
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
            "redirect_uris": [REDIRECT_URI]
        }
    }

def is_drive_connected() -> bool:
    if not os.path.exists(TOKEN_PATH):
        return False
    try:
        creds = Credentials.from_authorized_user_file(TOKEN_PATH, SCOPES)
        return creds.valid or (creds.expired and bool(creds.refresh_token))
    except Exception:
        return False

def get_auth_url() -> str:
    import urllib.parse
    params = {
        "client_id": CLIENT_ID,
        "redirect_uri": REDIRECT_URI,
        "response_type": "code",
        "scope": " ".join(SCOPES),
        "access_type": "offline",
        "prompt": "consent",
        "include_granted_scopes": "true"
    }
    return "https://accounts.google.com/o/oauth2/v2/auth?" + urllib.parse.urlencode(params)

def exchange_code_for_token(code: str) -> bool:
    import httpx
    token_url = "https://oauth2.googleapis.com/token"
    data = {
        "client_id": CLIENT_ID,
        "client_secret": CLIENT_SECRET,
        "code": code,
        "grant_type": "authorization_code",
        "redirect_uri": REDIRECT_URI
    }
    try:
        resp = httpx.post(token_url, data=data, timeout=20.0)
        if resp.status_code != 200:
            print(f"[GoogleDrive] Token exchange failed ({resp.status_code}): {resp.text}")
            return False
            
        token_info = resp.json()
        creds = Credentials(
            token=token_info.get("access_token"),
            refresh_token=token_info.get("refresh_token"),
            token_uri=token_url,
            client_id=CLIENT_ID,
            client_secret=CLIENT_SECRET,
            scopes=SCOPES
        )
        global _CACHED_FOLDER_ID
        _CACHED_FOLDER_ID = None
        os.makedirs(os.path.dirname(os.path.abspath(TOKEN_PATH)), exist_ok=True)
        with open(TOKEN_PATH, "w") as f:
            f.write(creds.to_json())
        print(f"[GoogleDrive] Successfully exchanged code and saved token to {TOKEN_PATH}")
        return True
    except Exception as e:
        print(f"[GoogleDrive] Error exchanging code for token: {e}")
        return False

def get_drive_service():
    if not os.path.exists(TOKEN_PATH):
        return None
    try:
        creds = Credentials.from_authorized_user_file(TOKEN_PATH, SCOPES)
        if creds.expired and creds.refresh_token:
            creds.refresh(Request())
            with open(TOKEN_PATH, "w") as f:
                f.write(creds.to_json())
        return build("drive", "v3", credentials=creds)
    except Exception as e:
        print(f"[GoogleDrive] Error building service: {e}")
        return None

_CACHED_FOLDER_ID: Optional[str] = None

def get_or_create_folder(service, folder_name: str = "Pitih Struk") -> Optional[str]:
    global _CACHED_FOLDER_ID
    if _CACHED_FOLDER_ID:
        return _CACHED_FOLDER_ID

    if FOLDER_ID:
        try:
            folder = service.files().get(fileId=FOLDER_ID, fields="id, name, trashed").execute()
            if not folder.get("trashed"):
                _CACHED_FOLDER_ID = FOLDER_ID
                return _CACHED_FOLDER_ID
        except Exception:
            pass

    try:
        q = f"mimeType = 'application/vnd.google-apps.folder' and name = '{folder_name}' and trashed = false"
        res = service.files().list(q=q, fields="files(id, name)").execute()
        files = res.get("files", [])
        if files:
            _CACHED_FOLDER_ID = files[0]["id"]
            return _CACHED_FOLDER_ID
    except Exception as e:
        print(f"[GoogleDrive] Error searching for folder '{folder_name}': {e}")

    try:
        meta = {
            "name": folder_name,
            "mimeType": "application/vnd.google-apps.folder"
        }
        created = service.files().create(body=meta, fields="id, name").execute()
        _CACHED_FOLDER_ID = created.get("id")
        print(f"[GoogleDrive] Created new folder '{folder_name}' with ID: {_CACHED_FOLDER_ID}")
        return _CACHED_FOLDER_ID
    except Exception as e:
        print(f"[GoogleDrive] Error creating folder '{folder_name}': {e}")
        return None

def upload_receipt_to_drive(local_path: str, filename: str) -> Optional[Dict[str, Any]]:
    service = get_drive_service()
    if not service:
        print("[GoogleDrive] Drive service not connected, skipping Drive upload.")
        return None

    try:
        folder_id = get_or_create_folder(service)
        file_metadata: Dict[str, Any] = {"name": filename}
        if folder_id:
            file_metadata["parents"] = [folder_id]

        media = MediaFileUpload(local_path, mimetype="image/jpeg", resumable=True)
        file = service.files().create(
            body=file_metadata,
            media_body=media,
            fields="id, name, webViewLink, thumbnailLink"
        ).execute()

        file_id = file.get("id")
        web_view_link = file.get("webViewLink")
        print(f"[GoogleDrive] Uploaded {filename} -> File ID: {file_id} (Folder: {folder_id})")

        # Delete local file after successful upload to prevent storage bloat
        if os.path.exists(local_path):
            try:
                os.remove(local_path)
                print(f"[GoogleDrive] Removed local temporary file: {local_path}")
            except Exception as re:
                print(f"[GoogleDrive] Could not remove local file: {re}")

        return {
            "file_id": file_id,
            "name": file.get("name"),
            "web_view_link": web_view_link,
            "image_url": f"/api/receipts/drive/{file_id}"
        }
    except Exception as e:
        print(f"[GoogleDrive] Failed to upload {filename} to Google Drive: {e}")
        return None

def download_drive_file_stream(file_id: str) -> Optional[io.BytesIO]:
    service = get_drive_service()
    if not service:
        return None
    try:
        request = service.files().get_media(fileId=file_id)
        fh = io.BytesIO()
        downloader = MediaIoBaseDownload(fh, request)
        done = False
        while not done:
            _, done = downloader.next_chunk()
        fh.seek(0)
        return fh
    except Exception as e:
        print(f"[GoogleDrive] Error downloading file {file_id}: {e}")
        return None

def delete_drive_file(file_id: str) -> bool:
    service = get_drive_service()
    if not service:
        return False
    try:
        service.files().delete(fileId=file_id).execute()
        print(f"[GoogleDrive] Deleted file {file_id} from Drive")
        return True
    except Exception as e:
        print(f"[GoogleDrive] Failed to delete file {file_id} from Drive: {e}")
        return False
