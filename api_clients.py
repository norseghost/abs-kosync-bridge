# [START FILE: abs-kosync-enhanced/api_clients.py]
import os
import requests
import logging
import time
import hashlib
from requests.auth import HTTPBasicAuth
from logging_utils import sanitize_log_data

logger = logging.getLogger(__name__)


class ABSClient:
    def __init__(self):
        # Kept your variable names (ABS_SERVER / ABS_KEY)
        self.base_url = os.environ.get("ABS_SERVER", "").rstrip('/')
        self.token = os.environ.get("ABS_KEY")
        self.headers = {"Authorization": f"Bearer {self.token}"}

    def check_connection(self):
        # Verify configuration first
        if not self.base_url or not self.token:
            logger.warning("⚠️ Audiobookshelf not configured (skipping)")
            return False

        url = f"{self.base_url}/api/me"
        try:
            r = requests.get(url, headers=self.headers, timeout=5)
            if r.status_code == 200:
                # If this is the first container start, show INFO for visibility; otherwise use DEBUG
                first_run_marker = '/data/.first_run_done'
                try:
                    first_run = not os.path.exists(first_run_marker)
                except Exception:
                    first_run = False

                if first_run:
                    username = r.json().get('username', 'Unknown')
                    logger.info(
                        f"✅ Connected to Audiobookshelf as user: {username}")
                    try:
                        open(first_run_marker, 'w').close()
                    except Exception:
                        pass
                return True
            else:
                # Keep failure visible as warning
                logger.warning(f"❌ Audiobookshelf Connection Failed: {
                               r.status_code} - {sanitize_log_data(r.text)}")
                return False
        except requests.exceptions.ConnectionError:
            logger.warning(f"❌ Could not connect to Audiobookshelf at {
                           self.base_url}. Check URL and Docker Network.")
            return False
        except Exception as e:
            logger.warning(f"❌ Audiobookshelf Error: {e}")
            return False

    def get_all_audiobooks(self):
        lib_url = f"{self.base_url}/api/libraries"
        try:
            r = requests.get(lib_url, headers=self.headers)
            if r.status_code != 200:
                return []
            libraries = r.json().get('libraries', [])
            all_audiobooks = []
            for lib in libraries:
                items_url = f"{self.base_url}/api/libraries/{lib['id']}/items"
                params = {"mediaType": "audiobook"}
                r_items = requests.get(
                    items_url, headers=self.headers, params=params)
                if r_items.status_code == 200:
                    all_audiobooks.extend(r_items.json().get('results', []))
            return all_audiobooks
        except Exception as e:
            logger.error(f"Exception fetching audiobooks: {e}")
            return []

    def get_audio_files(self, item_id):
        url = f"{self.base_url}/api/items/{item_id}"
        try:
            r = requests.get(url, headers=self.headers)
            if r.status_code == 200:
                data = r.json()
                files = []
                # Return list of dicts with stream_url and ext (for transcriber)
                audio_files = data.get('media', {}).get('audioFiles', [])
                audio_files.sort(key=lambda x: (
                    x.get('disc', 0) or 0, x.get('track', 0) or 0))

                for af in audio_files:
                    stream_url = f"{
                        self.base_url}/api/items/{item_id}/file/{af['ino']}?token={self.token}"
                    # Return dict with stream URL and extension (default to mp3)
                    files.append({
                        "stream_url": stream_url,
                        "ext": af.get("ext", "mp3")
                    })
                return files
            return []
        except Exception as e:
            logger.error(f"Error getting audio files: {e}")
            return []

    def get_item_details(self, item_id):
        url = f"{self.base_url}/api/items/{item_id}"
        try:
            r = requests.get(url, headers=self.headers)
            if r.status_code == 200:
                return r.json()
        except:
            pass
        return None

    def get_progress(self, item_id):
        url = f"{self.base_url}/api/me/progress/{item_id}"
        try:
            r = requests.get(url, headers=self.headers)
            if r.status_code == 200:
                return r.json().get('currentTime', 0)
        except:
            pass
        return 0.0

    def update_progress(self, abs_id, timestamp, time_listened=None):
        """
        Update progress using session-based sync.
        Creates a session, syncs progress, then closes the session.
        """
        if timestamp > 1000000:
            timestamp = timestamp / 1000.0
            logger.warning(
                f"⚠️ Converted ABS timestamp from milliseconds to seconds: {timestamp}")

        timestamp = float(timestamp)
        if time_listened is None:
            time_listened = 0.0
        time_listened = float(time_listened)

        session_id = self.create_session(abs_id)
        if not session_id:
            logger.error(f"Failed to create ABS session for item {abs_id}")
            return False

        try:
            url = f"{self.base_url}/api/session/{session_id}/sync"
            payload = {
                "currentTime": timestamp,
                "timeListened": time_listened
            }
            r = requests.post(url, headers=self.headers,
                              json=payload, timeout=10)

            if r.status_code in (200, 204):
                logger.debug(f"ABS progress updated via session: {
                             abs_id} -> {timestamp}s")
                success = True
            elif r.status_code == 404:
                logger.warning(f"ABS session not found (404): {session_id}")
                success = False
            else:
                logger.error(f"ABS session sync failed: {
                             r.status_code} - {r.text}")
                success = False
        except Exception as e:
            logger.error(f"Failed to sync ABS session progress: {e}")
            success = False

        self.close_session(session_id)

        return success

    def get_in_progress(self, min_progress=0.01):
        url = f"{self.base_url}/api/me/progress"
        try:
            r = requests.get(url, headers=self.headers, timeout=10)
            if r.status_code != 200:
                return []
            data = r.json()
            # Handle both direct list and wrapped dictionary response formats
            items = data if isinstance(data, list) else data.get(
                'libraryItemsInProgress', [])
            active_items = []
            for item in items:
                # Filter for audiobooks only
                if item.get('mediaType') and item.get('mediaType') != 'audiobook':
                    continue

                duration = item.get('duration', 0)
                current_time = item.get('currentTime', 0)
                if duration == 0 or item.get('isFinished'):
                    continue

                pct = current_time / duration
                if pct >= min_progress:
                    lib_item_id = item.get(
                        'libraryItemId') or item.get('itemId')
                    if not lib_item_id:
                        continue

                    # Quick detail fetch to get Title/Author
                    details = self.get_item_details(lib_item_id)
                    if not details:
                        continue
                    metadata = details.get('media', {}).get('metadata', {})

                    active_items.append({
                        "id": lib_item_id,
                        "title": metadata.get('title', details.get('name', 'Unknown')),
                        "author": metadata.get('authorName'),
                        "progress": pct,
                        "duration": duration,
                        "source": "ABS"
                    })
            return active_items
        except Exception as e:
            logger.error(f"Error fetching ABS sessions: {e}")
            return []

    def create_session(self, abs_id):
        """Create a new ABS session for the given abs_id (item id). Returns session_id or None."""
        play_url = f"{self.base_url}/api/items/{abs_id}/play"
        play_payload = {
            "deviceInfo": {
                "id": "abs-kosync-bot",
                "deviceId": "abs-kosync-bot",
                "clientName": "ABS-KoSync-Bridge",
                "clientVersion": "1.0",
                "manufacturer": "ABS-KoSync",
                "model": "Bridge",
                "sdkVersion": "1.0"
            },
            "mediaPlayer": "ABS-KoSync-Bridge",
            "supportedMimeTypes": ["audio/mpeg", "audio/mp4"],
            "forceDirectPlay": True,
            "forceTranscode": False
        }
        try:
            r = requests.post(play_url, headers=self.headers,
                              json=play_payload, timeout=10)
            if r.status_code == 200:
                id = r.json().get('id')
                logger.debug(f"Created new ABS session for item {
                             abs_id}, id: {id}")
                return id
            else:
                logger.error(f"Failed to create ABS session: {
                             r.status_code} - {r.text}")
        except Exception as e:
            logger.error(f"Exception creating ABS session: {e}")
        return None

    def close_session(self, session_id):
        try:
            close_url = f"{self.base_url}/api/session/{session_id}/close"
            requests.post(close_url, headers=self.headers, timeout=5)
        except Exception as e:
            logger.warning(f"⚠️ Failed to close session for ABS: {e}")


