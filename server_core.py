import json
import re
from typing import Optional, Dict


class StreamExtractor:
    """Encapsulates YouTube audio URL extraction with multiple strategies.

    Strategies, in order:
    1) YouTube Music Player API (WEB_REMIX)
    2) YouTube Web Player API (ANDROID/WEB)
    3) Modern HTML page parsing for playerResponse
    Optional: a caller can add yt-dlp handling outside this class.
    """

    def __init__(self, quality: str = 'high') -> None:
        self.quality = (quality or 'high').lower()

    def get_best_audio_url(self, video_id: str) -> Optional[Dict[str, str]]:
        url = None
        data = self._try_youtube_music_api(video_id)
        if not url and data:
            url = data.get('url')
            if url:
                return data

        data = self._try_youtube_standard_api(video_id)
        if not url and data:
            url = data.get('url')
            if url:
                return data

        data = self._try_modern_html_extraction(video_id)
        if not url and data:
            url = data.get('url')
            if url:
                return data

        return None

    # --- Internals ---

    def _select_format(self, audio_formats):
        quality_map = {'high': 192, 'medium': 128, 'low': 96}
        target = quality_map.get(self.quality, 128)
        best = None
        best_diff = 10**9
        for fmt in audio_formats:
            try:
                if not str(fmt.get('mimeType', '')).startswith('audio/'):
                    continue
                bitrate = int(fmt.get('bitrate', 0))
            except Exception:
                bitrate = 0
            diff = abs(bitrate - target)
            if diff < best_diff and fmt.get('url'):
                best = fmt
                best_diff = diff
        return best

    def _try_youtube_music_api(self, video_id: str) -> Optional[Dict[str, str]]:
        try:
            import requests
            api_url = 'https://music.youtube.com/youtubei/v1/player'
            body = {
                'context': {
                    'client': {
                        'clientName': 'WEB_REMIX',
                        'clientVersion': '1.20231219.01.00',
                        'userAgent': 'Mozilla/5.0'
                    }
                },
                'videoId': video_id,
                'params': 'CgIQBg=='
            }
            headers = {
                'User-Agent': 'Mozilla/5.0',
                'Accept': 'application/json',
                'Content-Type': 'application/json',
                'Origin': 'https://music.youtube.com',
                'Referer': 'https://music.youtube.com/'
            }
            r = requests.post(api_url, json=body, headers=headers, timeout=20)
            if r.status_code != 200:
                return None
            data = r.json() or {}
            sd = data.get('streamingData') or {}
            af = sd.get('adaptiveFormats') or []
            best = self._select_format(af)
            if best and str(best.get('url', '')).startswith('http'):
                return {
                    'url': best['url'],
                    'mime': best.get('mimeType', 'audio/mp4'),
                    'bitrate': str(best.get('bitrate', 128)),
                    'source': 'youtube_music_api'
                }
        except Exception:
            return None
        return None

    def _try_youtube_standard_api(self, video_id: str) -> Optional[Dict[str, str]]:
        try:
            import requests
            api_url = 'https://www.youtube.com/youtubei/v1/player'
            headers = {
                'User-Agent': 'Mozilla/5.0',
                'Accept': 'application/json',
                'Content-Type': 'application/json',
                'Origin': 'https://www.youtube.com',
                'Referer': 'https://www.youtube.com/'
            }
            clients = [
                {
                    'context': {'client': {'clientName': 'ANDROID', 'clientVersion': '19.09.37', 'androidSdkVersion': 30, 'userAgent': 'com.google.android.youtube/19.09.37'}},
                    'videoId': video_id,
                    'params': 'CgIQBg=='
                },
                {
                    'context': {'client': {'clientName': 'WEB', 'clientVersion': '2.20231219.01.00', 'userAgent': 'Mozilla/5.0'}},
                    'videoId': video_id,
                    'params': 'CgIQBg=='
                }
            ]
            for body in clients:
                r = requests.post(api_url, json=body, headers=headers, timeout=20)
                if r.status_code != 200:
                    continue
                data = r.json() or {}
                sd = data.get('streamingData') or {}
                af = sd.get('adaptiveFormats') or []
                best = self._select_format(af)
                if best and str(best.get('url', '')).startswith('http'):
                    return {
                        'url': best['url'],
                        'mime': best.get('mimeType', 'audio/mp4'),
                        'bitrate': str(best.get('bitrate', 128)),
                        'source': 'youtube_standard_api'
                    }
        except Exception:
            return None
        return None

    def _try_modern_html_extraction(self, video_id: str) -> Optional[Dict[str, str]]:
        try:
            import requests
            url = f'https://www.youtube.com/watch?v={video_id}'
            headers = {
                'User-Agent': 'Mozilla/5.0 (iPhone; CPU iPhone OS 14_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/14.0 Mobile/15E148 Safari/604.1',
                'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8'
            }
            r = requests.get(url, headers=headers, timeout=20)
            if r.status_code != 200:
                return None
            html = r.text or ''
            patterns = [
                r'var ytInitialPlayerResponse = ({.+?});',
                r'ytInitialPlayerResponse\s*=\s*({.+?});',
                r'"playerResponse":\s*({.+?})',
            ]
            player = None
            for p in patterns:
                m = re.search(p, html, re.DOTALL)
                if m:
                    try:
                        player = json.loads(m.group(1))
                        break
                    except Exception:
                        continue
            if not isinstance(player, dict):
                return None
            sd = player.get('streamingData') or {}
            af = sd.get('adaptiveFormats') or []
            best = self._select_format(af)
            if best and str(best.get('url', '')).startswith('http'):
                u = best['url']
                if 'googlevideo.com' not in u:
                    return None
                return {
                    'url': u,
                    'mime': best.get('mimeType', 'audio/mp4'),
                    'bitrate': str(best.get('bitrate', 128)),
                    'source': 'modern_html_extraction'
                }
        except Exception:
            return None
        return None