class KoSyncClient:
    def __init__(self):
        self.base_url = os.environ.get("KOSYNC_SERVER", "").rstrip('/')
        self.user = os.environ.get("KOSYNC_USER")
        # Calibre-web expects the plaintext password for Basic Auth
        self.password = os.environ.get("KOSYNC_KEY", "")

        # Pre-configure the Auth object
        self.auth = HTTPBasicAuth(
            self.user, self.password) if self.user else None

    def is_configured(self):
        return bool(self.base_url and self.user)

    def _mark_first_run(self):
        """Helper to manage the first-run log message and marker file."""
        marker = '/data/.first_run_done'
        if not os.path.exists(marker):
            logger.info(f"✅ Successfully authenticated with KoSync at {
                        self.base_url}")
            try:
                os.makedirs(os.path.dirname(marker), exist_ok=True)
                with open(marker, 'w') as f:
                    f.close()
            except Exception:
                pass

    def check_connection(self):
        if not self.is_configured():
            logger.warning("⚠️ KoSync not configured (skipping)")
            return False

        # Calibre-web/Koreader API usually prefers this header
        headers = {'accept': 'application/vnd.koreader.v1+json'}

        try:
            # Try primary healthcheck endpoint
            url = f"{self.base_url}/healthcheck"
            r = requests.get(url, auth=self.auth, headers=headers, timeout=5)

            # If 404 or fail, try the progress test endpoint
            if r.status_code != 200:
                url_test = f"{self.base_url}/syncs/progress/test-connection"
                r = requests.get(url_test, auth=self.auth,
                                 headers=headers, timeout=5)

            if r.status_code == 200:
                self._mark_first_run()
                return True

            logger.warning(f"❌ KoSync Unauthorized or Error: {r.status_code}")
            return False

        except Exception as e:
            logger.warning(f"❌ KoSync Connection Error: {e}")
            return False

    def get_progress(self, doc_id):
        if not self.is_configured():
            return 0.0, None

        headers = {'accept': 'application/vnd.koreader.v1+json'}
        url = f"{self.base_url}/syncs/progress/{doc_id}"

        try:
            r = requests.get(url, auth=self.auth, headers=headers, timeout=10)
            if r.status_code == 200:
                data = r.json()
                pct = float(data.get('percentage', 0))
                xpath = data.get('progress')
                return pct, xpath
        except Exception as e:
            logger.debug(f"Could not fetch progress for {doc_id}: {e}")

        return 0.0, None

    def update_progress(self, doc_id, percentage, xpath=None):
        if not self.is_configured():
            return False

        headers = {
            'accept': 'application/vnd.koreader.v1+json',
            'content-type': 'application/json'
        }
        url = f"{self.base_url}/syncs/progress"

        payload = {
            "document": doc_id,
            "percentage": percentage,
            "progress": xpath if xpath else "",
            "device": "abs-sync-bot",
            "device_id": "abs-sync-bot",
            "timestamp": int(time.time())
        }

        try:
            r = requests.put(url, auth=self.auth,
                             headers=headers, json=payload, timeout=10)
            if r.status_code in (200, 201, 204):
                logger.debug(f"📡 KoSync Updated: {
                             percentage:.1%} for {doc_id}")
                return True
            else:
                logger.error(f"Failed to update KoSync: {
                             r.status_code} - {r.text}")
                return False
        except Exception as e:
            logger.error(f"Failed to update KoSync: {e}")
            return False
# [END FILE]
