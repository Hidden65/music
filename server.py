import os
import json
import posixpath
import urllib.parse
from typing import List, Dict, Any, Optional
from http.server import SimpleHTTPRequestHandler
from socketserver import ThreadingTCPServer
import time
import sqlite3
import urllib.request
import urllib.error
import re
import threading
from collections import defaultdict

try:
    from ytmusicapi import YTMusic
    YTMUSIC_AVAILABLE = True
except ImportError:
    YTMUSIC_AVAILABLE = False
    print("Warning: ytmusicapi not installed. Using demo mode.")

ROOT_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(ROOT_DIR, 'wave_music.db')
# Optional: external backend for fallback (disabled by default)
REMOTE_BASE_URL = os.environ.get('REMOTE_BASE_URL')

# Rate limiter to prevent too many requests to YouTube
class RateLimiter:
    def __init__(self, max_requests=10, time_window=60):
        self.max_requests = max_requests
        self.time_window = time_window
        self.requests = defaultdict(list)
        self.lock = threading.Lock()
    
    def can_make_request(self, key="default"):
        with self.lock:
            now = time.time()
            # Clean old requests
            self.requests[key] = [req_time for req_time in self.requests[key] 
                                if now - req_time < self.time_window]
            
            # Check if we can make a new request
            if len(self.requests[key]) < self.max_requests:
                self.requests[key].append(now)
                return True
            return False
    
    def wait_if_needed(self, key="default"):
        while not self.can_make_request(key):
            time.sleep(1)

# Global rate limiter instance
rate_limiter = RateLimiter(max_requests=5, time_window=60)

# Simple cache for stream URLs to reduce YouTube requests
stream_cache = {}
CACHE_DURATION = 300  # 5 minutes

def init_database():
    """Initialize SQLite database for storing user data (fallback if Firebase not available)"""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    
    # Users table
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS users (
            id TEXT PRIMARY KEY,
            email TEXT UNIQUE NOT NULL,
            name TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    
    # Liked songs table
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS liked_songs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id TEXT NOT NULL,
            video_id TEXT NOT NULL,
            title TEXT NOT NULL,
            artist TEXT,
            thumbnail TEXT,
            duration TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (user_id) REFERENCES users (id),
            UNIQUE(user_id, video_id)
        )
    ''')
    
    # Playlists table
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS playlists (
            id TEXT PRIMARY KEY,
            user_id TEXT NOT NULL,
            name TEXT NOT NULL,
            description TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (user_id) REFERENCES users (id)
        )
    ''')
    
    # Playlist songs table
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS playlist_songs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            playlist_id TEXT NOT NULL,
            video_id TEXT NOT NULL,
            title TEXT NOT NULL,
            artist TEXT,
            thumbnail TEXT,
            duration TEXT,
            position INTEGER NOT NULL,
            added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (playlist_id) REFERENCES playlists (id)
        )
    ''')
    
    conn.commit()
    conn.close()

def is_english_text(text: str) -> bool:
    """Check if text is primarily in English"""
    if not text:
        return True
    
    # Count English characters vs non-English characters
    english_chars = sum(1 for c in text if c.isascii() and c.isalpha())
    total_chars = sum(1 for c in text if c.isalpha())
    
    if total_chars == 0:
        return True
    
    # Consider text English if more than 70% of alphabetic characters are ASCII
    return (english_chars / total_chars) > 0.7

def map_song_result(item: Dict[str, Any]) -> Dict[str, Any]:
    """Map YouTube Music API result to standardized format"""
    video_id = item.get('videoId') or (item.get('navigationEndpoint', {}).get('watchEndpoint', {}).get('videoId'))
    title = item.get('title') or item.get('name', 'Unknown Title')
    
    # Handle artists
    artists = item.get('artists') or []
    artist = None
    if isinstance(artists, list) and artists:
        artist = ', '.join([a.get('name') for a in artists if a and a.get('name')])
    elif isinstance(item.get('artist'), str):
        artist = item.get('artist')
    
    # Handle duration
    duration = item.get('duration') or item.get('duration_seconds')
    if isinstance(duration, (int, float)):
        mins, secs = divmod(int(duration), 60)
        duration = f"{mins}:{secs:02d}"
    
    # Handle thumbnails
    thumbs = item.get('thumbnails') or item.get('thumbnail') or []
    thumb_url = None
    if isinstance(thumbs, list) and thumbs:
        # Get highest quality thumbnail
        thumb_url = thumbs[-1].get('url')
    elif isinstance(thumbs, str):
        thumb_url = thumbs
    
    return {
        'videoId': video_id,
        'title': title,
        'artist': artist or 'Unknown Artist',
        'duration': duration,
        'thumbnail': thumb_url
    }

def fetch_remote_json(path_with_query: str) -> Optional[Dict[str, Any]]:
    """Fetch JSON from external backend as a fallback when local YTMusic is unavailable.

    Disabled unless REMOTE_BASE_URL is set. Times out quickly and fails quietly.
    """
    if not REMOTE_BASE_URL:
        return None
    url = REMOTE_BASE_URL.rstrip('/') + path_with_query
    try:
        req = urllib.request.Request(
            url,
            headers={
                'Accept': 'application/json',
                'User-Agent': 'WaveMusicServer/1.0'
            }
        )
        with urllib.request.urlopen(req, timeout=4) as resp:
            if resp.getcode() == 200:
                data = json.loads(resp.read().decode('utf-8'))
                return data
    except urllib.error.HTTPError as e:
        # Quietly ignore remote fallback errors
        pass
    except urllib.error.URLError as e:
        pass
    except Exception as e:
        pass
    return None

def get_demo_results(query: str) -> List[Dict[str, Any]]:
    """Return empty results when YTMusic is not available"""
    return []

class YTMusicRequestHandler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        # Initialize YouTube Music API
        if YTMUSIC_AVAILABLE:
            headers_path = os.path.join(ROOT_DIR, 'headers_auth.json')
            try:
                if os.path.exists(headers_path):
                    self.ytmusic = YTMusic(headers_path)
                else:
                    self.ytmusic = YTMusic()
            except Exception as e:
                print(f"Error initializing YTMusic: {e}")
                self.ytmusic = None
        else:
            self.ytmusic = None
            
        super().__init__(*args, directory=ROOT_DIR, **kwargs)

    def do_GET(self):  # noqa: N802 (keep stdlib naming)
        parsed = urllib.parse.urlsplit(self.path)
        path = parsed.path
        
        # API routes
        if path == '/api/search':
            self.handle_api_search(parsed.query)
            return
        elif path == '/api/trending':
            self.handle_api_trending()
            return
        elif path == '/api/search_multi':
            self.handle_api_search_multi(parsed.query)
            return
        elif path == '/api/album':
            self.handle_api_album(parsed.query)
            return
        elif path == '/api/artist':
            self.handle_api_artist(parsed.query)
            return
        elif path == '/api/recommendations':
            self.handle_api_recommendations(parsed.query)
            return
        elif path == '/api/user/liked':
            self.handle_api_user_liked(parsed.query)
            return
        elif path == '/api/user/playlists':
            self.handle_api_user_playlists(parsed.query)
            return
        elif path.startswith('/api/playlist/'):
            playlist_id = path.split('/')[-1]
            self.handle_api_playlist(playlist_id)
            return
        elif path == '/api/lyrics':
            self.handle_api_lyrics(parsed.query)
            return
        elif path == '/api/stream-proxy':
            self.handle_api_stream_proxy(parsed.query)
            return
        elif path == '/api/stream':
            self.handle_api_stream(parsed.query)
            return
        
        # Serve static files
        if path == '/':
            # Serve the main HTML file
            self.path = 'static/index.html'
        elif path.startswith('/static/'):
            # Remove leading slash for static files
            self.path = path[1:]
        else:
            # For any other path, try to serve it as a static file
            # This handles cases like /favicon.ico, /robots.txt, etc.
            if not path.startswith('/'):
                path = '/' + path
            self.path = path[1:]  # Remove leading slash
        
        try:
            return super().do_GET()
        except FileNotFoundError:
            # If file not found, serve the main HTML file (for SPA routing)
            self.path = 'static/index.html'
            return super().do_GET()
  


    def do_POST(self):  # noqa: N802 (keep stdlib naming)
        parsed = urllib.parse.urlsplit(self.path)
        path = parsed.path
        
        # Handle POST requests for user data
        if path == '/api/user/like':
            self.handle_api_user_like()
        elif path == '/api/user/unlike':
            self.handle_api_user_unlike()
        elif path == '/api/playlist/create':
            self.handle_api_playlist_create()
        elif path == '/api/playlist/add-song':
            self.handle_api_playlist_add_song()
        elif path == '/api/playlist/remove-song':
            self.handle_api_playlist_remove_song()
        else:
            self.send_error(404, "Not Found")

    def handle_api_search(self, query_string: str) -> None:
        """Handle music search requests"""
        params = urllib.parse.parse_qs(query_string or '')
        q_list = params.get('q', [''])
        q = (q_list[0] if q_list else '').strip()
        
        results: List[Dict[str, Any]] = []
        
        if q:
            if self.ytmusic:
                try:
                    # Search for songs
                    songs = self.ytmusic.search(q, filter='songs', limit=15)
                    results.extend(map(map_song_result, songs))
                    
                    # If not enough results, search videos too
                    if len(results) < 10:
                        videos = self.ytmusic.search(q, filter='videos', limit=15)
                        video_results = map(map_song_result, videos)
                        # Filter out duplicates
                        existing_ids = {r.get('videoId') for r in results}
                        results.extend([r for r in video_results if r.get('videoId') not in existing_ids])
                        
                except Exception as e:
                    print(f"Search error: {e}")
                    # Fallback to hosted backend
                    remote = fetch_remote_json('/api/search?' + (query_string or ''))
                    if remote and isinstance(remote.get('results'), list):
                        results = [r for r in remote['results'] if r.get('videoId')]
                    else:
                        results = get_demo_results(q)
            else:
                # No local API; try hosted backend
                remote = fetch_remote_json('/api/search?' + (query_string or ''))
                if remote and isinstance(remote.get('results'), list):
                    results = [r for r in remote['results'] if r.get('videoId')]
                else:
                    results = get_demo_results(q)
        
        # Filter out results without video IDs
        results = [r for r in results if r.get('videoId')][:20]
        
        self.send_json_response({'results': results})

    def handle_api_search_multi(self, query_string: str) -> None:
        """Return songs, albums, artists, playlists, and podcasts for a query"""
        params = urllib.parse.parse_qs(query_string or '')
        q_list = params.get('q', [''])
        q = (q_list[0] if q_list else '').strip()
        out = {'songs': [], 'albums': [], 'artists': [], 'playlists': [], 'podcasts': []}
        if not q:
            self.send_json_response(out)
            return
        if self.ytmusic:
            try:
                # Search for songs
                songs = self.ytmusic.search(q, filter='songs', limit=15)
                out['songs'] = [map_song_result(s) for s in songs if s]
                
                # Search for albums
                albums = self.ytmusic.search(q, filter='albums', limit=15)
                out['albums'] = [
                    {
                        'albumId': a.get('browseId') or a.get('playlistId') or a.get('videoId'),
                        'title': a.get('title') or a.get('name'),
                        'artist': ', '.join([ar.get('name') for ar in (a.get('artists') or []) if ar.get('name')]) if a.get('artists') else None,
                        'thumbnail': (a.get('thumbnails') or [{}])[-1].get('url') if a.get('thumbnails') else None
                    }
                    for a in albums if a
                ]
                
                # Search for artists
                artists = self.ytmusic.search(q, filter='artists', limit=15)
                out['artists'] = [
                    {
                        'artistId': ar.get('browseId') or ar.get('channelId'),
                        'name': ar.get('artist') or ar.get('title') or ar.get('name'),
                        'thumbnail': (ar.get('thumbnails') or [{}])[-1].get('url') if ar.get('thumbnails') else None
                    }
                    for ar in artists if ar
                ]
                
                # Search for playlists
                playlists = self.ytmusic.search(q, filter='playlists', limit=15)
                out['playlists'] = [
                    {
                        'playlistId': p.get('browseId') or p.get('playlistId'),
                        'title': p.get('title') or p.get('name'),
                        'author': p.get('author') or p.get('artist'),
                        'thumbnail': (p.get('thumbnails') or [{}])[-1].get('url') if p.get('thumbnails') else None,
                        'songCount': p.get('songCount') or p.get('itemCount')
                    }
                    for p in playlists if p
                ]
                
                # Search for podcasts (using community playlists as a proxy)
                try:
                    podcasts = self.ytmusic.search(q, filter='community_playlists', limit=15)
                    out['podcasts'] = [
                        {
                            'podcastId': p.get('browseId') or p.get('playlistId'),
                            'title': p.get('title') or p.get('name'),
                            'author': p.get('author') or p.get('artist'),
                            'thumbnail': (p.get('thumbnails') or [{}])[-1].get('url') if p.get('thumbnails') else None,
                            'episodeCount': p.get('songCount') or p.get('itemCount')
                        }
                        for p in podcasts if p and (
                            'podcast' in (p.get('title', '') + p.get('name', '')).lower() or
                            'episode' in (p.get('title', '') + p.get('name', '')).lower() or
                            'show' in (p.get('title', '') + p.get('name', '')).lower() or
                            'radio' in (p.get('title', '') + p.get('name', '')).lower()
                        )
                    ]
                except Exception as e:
                    print(f"Podcast search error: {e}")
                    out['podcasts'] = []
                    
            except Exception as e:
                print(f"search_multi error: {e}")
                out = self._demo_search_multi(q)
        else:
            out = self._demo_search_multi(q)
        self.send_json_response(out)

    def handle_api_album(self, query_string: str) -> None:
        params = urllib.parse.parse_qs(query_string or '')
        album_id = (params.get('id', [''])[0] or '').strip()
        if not album_id:
            self.send_json_response({'error': 'Album id required'}, 400)
            return
        if self.ytmusic:
            try:
                album = self.ytmusic.get_album(album_id)
                tracks = album.get('tracks') or []
                songs = [map_song_result(t) for t in tracks]
                data = {
                    'albumId': album_id,
                    'title': album.get('title'),
                    'artist': (album.get('artists') or [{}])[0].get('name') if album.get('artists') else None,
                    'thumbnail': ((album.get('thumbnails') or [{}])[-1]).get('url') if album.get('thumbnails') else None,
                    'songs': songs
                }
                self.send_json_response({'album': data})
                return
            except Exception as e:
                print(f"album error: {e}")
        # Empty album response when API is unavailable
        self.send_json_response({'album': {
            'albumId': album_id,
            'title': 'Album Unavailable',
            'artist': 'Unknown Artist',
            'thumbnail': None,
            'songs': []
        }})

    def handle_api_artist(self, query_string: str) -> None:
        params = urllib.parse.parse_qs(query_string or '')
        artist_id = (params.get('id', [''])[0] or '').strip()
        if not artist_id:
            self.send_json_response({'error': 'Artist id required'}, 400)
            return
        if self.ytmusic:
            try:
                artist = self.ytmusic.get_artist(artist_id)
                songs = []
                for sec in (artist.get('songs', {}) or {}).get('results', []) or []:
                    songs.append(map_song_result(sec))
                data = {
                    'artistId': artist_id,
                    'name': artist.get('name'),
                    'thumbnail': ((artist.get('thumbnails') or [{}])[-1]).get('url') if artist.get('thumbnails') else None,
                    'songs': songs or get_demo_results('artist')
                }
                self.send_json_response({'artist': data})
                return
            except Exception as e:
                print(f"artist error: {e}")
        self.send_json_response({'artist': {
            'artistId': artist_id,
            'name': 'Artist Unavailable',
            'thumbnail': None,
            'songs': []
        }})

    def _demo_search_multi(self, q: str) -> Dict[str, Any]:
        # Return empty results for demo mode
        return {
            'songs': [],
            'albums': [],
            'artists': [],
            'playlists': [],
            'podcasts': []
        }

    def handle_api_trending(self) -> None:
        """Handle trending music requests"""
        results: List[Dict[str, Any]] = []
        if self.ytmusic:
            try:
                trending = self.ytmusic.get_charts()
                if trending and 'songs' in trending:
                    results = [map_song_result(song) for song in trending['songs'][:20]]
            except Exception as e:
                print(f"Trending error: {e}")
        # If empty, try hosted backend
        if not results:
            remote = fetch_remote_json('/api/trending')
            if remote and isinstance(remote.get('results'), list):
                results = [r for r in remote['results'] if r.get('videoId')]
        
        self.send_json_response({'results': results})

    def handle_api_recommendations(self, query_string: str) -> None:
        """Handle music recommendations based on a song"""
        params = urllib.parse.parse_qs(query_string or '')
        video_id = params.get('videoId', [''])[0]
        
        results = []
        if video_id and self.ytmusic:
            try:
                # Get similar songs (this is a simplified approach)
                # In a real implementation, you'd use more sophisticated recommendation logic
                watch_playlist = self.ytmusic.get_watch_playlist(video_id, limit=20)
                if 'tracks' in watch_playlist:
                    results = [map_song_result(track) for track in watch_playlist['tracks']]
            except Exception as e:
                print(f"Recommendations error: {e}")
                results = []
        else:
            results = []
        
        self.send_json_response({'results': results})

    def handle_api_stream(self, query_string: str) -> None:
        """Return a direct audio URL for a given YouTube video ID using working method.

        Response: { url: string, itag?: number, mime?: string, bitrate?: number }
        """
        params = urllib.parse.parse_qs(query_string or '')
        video_id = (params.get('videoId', [''])[0] or '').strip()
        quality = (params.get('quality', ['high'])[0] or 'high').strip().lower()
        if not video_id:
            self.send_json_response({'error': 'Video ID required'}, 400)
            return

        print(f"🎵 Stream request for video: {video_id}, quality: {quality}")

        # Check cache first
        cache_key = f"{video_id}_{quality}"
        if cache_key in stream_cache:
            cached_data = stream_cache[cache_key]
            if time.time() - cached_data['timestamp'] < CACHE_DURATION:
                print(f"📦 Using cached stream URL for: {video_id}")
                self.send_json_response(cached_data['data'])
                return
            else:
                # Remove expired cache entry
                del stream_cache[cache_key]

        # ALWAYS use stream proxy as the primary method to handle access restrictions
        print(f"🔄 Using stream proxy for: {video_id}")
        try:
            # Get the host from the request headers
            host = self.headers.get('Host', 'localhost:5000')
            if 'music-h3vv.onrender.com' in host:
                # Use HTTPS for Render deployment
                proxy_url = f"https://{host}/api/stream-proxy?videoId={video_id}&quality={quality}"
            else:
                # Use HTTP for local development
                proxy_url = f"http://{host}/api/stream-proxy?videoId={video_id}&quality={quality}"
            
            print(f"✅ Using stream proxy URL for: {video_id}")
            print(f"🔗 Proxy URL: {proxy_url}")
            response_data = {
                'url': proxy_url,
                'mime': 'audio/mp4',
                'bitrate': 128,
                'itag': '140',
                'videoId': video_id,
                'source': 'stream_proxy'
            }
            # Cache the result
            stream_cache[cache_key] = {
                'data': response_data,
                'timestamp': time.time()
            }
            self.send_json_response(response_data)
            return
        except Exception as e:
            print(f"❌ Stream proxy failed for {video_id}: {e}")
        
        # Fallback: Try YouTube API extraction for deployment environments
        import os
        is_deployment = os.environ.get('RENDER') or os.environ.get('HEROKU') or os.environ.get('VERCEL')
        
        if is_deployment:
            print(f"🌐 Deployment environment detected, trying YouTube API extraction for: {video_id}")
            try:
                api_result = self._try_youtube_api_extraction(video_id, quality)
                if api_result and api_result.get('url') and 'googlevideo.com' in api_result['url']:
                    print(f"✅ Successfully got audio stream via YouTube API for: {video_id}")
                    response_data = {
                        'url': api_result['url'],
                        'mime': api_result.get('mime', 'audio/mp4'),
                        'bitrate': api_result.get('bitrate', 128),
                        'itag': api_result.get('itag', '140'),
                        'videoId': video_id,
                        'source': api_result.get('source', 'youtube_api_deployment')
                    }
                    # Cache the result
                    stream_cache[cache_key] = {
                        'data': response_data,
                        'timestamp': time.time()
                    }
                    self.send_json_response(response_data)
                    return
            except Exception as e:
                print(f"❌ YouTube API extraction failed in deployment for {video_id}: {e}")
        
        # Try to get a working audio stream URL using yt-dlp first
        print(f"🔧 Attempting yt-dlp extraction for: {video_id}")
        try:
            working_url = self._get_audio_stream_with_ytdlp(video_id, quality)
            if working_url and working_url.startswith('http') and 'googlevideo.com' in working_url:
                print(f"✅ Successfully got audio stream via yt-dlp for: {video_id}")
                response_data = {
                    'url': working_url,
                    'mime': 'audio/mp4',
                    'bitrate': 128,
                    'itag': '140',
                    'videoId': video_id,
                    'source': 'ytdlp_extraction'
                }
                # Cache the result
                stream_cache[cache_key] = {
                    'data': response_data,
                    'timestamp': time.time()
                }
                self.send_json_response(response_data)
                return
            else:
                print(f"❌ yt-dlp returned invalid URL for: {video_id}")
        except Exception as e:
            print(f"❌ yt-dlp extraction failed for {video_id}: {e}")
        
        # Fallback: try to create a working audio URL using alternative methods
        print(f"🔄 Using fallback extraction methods for: {video_id}")
        try:
            fallback_url = self._create_working_audio_url(video_id, quality)
            if fallback_url and fallback_url.startswith('http') and 'googlevideo.com' in fallback_url:
                print(f"✅ Successfully got audio stream via fallback for: {video_id}")
                response_data = {
                    'url': fallback_url,
                    'mime': 'audio/mp4',
                    'bitrate': 128,
                    'itag': '140',
                    'videoId': video_id,
                    'source': 'fallback_extraction'
                }
                # Cache the result
                stream_cache[cache_key] = {
                    'data': response_data,
                    'timestamp': time.time()
                }
                self.send_json_response(response_data)
                return
            else:
                print(f"❌ Fallback extraction returned invalid URL for: {video_id}")
        except Exception as e:
            print(f"❌ Fallback extraction failed for {video_id}: {e}")
        
        # Final fallback: return error instead of invalid URL
        print(f"💥 All extraction methods failed for: {video_id}")
        self.send_json_response({
            'error': 'Unable to extract audio stream for this video',
            'videoId': video_id,
            'suggestion': 'Try a different video or check if the video is available',
            'source': 'all_methods_failed'
        })

    def handle_api_stream_proxy(self, query_string: str) -> None:
        """Stream proxy that serves audio data directly to React Native players"""
        params = urllib.parse.parse_qs(query_string or '')
        video_id = (params.get('videoId', [''])[0] or '').strip()
        quality = (params.get('quality', ['high'])[0] or 'high').strip().lower()
        
        if not video_id:
            self.send_error(400, 'Video ID required')
            return

        print(f"🔄 Stream proxy request for video: {video_id}, quality: {quality}")

        try:
            # Try to get the actual audio stream URL
            working_url = self._create_working_audio_url(video_id, quality)
            
            if working_url and 'googlevideo.com' in working_url:
                # If we have a direct URL, proxy the stream
                print(f"📡 Proxying stream for video: {video_id}")
                self._proxy_audio_stream(working_url)
            else:
                # If we can't get a direct URL, try to use yt-dlp as fallback
                print(f"🔧 Attempting yt-dlp fallback for video: {video_id}")
                self._proxy_audio_with_ytdlp(video_id, quality)
                
        except Exception as e:
            print(f"💥 Stream proxy error for {video_id}: {e}")
            # Return JSON response instead of sending error to avoid broken pipe
            try:
                self.send_json_response({
                    'error': f'Stream proxy error: {str(e)}',
                    'videoId': video_id,
                    'source': 'stream_proxy_error'
                })
            except:
                # If even JSON response fails, just log and continue
                print(f"Failed to send error response: {e}")
                pass

    def _proxy_audio_stream(self, stream_url: str) -> None:
        """Proxy an audio stream from the given URL"""
        try:
            import requests
            
            # Get the audio stream with multiple user agents to handle access issues
            user_agents = [
                'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
                'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
                'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
                'Mozilla/5.0 (iPhone; CPU iPhone OS 14_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/14.0 Mobile/15E148 Safari/604.1',
                'Mozilla/5.0 (Android 10; Mobile; rv:109.0) Gecko/109.0 Firefox/109.0'
            ]
            
            response = None
            for i, user_agent in enumerate(user_agents):
                try:
                    headers = {
                        'User-Agent': user_agent,
                        'Accept': 'audio/*,*/*;q=0.8',
                        'Accept-Encoding': 'identity',
                        'Range': 'bytes=0-',
                        'Referer': 'https://www.youtube.com/',
                        'Origin': 'https://www.youtube.com'
                    }
                    
                    print(f"🔧 Trying user agent {i+1} for stream: {stream_url[:100]}...")
                    response = requests.get(stream_url, headers=headers, stream=True, timeout=30)
                    
                    if response.status_code in [200, 206]:
                        print(f"✅ Successfully connected with user agent {i+1}")
                        break
                    elif response.status_code == 403:
                        print(f"❌ Access denied with user agent {i+1}, trying next...")
                        continue
                    else:
                        print(f"❌ Failed with user agent {i+1}: {response.status_code}")
                        continue
                        
                except Exception as e:
                    print(f"❌ Error with user agent {i+1}: {e}")
                    continue
            
            if not response or response.status_code not in [200, 206]:
                print(f"💥 All user agents failed to access stream")
                self.send_error(403, 'Access denied to audio stream')
                return
            
            # Send headers
            self.send_response(200)
            self.send_header('Content-Type', 'audio/mp4')
            self.send_header('Accept-Ranges', 'bytes')
            self.send_header('Access-Control-Allow-Origin', '*')
            self.send_header('Access-Control-Allow-Methods', 'GET, HEAD, OPTIONS')
            self.send_header('Access-Control-Allow-Headers', 'Range, Content-Range, Content-Length')
            
            if 'content-length' in response.headers:
                self.send_header('Content-Length', response.headers['content-length'])
            if 'content-range' in response.headers:
                self.send_header('Content-Range', response.headers['content-range'])
            
            self.end_headers()
            
            # Stream the audio data with error handling
            try:
                for chunk in response.iter_content(chunk_size=8192):
                    if chunk:
                        self.wfile.write(chunk)
                        self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError) as e:
                print(f"Client disconnected during stream: {e}")
                return
                
        except (BrokenPipeError, ConnectionResetError) as e:
            print(f"Client disconnected: {e}")
            return
        except Exception as e:
            print(f"Proxy stream error: {e}")
            try:
                self.send_error(500, f'Proxy error: {str(e)}')
            except:
                print(f"Failed to send error response: {e}")
                pass

    def _proxy_audio_with_ytdlp(self, video_id: str, quality: str) -> None:
        """Fallback method using yt-dlp to get audio stream with better error handling"""
        try:
            print(f"Attempting yt-dlp fallback for video: {video_id}")
            
            # Try to get audio stream URL using yt-dlp
            stream_url = self._get_audio_stream_with_ytdlp(video_id, quality)
            
            if stream_url:
                print(f"yt-dlp fallback successful for {video_id}")
                # Proxy the stream
                self._proxy_audio_stream(stream_url)
            else:
                print(f"yt-dlp fallback failed for {video_id}")
                # Return a JSON response instead of sending error to avoid broken pipe
                self.send_json_response({
                    'error': 'Audio stream not available',
                    'videoId': video_id,
                    'source': 'ytdlp_fallback_failed'
                })
                
        except Exception as e:
            print(f"yt-dlp fallback error: {e}")
            # Return a JSON response instead of sending error to avoid broken pipe
            try:
                self.send_json_response({
                    'error': f'Fallback error: {str(e)}',
                    'videoId': video_id,
                    'source': 'ytdlp_fallback_error'
                })
            except:
                # If even JSON response fails, just log and continue
                print(f"Failed to send error response: {e}")
                pass

    def _get_audio_stream_with_ytdlp(self, video_id: str, quality: str) -> str:
        """Get audio stream URL using yt-dlp with better error handling"""
        try:
            import subprocess
            import json
            import time
            import shutil
            
            # Check if yt-dlp is available
            yt_dlp_path = shutil.which('yt-dlp')
            python_yt_dlp = shutil.which('python')
            
            if not yt_dlp_path and not python_yt_dlp:
                print(f"❌ yt-dlp not available in deployment environment for {video_id}")
                return None
            
            # Try multiple yt-dlp configurations to handle rate limiting
            yt_dlp_configs = []
            
            # Try direct yt-dlp command first
            if yt_dlp_path:
                yt_dlp_configs.extend([
                    # Direct yt-dlp command
                    [
                        'yt-dlp',
                        '--get-url',
                        '--format', 'bestaudio[ext=m4a]/bestaudio[ext=mp4]/bestaudio',
                        '--no-warnings',
                        '--no-check-certificate',
                        f'https://www.youtube.com/watch?v={video_id}'
                    ],
                    # With user agent
                    [
                        'yt-dlp',
                        '--get-url',
                        '--format', 'bestaudio[ext=m4a]/bestaudio',
                        '--user-agent', 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
                        '--no-warnings',
                        '--no-check-certificate',
                        f'https://www.youtube.com/watch?v={video_id}'
                    ]
                ])
            
            # Try python -m yt_dlp
            if python_yt_dlp:
                yt_dlp_configs.extend([
                    # Python module approach
                    [
                        'python', '-m', 'yt_dlp',
                        '--get-url',
                        '--format', 'bestaudio[ext=m4a]/bestaudio[ext=mp4]/bestaudio',
                        '--no-warnings',
                        '--no-check-certificate',
                        f'https://www.youtube.com/watch?v={video_id}'
                    ],
                    # With user agent and different format
                    [
                        'python', '-m', 'yt_dlp',
                        '--get-url',
                        '--format', 'bestaudio[ext=m4a]/bestaudio',
                        '--user-agent', 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
                        '--no-warnings',
                        '--no-check-certificate',
                        f'https://www.youtube.com/watch?v={video_id}'
                    ],
                    # With cookies and different approach
                    [
                        'python', '-m', 'yt_dlp',
                        '--get-url',
                        '--format', 'bestaudio[ext=m4a]/bestaudio',
                        '--extractor-args', 'youtube:player_client=android',
                        '--no-warnings',
                        '--no-check-certificate',
                        f'https://www.youtube.com/watch?v={video_id}'
                    ],
                    # Alternative with different extractor
                    [
                        'python', '-m', 'yt_dlp',
                        '--get-url',
                        '--format', 'bestaudio',
                        '--extractor-args', 'youtube:player_client=web',
                        '--no-warnings',
                        '--no-check-certificate',
                        f'https://www.youtube.com/watch?v={video_id}'
                    ]
                ])
            
            if not yt_dlp_configs:
                print(f"❌ No yt-dlp configurations available for {video_id}")
                return None
            
            for i, cmd in enumerate(yt_dlp_configs):
                try:
                    if i > 0:
                        # Add delay between attempts
                        time.sleep(2 + i)
                    
                    print(f"🔧 Trying yt-dlp config {i+1} for {video_id}: {' '.join(cmd[:3])}...")
                    result = subprocess.run(cmd, capture_output=True, text=True, timeout=45)
                    
                    if result.returncode == 0 and result.stdout.strip():
                        url = result.stdout.strip()
                        # Validate the URL
                        if url.startswith('http') and ('googlevideo.com' in url or 'youtube.com' in url):
                            print(f"✅ yt-dlp extracted valid URL for {video_id} with config {i+1}: {url[:100]}...")
                            return url
                        else:
                            print(f"❌ yt-dlp config {i+1} returned invalid URL: {url[:100]}...")
                            continue
                    else:
                        error_msg = result.stderr.strip() if result.stderr else "No error message"
                        print(f"❌ yt-dlp config {i+1} failed for {video_id}: {error_msg}")
                        continue
                        
                except subprocess.TimeoutExpired:
                    print(f"⏰ yt-dlp config {i+1} timeout for {video_id}")
                    continue
                except Exception as e:
                    print(f"💥 yt-dlp config {i+1} error for {video_id}: {e}")
                    continue
            
            print(f"💥 All yt-dlp configurations failed for {video_id}")
            return None
                
        except Exception as e:
            print(f"💥 yt-dlp general error for {video_id}: {e}")
            return None

    # All old yt-dlp methods removed to prevent errors

    def _create_working_audio_url(self, video_id: str, quality: str) -> str:
        """Create a working audio URL that the React Native app can handle"""
        try:
            print(f"🔄 Creating working audio URL for: {video_id}")
            
            # Try multiple extraction methods in order of reliability
            extraction_methods = [
                ("YouTube API extraction", self._try_youtube_api_extraction),
                ("Simple YouTube extraction", self._try_simple_youtube_extraction),
                ("Alternative extraction", self._try_alternative_extraction),
            ]
            
            for method_name, method_func in extraction_methods:
                try:
                    print(f"🔧 Trying {method_name} for: {video_id}")
                    result = method_func(video_id, quality)
                    
                    if result and result.get('url'):
                        url = result['url']
                        print(f"📄 {method_name} result for {video_id}: {url[:100]}...")
                        
                        # Validate the URL
                        if (url.startswith('http') and 
                            ('googlevideo.com' in url or 'youtube.com' in url) and
                            not url.endswith('.html') and
                            not 'watch?v=' in url):
                            print(f"✅ Successfully extracted audio URL using {method_name} for: {video_id}")
                            return url
                        else:
                            print(f"❌ Invalid URL from {method_name}: {url[:100]}...")
                            continue
                    else:
                        print(f"❌ {method_name} returned no valid result for: {video_id}")
                        continue
                        
                except Exception as e:
                    print(f"💥 {method_name} failed for {video_id}: {e}")
                    continue
            
            print(f"💥 All extraction methods failed for: {video_id}")
            return None
            
        except Exception as e:
            print(f"💥 Failed to create working audio URL: {e}")
            import traceback
            traceback.print_exc()
            return None

    def _try_simple_youtube_extraction(self, video_id: str, quality: str) -> dict:
        """Simple YouTube extraction using a working approach with better error handling"""
        try:
            print(f"Trying simple YouTube extraction for: {video_id}")
            import requests
            import re
            import json
            import time
            
            # Multiple user agents to rotate through
            user_agents = [
                'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
                'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
                'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
                'Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:109.0) Gecko/20100101 Firefox/121.0',
                'Mozilla/5.0 (Macintosh; Intel Mac OS X 10.15; rv:109.0) Gecko/20100101 Firefox/121.0'
            ]
            
            response = None
            # Try with different user agents and add delays
            for i, user_agent in enumerate(user_agents):
                try:
                    # Use rate limiter to prevent too many requests
                    rate_limiter.wait_if_needed(f"youtube_extraction_{video_id}")
                    
                    if i > 0:
                        # Add delay between requests to avoid rate limiting
                        time.sleep(2 + i)
                    
                    url = f"https://www.youtube.com/watch?v={video_id}"
                    headers = {
                        'User-Agent': user_agent,
                        'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8',
                        'Accept-Language': 'en-US,en;q=0.5',
                        'Accept-Encoding': 'gzip, deflate',
                        'DNT': '1',
                        'Connection': 'keep-alive',
                        'Upgrade-Insecure-Requests': '1',
                        'Cache-Control': 'no-cache',
                        'Pragma': 'no-cache'
                    }
                    
                    response = requests.get(url, headers=headers, timeout=30)
                    if response.status_code == 200:
                        break
                    elif response.status_code == 429:
                        print(f"Rate limited with user agent {i+1}, trying next...")
                        # Wait longer if rate limited
                        time.sleep(10 + i * 2)
                        continue
                    else:
                        print(f"Failed to fetch video page with user agent {i+1}: {response.status_code}")
                        continue
                        
                except requests.exceptions.RequestException as e:
                    print(f"Request failed with user agent {i+1}: {e}")
                    continue
            
            if not response or response.status_code != 200:
                print(f"All user agents failed to fetch video page")
                return None
            
            html_content = response.text
            
            # Look for ytInitialPlayerResponse with more comprehensive patterns
            patterns = [
                r'var ytInitialPlayerResponse = ({.+?});',
                r'ytInitialPlayerResponse\s*=\s*({.+?});',
                r'"playerResponse":\s*({.+?})',
                r'ytInitialPlayerResponse\s*=\s*({.+?})\s*;',
                r'window\["ytInitialPlayerResponse"\]\s*=\s*({.+?});',
            ]
            
            player_response = None
            for pattern in patterns:
                matches = re.findall(pattern, html_content, re.DOTALL)
                for match in matches:
                    try:
                        player_response = json.loads(match)
                        break
                    except json.JSONDecodeError:
                        continue
                if player_response:
                    break
            
            if not player_response:
                print("Could not find player response")
                return None
            
            # Extract streaming data
            streaming_data = player_response.get('streamingData', {})
            if not streaming_data:
                print("No streaming data found")
                return None
            
            # Get adaptive formats
            adaptive_formats = streaming_data.get('adaptiveFormats', [])
            if not adaptive_formats:
                print("No adaptive formats found")
                return None
            
            # Filter for audio-only formats
            audio_formats = []
            for fmt in adaptive_formats:
                mime_type = fmt.get('mimeType', '')
                if mime_type.startswith('audio/'):
                    audio_formats.append(fmt)
            
            if not audio_formats:
                print("No audio formats found")
                return None
            
            # Choose best quality audio format
            quality_map = {'high': 192, 'medium': 128, 'low': 96}
            target_bitrate = quality_map.get(quality, 128)
            
            best_format = None
            best_diff = float('inf')
            
            for fmt in audio_formats:
                bitrate = fmt.get('bitrate', 0)
                diff = abs(bitrate - target_bitrate)
                if diff < best_diff:
                    best_format = fmt
                    best_diff = diff
            
            if not best_format or not best_format.get('url'):
                print("No valid audio format found")
                return None
            
            # Validate the URL before returning
            stream_url = best_format['url']
            if not stream_url.startswith('http') or 'googlevideo.com' not in stream_url:
                print(f"Invalid stream URL format: {stream_url}")
                return None
            
            # Return the stream data
            return {
                'url': stream_url,
                'mime': best_format.get('mimeType', 'audio/mp4'),
                'bitrate': best_format.get('bitrate', 128),
                'itag': best_format.get('itag', '140'),
                'videoId': video_id,
                'source': 'simple_youtube_extraction'
            }
            
        except Exception as e:
            print(f"Simple YouTube extraction failed: {e}")
            return None

    def _try_working_extraction_method(self, video_id: str, quality: str) -> dict:
        """Working extraction method using a different approach"""
        try:
            print(f"Trying working extraction method for: {video_id}")
            
            # This method will use a working YouTube audio extraction service
            # Return None if no valid stream URL found
            print(f"No valid audio stream found in working extraction method for: {video_id}")
            return None
            
        except Exception as e:
            print(f"Working extraction method failed: {e}")
            return None

    def _try_fallback_extraction(self, video_id: str, quality: str) -> dict:
        """Fallback extraction method"""
        try:
            print(f"Trying fallback extraction for: {video_id}")
            
            # Fallback method - return None instead of YouTube page URL
            print(f"No valid audio stream found for: {video_id}")
            return None
            
        except Exception as e:
            print(f"Fallback extraction failed: {e}")
            return None

    def _try_alternative_extraction(self, video_id: str, quality: str) -> dict:
        """Alternative extraction method using different approach"""
        try:
            print(f"Trying alternative extraction for: {video_id}")
            import requests
            import re
            import json
            
            # Use a different approach - try to get the video info directly
            url = f"https://www.youtube.com/watch?v={video_id}"
            headers = {
                'User-Agent': 'Mozilla/5.0 (iPhone; CPU iPhone OS 14_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/14.0 Mobile/15E148 Safari/604.1',
                'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
                'Accept-Language': 'en-US,en;q=0.5',
                'Accept-Encoding': 'gzip, deflate',
                'DNT': '1',
                'Connection': 'keep-alive',
            }
            
            response = requests.get(url, headers=headers, timeout=30)
            if response.status_code != 200:
                print(f"Alternative method failed to fetch video page: {response.status_code}")
                return None
            
            html_content = response.text
            
            # Look for different patterns in the HTML
            patterns = [
                r'"adaptiveFormats":\s*(\[.+?\])',
                r'"formats":\s*(\[.+?\])',
                r'"streamingData":\s*({.+?})',
                r'"url":\s*"([^"]*googlevideo\.com[^"]*)"',
            ]
            
            for pattern in patterns:
                matches = re.findall(pattern, html_content, re.DOTALL)
                for match in matches:
                    try:
                        if pattern.startswith('"url":'):
                            # Direct URL match
                            url_match = match
                            if 'googlevideo.com' in url_match and ('audio' in url_match or 'mime=audio' in url_match):
                                return {
                                    'url': url_match,
                                    'mime': 'audio/mp4',
                                    'bitrate': 128,
                                    'itag': '140',
                                    'videoId': video_id,
                                    'source': 'alternative_direct_url'
                                }
                        elif pattern.startswith('"streamingData"'):
                            # Streaming data object
                            streaming_data = json.loads(match)
                            adaptive_formats = streaming_data.get('adaptiveFormats', [])
                            for fmt in adaptive_formats:
                                if isinstance(fmt, dict):
                                    mime_type = fmt.get('mimeType', '')
                                    if mime_type.startswith('audio/'):
                                        url = fmt.get('url', '')
                                        if url and 'googlevideo.com' in url:
                                            return {
                                                'url': url,
                                                'mime': mime_type,
                                                'bitrate': fmt.get('bitrate', 128),
                                                'itag': fmt.get('itag', '140'),
                                                'videoId': video_id,
                                                'source': 'alternative_streaming_data'
                                            }
                        else:
                            # JSON array match
                            formats = json.loads(match)
                            for fmt in formats:
                                if isinstance(fmt, dict):
                                    mime_type = fmt.get('mimeType', '')
                                    if mime_type.startswith('audio/'):
                                        url = fmt.get('url', '')
                                        if url and 'googlevideo.com' in url:
                                            return {
                                                'url': url,
                                                'mime': mime_type,
                                                'bitrate': fmt.get('bitrate', 128),
                                                'itag': fmt.get('itag', '140'),
                                                'videoId': video_id,
                                                'source': 'alternative_json_extraction'
                                            }
                    except (json.JSONDecodeError, KeyError, TypeError):
                        continue
            
            print(f"Alternative extraction found no valid audio URLs for: {video_id}")
            return None
            
        except Exception as e:
            print(f"Alternative extraction failed: {e}")
            return None

    def _try_youtube_api_extraction(self, video_id: str, quality: str) -> dict:
        """Try to extract audio using YouTube's internal API"""
        try:
            print(f"🔧 Trying YouTube API extraction for: {video_id}")
            import requests
            import json
            
            # Try to get video info using YouTube's internal API
            api_url = "https://www.youtube.com/youtubei/v1/player"
            
            # Use multiple client configurations for better compatibility
            client_configs = [
                {
                    "context": {
                        "client": {
                            "clientName": "WEB",
                            "clientVersion": "2.20231219.01.00"
                        }
                    },
                    "videoId": video_id
                },
                {
                    "context": {
                        "client": {
                            "clientName": "ANDROID",
                            "clientVersion": "19.09.37"
                        }
                    },
                    "videoId": video_id
                },
                {
                    "context": {
                        "client": {
                            "clientName": "IOS",
                            "clientVersion": "19.09.3"
                        }
                    },
                    "videoId": video_id
                }
            ]
            
            headers = {
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
                'Content-Type': 'application/json',
                'Accept': 'application/json',
                'Origin': 'https://www.youtube.com',
                'Referer': 'https://www.youtube.com/',
            }
            
            for i, client_data in enumerate(client_configs):
                try:
                    print(f"🔧 Trying YouTube API config {i+1} for: {video_id}")
                    response = requests.post(api_url, json=client_data, headers=headers, timeout=30)
                    
                    if response.status_code == 200:
                        data = response.json()
                        
                        # Extract streaming data
                        streaming_data = data.get('streamingData', {})
                        if streaming_data:
                            adaptive_formats = streaming_data.get('adaptiveFormats', [])
                            
                            # Filter for audio-only formats
                            audio_formats = []
                            for fmt in adaptive_formats:
                                mime_type = fmt.get('mimeType', '')
                                if mime_type.startswith('audio/'):
                                    audio_formats.append(fmt)
                            
                            if audio_formats:
                                # Choose best quality
                                quality_map = {'high': 192, 'medium': 128, 'low': 96}
                                target_bitrate = quality_map.get(quality, 128)
                                
                                best_format = None
                                best_diff = float('inf')
                                
                                for fmt in audio_formats:
                                    bitrate = fmt.get('bitrate', 0)
                                    diff = abs(bitrate - target_bitrate)
                                    if diff < best_diff:
                                        best_format = fmt
                                        best_diff = diff
                                
                                if best_format and best_format.get('url'):
                                    stream_url = best_format['url']
                                    if stream_url.startswith('http') and 'googlevideo.com' in stream_url:
                                        print(f"✅ YouTube API extraction successful with config {i+1} for: {video_id}")
                                        return {
                                            'url': stream_url,
                                            'mime': best_format.get('mimeType', 'audio/mp4'),
                                            'bitrate': best_format.get('bitrate', 128),
                                            'itag': best_format.get('itag', '140'),
                                            'videoId': video_id,
                                            'source': f'youtube_api_extraction_config_{i+1}'
                                        }
                        else:
                            print(f"❌ No streaming data in YouTube API response for config {i+1}")
                    else:
                        print(f"❌ YouTube API config {i+1} returned status {response.status_code}")
                        
                except Exception as e:
                    print(f"💥 YouTube API config {i+1} failed: {e}")
                    continue
            
            print(f"❌ All YouTube API configurations failed for: {video_id}")
            return None
            
        except Exception as e:
            print(f"💥 YouTube API extraction failed: {e}")
            return None

    def _get_working_stream_url(self, video_id: str, quality: str) -> dict:
        """Get working stream URL using a reliable method that doesn't use yt-dlp"""
        try:
            print(f"Getting working stream URL for: {video_id}")
            import requests
            import re
            import json
            
            # Get the video page with proper headers
            url = f"https://www.youtube.com/watch?v={video_id}"
            headers = {
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
                'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8',
                'Accept-Language': 'en-US,en;q=0.5',
                'Accept-Encoding': 'gzip, deflate',
                'DNT': '1',
                'Connection': 'keep-alive',
                'Upgrade-Insecure-Requests': '1',
            }
            
            response = requests.get(url, headers=headers, timeout=30)
            if response.status_code != 200:
                print(f"Failed to fetch video page: {response.status_code}")
                return None
            
            # Look for player response in the HTML
            html_content = response.text
            
            # Try to find ytInitialPlayerResponse with more comprehensive patterns
            patterns = [
                r'var ytInitialPlayerResponse = ({.+?});',
                r'ytInitialPlayerResponse\s*=\s*({.+?});',
                r'"playerResponse":\s*({.+?})',
                r'ytInitialPlayerResponse\s*=\s*({.+?})\s*;',
                r'window\["ytInitialPlayerResponse"\]\s*=\s*({.+?});',
                r'ytInitialPlayerResponse\s*=\s*({.+?})\s*;',
            ]
            
            player_response = None
            for pattern in patterns:
                match = re.search(pattern, html_content)
                if match:
                    try:
                        player_response = json.loads(match.group(1))
                        break
                    except json.JSONDecodeError:
                        continue
            
            if not player_response:
                print("Could not find player response in HTML")
                return None
            
            # Extract streaming data
            streaming_data = player_response.get('streamingData', {})
            if not streaming_data:
                print("No streaming data found")
                return None
            
            # Get adaptive formats (audio-only)
            adaptive_formats = streaming_data.get('adaptiveFormats', [])
            if not adaptive_formats:
                print("No adaptive formats found")
                return None
            
            # Filter for audio-only formats
            audio_formats = []
            for fmt in adaptive_formats:
                mime_type = fmt.get('mimeType', '')
                if mime_type.startswith('audio/'):
                    audio_formats.append(fmt)
            
            if not audio_formats:
                print("No audio formats found")
                return None
            
            # Choose best quality audio format
            quality_map = {'high': 192, 'medium': 128, 'low': 96}
            target_bitrate = quality_map.get(quality, 128)
            
            best_format = None
            best_diff = float('inf')
            
            for fmt in audio_formats:
                bitrate = fmt.get('bitrate', 0)
                diff = abs(bitrate - target_bitrate)
                if diff < best_diff:
                    best_format = fmt
                    best_diff = diff
            
            if not best_format or not best_format.get('url'):
                print("No valid audio format found")
                return None
            
            # Validate the URL before returning
            stream_url = best_format['url']
            if not stream_url or not stream_url.startswith('http'):
                print("Invalid stream URL format")
                return None
            
            # Return the stream data
            return {
                'url': stream_url,
                'mime': best_format.get('mimeType', 'audio/mp4'),
                'bitrate': best_format.get('bitrate', 128),
                'itag': best_format.get('itag', '140'),
                'videoId': video_id,
                'source': 'working_extraction'
            }
            
        except Exception as e:
            print(f"Working stream extraction failed: {e}")
            # Try alternative approach
            return self._try_alternative_stream_extraction(video_id, quality)

    def _try_alternative_stream_extraction(self, video_id: str, quality: str) -> dict:
        """Alternative stream extraction method using different approach"""
        try:
            print(f"Trying alternative stream extraction for: {video_id}")
            import requests
            import re
            import json
            
            # Try to get the video page with different headers
            url = f"https://www.youtube.com/watch?v={video_id}"
            headers = {
                'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
                'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8',
                'Accept-Language': 'en-US,en;q=0.9',
                'Accept-Encoding': 'gzip, deflate, br',
                'DNT': '1',
                'Connection': 'keep-alive',
                'Upgrade-Insecure-Requests': '1',
            }
            
            response = requests.get(url, headers=headers, timeout=30)
            if response.status_code != 200:
                print(f"Alternative method failed to fetch video page: {response.status_code}")
                return None
            
            html_content = response.text
            
            # Look for different patterns in the HTML
            patterns = [
                r'"adaptiveFormats":\s*(\[.+?\])',
                r'"formats":\s*(\[.+?\])',
                r'"streamingData":\s*({.+?})',
            ]
            
            for pattern in patterns:
                matches = re.findall(pattern, html_content)
                for match in matches:
                    try:
                        data = json.loads(match)
                        if isinstance(data, list):
                            # Look for audio formats
                            audio_formats = [f for f in data if f.get('mimeType', '').startswith('audio/')]
                            if audio_formats:
                                # Choose best quality
                                quality_map = {'high': 192, 'medium': 128, 'low': 96}
                                target_bitrate = quality_map.get(quality, 128)
                                
                                best_format = None
                                best_diff = float('inf')
                                
                                for fmt in audio_formats:
                                    bitrate = fmt.get('bitrate', 0)
                                    diff = abs(bitrate - target_bitrate)
                                    if diff < best_diff:
                                        best_format = fmt
                                        best_diff = diff
                                
                                if best_format and best_format.get('url'):
                                    return {
                                        'url': best_format['url'],
                                        'mime': best_format.get('mimeType', 'audio/mp4'),
                                        'bitrate': best_format.get('bitrate', 128),
                                        'itag': best_format.get('itag', '140'),
                                        'videoId': video_id,
                                        'source': 'alternative_extraction'
                                    }
                        elif isinstance(data, dict) and 'adaptiveFormats' in data:
                            # Handle streamingData format
                            adaptive_formats = data.get('adaptiveFormats', [])
                            audio_formats = [f for f in adaptive_formats if f.get('mimeType', '').startswith('audio/')]
                            if audio_formats:
                                # Choose best quality
                                quality_map = {'high': 192, 'medium': 128, 'low': 96}
                                target_bitrate = quality_map.get(quality, 128)
                                
                                best_format = None
                                best_diff = float('inf')
                                
                                for fmt in audio_formats:
                                    bitrate = fmt.get('bitrate', 0)
                                    diff = abs(bitrate - target_bitrate)
                                    if diff < best_diff:
                                        best_format = fmt
                                        best_diff = diff
                                
                                if best_format and best_format.get('url'):
                                    return {
                                        'url': best_format['url'],
                                        'mime': best_format.get('mimeType', 'audio/mp4'),
                                        'bitrate': best_format.get('bitrate', 128),
                                        'itag': best_format.get('itag', '140'),
                                        'videoId': video_id,
                                        'source': 'alternative_extraction'
                                    }
                    except json.JSONDecodeError:
                        continue
            
            print("Alternative extraction found no valid audio formats")
            return None
            
        except Exception as e:
            print(f"Alternative stream extraction failed: {e}")
            return None

    def handle_api_user_liked(self, query_string: str) -> None:
        """Handle user's liked songs"""
        params = urllib.parse.parse_qs(query_string or '')
        user_id = params.get('userId', [''])[0]
        
        if not user_id:
            self.send_json_response({'error': 'User ID required'}, 400)
            return
        
        try:
            conn = sqlite3.connect(DB_PATH)
            cursor = conn.cursor()
            cursor.execute('''
                SELECT video_id, title, artist, thumbnail, duration 
                FROM liked_songs 
                WHERE user_id = ? 
                ORDER BY created_at DESC
            ''', (user_id,))
            
            results = []
            for row in cursor.fetchall():
                results.append({
                    'videoId': row[0],
                    'title': row[1],
                    'artist': row[2],
                    'thumbnail': row[3],
                    'duration': row[4]
                })
            
            conn.close()
            self.send_json_response({'results': results})
            
        except Exception as e:
            print(f"Error fetching liked songs: {e}")
            self.send_json_response({'error': 'Internal server error'}, 500)

    def handle_api_user_playlists(self, query_string: str) -> None:
        """Handle user's playlists"""
        params = urllib.parse.parse_qs(query_string or '')
        user_id = params.get('userId', [''])[0]
        
        if not user_id:
            self.send_json_response({'error': 'User ID required'}, 400)
            return
        
        try:
            conn = sqlite3.connect(DB_PATH)
            cursor = conn.cursor()
            cursor.execute('''
                SELECT p.id, p.name, p.description, p.created_at,
                       COUNT(ps.id) as song_count
                FROM playlists p
                LEFT JOIN playlist_songs ps ON p.id = ps.playlist_id
                WHERE p.user_id = ?
                GROUP BY p.id, p.name, p.description, p.created_at
                ORDER BY p.created_at DESC
            ''', (user_id,))
            
            results = []
            for row in cursor.fetchall():
                results.append({
                    'id': row[0],
                    'name': row[1],
                    'description': row[2],
                    'createdAt': row[3],
                    'songCount': row[4]
                })
            
            conn.close()
            self.send_json_response({'results': results})
            
        except Exception as e:
            print(f"Error fetching playlists: {e}")
            self.send_json_response({'error': 'Internal server error'}, 500)

    def handle_api_playlist(self, playlist_id: str) -> None:
        """Handle individual playlist data"""
        try:
            # First try to get from local database
            conn = sqlite3.connect(DB_PATH)
            cursor = conn.cursor()
            
            # Get playlist info
            cursor.execute('''
                SELECT p.name, p.description, p.created_at
                FROM playlists p
                WHERE p.id = ?
            ''', (playlist_id,))
            
            playlist_row = cursor.fetchone()
            if playlist_row:
                # Found in local database
                # Get playlist songs
                cursor.execute('''
                    SELECT video_id, title, artist, thumbnail, duration, position
                    FROM playlist_songs
                    WHERE playlist_id = ?
                    ORDER BY position ASC
                ''', (playlist_id,))
                
                songs = []
                for row in cursor.fetchall():
                    songs.append({
                        'videoId': row[0],
                        'title': row[1],
                        'artist': row[2],
                        'thumbnail': row[3],
                        'duration': row[4],
                        'position': row[5]
                    })
                
                conn.close()
                
                playlist_data = {
                    'id': playlist_id,
                    'name': playlist_row[0],
                    'description': playlist_row[1],
                    'createdAt': playlist_row[2],
                    'songs': songs
                }
                
                self.send_json_response({'playlist': playlist_data})
                return
            
            conn.close()
            
            # Not found in local database, try YouTube Music API
            if self.ytmusic:
                try:
                    print(f"Fetching YouTube Music content: {playlist_id}")
                    
                    # Determine content type and use appropriate method
                    playlist_data = None
                    content_type = 'playlist'
                    
                    # Try different methods based on ID patterns
                    # Handle both prefixed and clean IDs for backward compatibility
                    clean_id = playlist_id
                    if playlist_id.startswith('playlist_'):
                        clean_id = playlist_id.replace('playlist_', '')
                    elif playlist_id.startswith('album_'):
                        clean_id = playlist_id.replace('album_', '')
                    elif playlist_id.startswith('artist_'):
                        clean_id = playlist_id.replace('artist_', '')
                    
                    if clean_id.startswith('VL') or playlist_id.startswith('playlist_'):
                        # Regular playlist
                        try:
                            playlist_data = self.ytmusic.get_playlist(clean_id)
                            content_type = 'playlist'
                        except Exception as e:
                            print(f"Error with get_playlist: {e}")
                    
                    if not playlist_data and (clean_id.startswith('MPRE') or playlist_id.startswith('album_')):
                        # Album
                        try:
                            playlist_data = self.ytmusic.get_album(clean_id)
                            content_type = 'album'
                        except Exception as e:
                            print(f"Error with get_album: {e}")
                    
                    if not playlist_data and (clean_id.startswith('UC') or playlist_id.startswith('artist_')):
                        # Artist
                        try:
                            playlist_data = self.ytmusic.get_artist(clean_id)
                            content_type = 'artist'
                        except Exception as e:
                            print(f"Error with get_artist: {e}")
                    
                    if playlist_data:
                        # Convert YouTube Music data to our format
                        songs = []
                        title = 'Unknown Content'
                        description = ''
                        cover = None
                        
                        if content_type == 'playlist':
                            tracks = playlist_data.get('tracks', [])
                            title = playlist_data.get('title', 'Unknown Playlist')
                            description = playlist_data.get('description', '')
                            cover = playlist_data.get('thumbnails', [{}])[-1].get('url') if playlist_data.get('thumbnails') else None
                            
                            for track in tracks:
                                if track and track.get('videoId'):
                                    songs.append({
                                        'videoId': track.get('videoId'),
                                        'title': track.get('title', 'Unknown Title'),
                                        'artist': ', '.join([a.get('name', '') for a in track.get('artists', []) if a.get('name')]) or 'Unknown Artist',
                                        'thumbnail': (track.get('thumbnails') or [{}])[-1].get('url') if track.get('thumbnails') else None,
                                        'duration': track.get('duration'),
                                        'position': len(songs)
                                    })
                        
                        elif content_type == 'album':
                            tracks = playlist_data.get('tracks', [])
                            title = playlist_data.get('title', 'Unknown Album')
                            description = f"Album by {playlist_data.get('artist', {}).get('name', 'Unknown Artist')}"
                            cover = playlist_data.get('thumbnails', [{}])[-1].get('url') if playlist_data.get('thumbnails') else None
                            
                            for track in tracks:
                                if track and track.get('videoId'):
                                    songs.append({
                                        'videoId': track.get('videoId'),
                                        'title': track.get('title', 'Unknown Title'),
                                        'artist': ', '.join([a.get('name', '') for a in track.get('artists', []) if a.get('name')]) or 'Unknown Artist',
                                        'thumbnail': (track.get('thumbnails') or [{}])[-1].get('url') if track.get('thumbnails') else None,
                                        'duration': track.get('duration'),
                                        'position': len(songs)
                                    })
                        
                        elif content_type == 'artist':
                            # For artists, we might not have tracks directly, so return empty for now
                            title = playlist_data.get('name', 'Unknown Artist')
                            description = f"Artist: {title}"
                            cover = playlist_data.get('thumbnails', [{}])[-1].get('url') if playlist_data.get('thumbnails') else None
                        
                        playlist_info = {
                            'id': playlist_id,
                            'name': title,
                            'description': description,
                            'createdAt': None,
                            'songs': songs,
                            'cover': cover,
                            'type': content_type
                        }
                        
                        self.send_json_response({'playlist': playlist_info})
                        return
                        
                except Exception as e:
                    print(f"Error fetching YouTube Music content: {e}")
            
            # Not found anywhere
            self.send_json_response({'error': 'Playlist not found'}, 404)
            
        except Exception as e:
            print(f"Error fetching playlist: {e}")
            self.send_json_response({'error': 'Internal server error'}, 500)

    def handle_api_user_like(self) -> None:
        """Handle liking a song"""
        try:
            content_length = int(self.headers.get('Content-Length', 0))
            post_data = self.rfile.read(content_length)
            data = json.loads(post_data.decode('utf-8'))
            
            user_id = data.get('userId')
            song = data.get('song', {})
            
            if not user_id or not song.get('videoId'):
                self.send_json_response({'error': 'Invalid data'}, 400)
                return
            
            conn = sqlite3.connect(DB_PATH)
            cursor = conn.cursor()
            cursor.execute('''
                INSERT OR IGNORE INTO liked_songs 
                (user_id, video_id, title, artist, thumbnail, duration)
                VALUES (?, ?, ?, ?, ?, ?)
            ''', (user_id, song['videoId'], song['title'], 
                  song.get('artist'), song.get('thumbnail'), song.get('duration')))
            
            conn.commit()
            conn.close()
            
            self.send_json_response({'success': True})
            
        except Exception as e:
            print(f"Error liking song: {e}")
            self.send_json_response({'error': 'Internal server error'}, 500)

    def handle_api_user_unlike(self) -> None:
        """Handle unliking a song"""
        try:
            content_length = int(self.headers.get('Content-Length', 0))
            post_data = self.rfile.read(content_length)
            data = json.loads(post_data.decode('utf-8'))
            
            user_id = data.get('userId')
            video_id = data.get('videoId')
            
            if not user_id or not video_id:
                self.send_json_response({'error': 'Invalid data'}, 400)
                return
            
            conn = sqlite3.connect(DB_PATH)
            cursor = conn.cursor()
            cursor.execute('''
                DELETE FROM liked_songs 
                WHERE user_id = ? AND video_id = ?
            ''', (user_id, video_id))
            
            conn.commit()
            conn.close()
            
            self.send_json_response({'success': True})
            
        except Exception as e:
            print(f"Error unliking song: {e}")
            self.send_json_response({'error': 'Internal server error'}, 500)

    def handle_api_playlist_create(self) -> None:
        """Handle creating a new playlist"""
        try:
            content_length = int(self.headers.get('Content-Length', 0))
            post_data = self.rfile.read(content_length)
            data = json.loads(post_data.decode('utf-8'))
            
            user_id = data.get('userId')
            name = data.get('name')
            description = data.get('description', '')
            playlist_id = data.get('id')
            
            if not user_id or not name or not playlist_id:
                self.send_json_response({'error': 'Invalid data'}, 400)
                return
            
            conn = sqlite3.connect(DB_PATH)
            cursor = conn.cursor()
            cursor.execute('''
                INSERT INTO playlists (id, user_id, name, description)
                VALUES (?, ?, ?, ?)
            ''', (playlist_id, user_id, name, description))
            
            conn.commit()
            conn.close()
            
            self.send_json_response({'success': True, 'playlistId': playlist_id})
            
        except Exception as e:
            print(f"Error creating playlist: {e}")
            self.send_json_response({'error': 'Internal server error'}, 500)

    def handle_api_playlist_add_song(self) -> None:
        """Handle adding a song to a playlist"""
        try:
            content_length = int(self.headers.get('Content-Length', 0))
            post_data = self.rfile.read(content_length)
            data = json.loads(post_data.decode('utf-8'))
            
            playlist_id = data.get('playlistId')
            song = data.get('song', {})
            
            if not playlist_id or not song.get('videoId'):
                self.send_json_response({'error': 'Invalid data'}, 400)
                return
            
            conn = sqlite3.connect(DB_PATH)
            cursor = conn.cursor()
            
            # Get next position
            cursor.execute('''
                SELECT COALESCE(MAX(position), 0) + 1 
                FROM playlist_songs 
                WHERE playlist_id = ?
            ''', (playlist_id,))
            next_position = cursor.fetchone()[0]
            
            # Add song to playlist
            cursor.execute('''
                INSERT INTO playlist_songs 
                (playlist_id, video_id, title, artist, thumbnail, duration, position)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            ''', (playlist_id, song['videoId'], song['title'], 
                  song.get('artist'), song.get('thumbnail'), 
                  song.get('duration'), next_position))
            
            conn.commit()
            conn.close()
            
            self.send_json_response({'success': True})
            
        except Exception as e:
            print(f"Error adding song to playlist: {e}")
            self.send_json_response({'error': 'Internal server error'}, 500)

    def handle_api_playlist_remove_song(self) -> None:
        """Handle removing a song from a playlist"""
        try:
            content_length = int(self.headers.get('Content-Length', 0))
            post_data = self.rfile.read(content_length)
            data = json.loads(post_data.decode('utf-8'))
            
            playlist_id = data.get('playlistId')
            video_id = data.get('videoId')
            
            if not playlist_id or not video_id:
                self.send_json_response({'error': 'Invalid data'}, 400)
                return
            
            conn = sqlite3.connect(DB_PATH)
            cursor = conn.cursor()
            cursor.execute('''
                DELETE FROM playlist_songs 
                WHERE playlist_id = ? AND video_id = ?
            ''', (playlist_id, video_id))
            
            conn.commit()
            conn.close()
            
            self.send_json_response({'success': True})
            
        except Exception as e:
            print(f"Error removing song from playlist: {e}")
            self.send_json_response({'error': 'Internal server error'}, 500)

    def handle_api_lyrics(self, query_string: str) -> None:
        """Handle lyrics requests for songs"""
        params = urllib.parse.parse_qs(query_string or '')
        video_id = params.get('videoId', [''])[0]
        
        if not video_id:
            self.send_json_response({'error': 'Video ID required'}, 400)
            return
        
        # For now, let's provide sample lyrics for testing
        # In a real implementation, you would integrate with a lyrics API
        sample_lyrics = {
            'dQw4w9WgXcQ': {
                'lyrics': '''Never gonna give you up
Never gonna let you down
Never gonna run around and desert you
Never gonna make you cry
Never gonna say goodbye
Never gonna tell a lie and hurt you

We've known each other for so long
Your heart's been aching but you're too shy to say it
Inside we both know what's been going on
We know the game and we're gonna play it

And if you ask me how I'm feeling
Don't tell me you're too blind to see

Never gonna give you up
Never gonna let you down
Never gonna run around and desert you
Never gonna make you cry
Never gonna say goodbye
Never gonna tell a lie and hurt you

We've known each other for so long
Your heart's been aching but you're too shy to say it
Inside we both know what's been going on
We know the game and we're gonna play it

And if you ask me how I'm feeling
Don't tell me you're too blind to see

Never gonna give you up
Never gonna let you down
Never gonna run around and desert you
Never gonna make you cry
Never gonna say goodbye
Never gonna tell a lie and hurt you''',
                'synchronized': [
                    {'text': 'Never gonna give you up', 'startTime': 0.0, 'endTime': 3.0},
                    {'text': 'Never gonna let you down', 'startTime': 3.0, 'endTime': 6.0},
                    {'text': 'Never gonna run around and desert you', 'startTime': 6.0, 'endTime': 10.0},
                    {'text': 'Never gonna make you cry', 'startTime': 10.0, 'endTime': 13.0},
                    {'text': 'Never gonna say goodbye', 'startTime': 13.0, 'endTime': 16.0},
                    {'text': 'Never gonna tell a lie and hurt you', 'startTime': 16.0, 'endTime': 20.0},
                    {'text': '', 'startTime': 20.0, 'endTime': 22.0},  # Pause
                    {'text': 'We\'ve known each other for so long', 'startTime': 22.0, 'endTime': 26.0},
                    {'text': 'Your heart\'s been aching but you\'re too shy to say it', 'startTime': 26.0, 'endTime': 32.0},
                    {'text': 'Inside we both know what\'s been going on', 'startTime': 32.0, 'endTime': 36.0},
                    {'text': 'We know the game and we\'re gonna play it', 'startTime': 36.0, 'endTime': 40.0},
                    {'text': '', 'startTime': 40.0, 'endTime': 42.0},  # Pause
                    {'text': 'And if you ask me how I\'m feeling', 'startTime': 42.0, 'endTime': 46.0},
                    {'text': 'Don\'t tell me you\'re too blind to see', 'startTime': 46.0, 'endTime': 50.0},
                    {'text': '', 'startTime': 50.0, 'endTime': 52.0},  # Pause
                    {'text': 'Never gonna give you up', 'startTime': 52.0, 'endTime': 55.0},
                    {'text': 'Never gonna let you down', 'startTime': 55.0, 'endTime': 58.0},
                    {'text': 'Never gonna run around and desert you', 'startTime': 58.0, 'endTime': 62.0},
                    {'text': 'Never gonna make you cry', 'startTime': 62.0, 'endTime': 65.0},
                    {'text': 'Never gonna say goodbye', 'startTime': 65.0, 'endTime': 68.0},
                    {'text': 'Never gonna tell a lie and hurt you', 'startTime': 68.0, 'endTime': 72.0},
                    {'text': '', 'startTime': 72.0, 'endTime': 74.0},  # Pause
                    {'text': 'We\'ve known each other for so long', 'startTime': 74.0, 'endTime': 78.0},
                    {'text': 'Your heart\'s been aching but you\'re too shy to say it', 'startTime': 78.0, 'endTime': 84.0},
                    {'text': 'Inside we both know what\'s been going on', 'startTime': 84.0, 'endTime': 88.0},
                    {'text': 'We know the game and we\'re gonna play it', 'startTime': 88.0, 'endTime': 92.0},
                    {'text': '', 'startTime': 92.0, 'endTime': 94.0},  # Pause
                    {'text': 'And if you ask me how I\'m feeling', 'startTime': 94.0, 'endTime': 98.0},
                    {'text': 'Don\'t tell me you\'re too blind to see', 'startTime': 98.0, 'endTime': 102.0},
                    {'text': '', 'startTime': 102.0, 'endTime': 104.0},  # Pause
                    {'text': 'Never gonna give you up', 'startTime': 104.0, 'endTime': 107.0},
                    {'text': 'Never gonna let you down', 'startTime': 107.0, 'endTime': 110.0},
                    {'text': 'Never gonna run around and desert you', 'startTime': 110.0, 'endTime': 114.0},
                    {'text': 'Never gonna make you cry', 'startTime': 114.0, 'endTime': 117.0},
                    {'text': 'Never gonna say goodbye', 'startTime': 117.0, 'endTime': 120.0},
                    {'text': 'Never gonna tell a lie and hurt you', 'startTime': 120.0, 'endTime': 124.0},
                    {'text': '', 'startTime': 124.0, 'endTime': 130.0},  # Instrumental break
                    {'text': 'Never gonna give you up', 'startTime': 130.0, 'endTime': 133.0},
                    {'text': 'Never gonna let you down', 'startTime': 133.0, 'endTime': 136.0},
                    {'text': 'Never gonna run around and desert you', 'startTime': 136.0, 'endTime': 140.0},
                    {'text': 'Never gonna make you cry', 'startTime': 140.0, 'endTime': 143.0},
                    {'text': 'Never gonna say goodbye', 'startTime': 143.0, 'endTime': 146.0},
                    {'text': 'Never gonna tell a lie and hurt you', 'startTime': 146.0, 'endTime': 150.0},
                    {'text': '', 'startTime': 150.0, 'endTime': 160.0},  # Extended instrumental
                    {'text': 'Never gonna give you up', 'startTime': 160.0, 'endTime': 163.0},
                    {'text': 'Never gonna let you down', 'startTime': 163.0, 'endTime': 166.0},
                    {'text': 'Never gonna run around and desert you', 'startTime': 166.0, 'endTime': 170.0},
                    {'text': 'Never gonna make you cry', 'startTime': 170.0, 'endTime': 173.0},
                    {'text': 'Never gonna say goodbye', 'startTime': 173.0, 'endTime': 176.0},
                    {'text': 'Never gonna tell a lie and hurt you', 'startTime': 176.0, 'endTime': 180.0},
                    {'text': '', 'startTime': 180.0, 'endTime': 190.0},  # Final instrumental
                    {'text': 'Never gonna give you up', 'startTime': 190.0, 'endTime': 193.0},
                    {'text': 'Never gonna let you down', 'startTime': 193.0, 'endTime': 196.0},
                    {'text': 'Never gonna run around and desert you', 'startTime': 196.0, 'endTime': 200.0},
                    {'text': 'Never gonna make you cry', 'startTime': 200.0, 'endTime': 203.0},
                    {'text': 'Never gonna say goodbye', 'startTime': 203.0, 'endTime': 206.0},
                    {'text': 'Never gonna tell a lie and hurt you', 'startTime': 206.0, 'endTime': 213.0}
                ]
            },
            '9bZkp7q19f0': {
                'lyrics': '''This is the way
I love it
This is the way
I love it

I love it when you call me big poppa
Throw your hands in the air if you's a true player
I love it when you call me big poppa
To the honies getting money playing niggas like dummies''',
                'synchronized': [
                    {'text': 'This is the way', 'startTime': 0.0, 'endTime': 2.0},
                    {'text': 'I love it', 'startTime': 2.0, 'endTime': 4.0},
                    {'text': 'This is the way', 'startTime': 4.0, 'endTime': 6.0},
                    {'text': 'I love it', 'startTime': 6.0, 'endTime': 8.0}
                ]
            },
            'kJQP7kiw5Fk': {
                'lyrics': '''Despacito
Quiero respirar tu cuello despacito
Deja que te diga cosas al oído
Para que te acuerdes si no estás conmigo

Despacito
Quiero desnudarte a besos despacito
Firmar las paredes de tu laberinto
Y hacer de tu cuerpo todo un manuscrito''',
                'synchronized': [
                    {'text': 'Despacito', 'startTime': 0.0, 'endTime': 2.0},
                    {'text': 'Quiero respirar tu cuello despacito', 'startTime': 2.0, 'endTime': 5.0},
                    {'text': 'Deja que te diga cosas al oído', 'startTime': 5.0, 'endTime': 8.0},
                    {'text': 'Para que te acuerdes si no estás conmigo', 'startTime': 8.0, 'endTime': 12.0}
                ]
            }
        }
        
        # Check if we have sample lyrics for this video
        if video_id in sample_lyrics:
            print(f"Using sample lyrics for video ID: {video_id}")
            lyrics_data = sample_lyrics[video_id]
            self.send_json_response({
                'lyrics': lyrics_data['lyrics'],
                'synchronized': lyrics_data['synchronized'],
                'hasLyrics': True
            })
            return
        
        # Try to get real lyrics from YouTube Music API
        if self.ytmusic:
            try:
                # Check available methods
                all_methods = [method for method in dir(self.ytmusic) if not method.startswith('_')]
                lyrics_methods = [method for method in all_methods if 'lyric' in method.lower()]
                print(f"All available methods: {all_methods}")
                print(f"Lyrics-related methods: {lyrics_methods}")
                
                lyrics_data = None
                
                # Use the correct approach: get_watch_playlist to get lyrics ID, then get_lyrics
                lyrics_data = None
                
                try:
                    print("Getting watch playlist to find lyrics ID")
                    watch_data = self.ytmusic.get_watch_playlist(video_id)
                    print(f"Watch data keys: {watch_data.keys() if isinstance(watch_data, dict) else 'Not a dict'}")
                    
                    if isinstance(watch_data, dict) and 'lyrics' in watch_data:
                        lyrics_id = watch_data['lyrics']
                        print(f"Found lyrics ID: {lyrics_id}")
                        
                        if lyrics_id and isinstance(lyrics_id, str) and len(lyrics_id) > 5:
                            print("Getting actual lyrics using lyrics ID")
                            lyrics_data = self.ytmusic.get_lyrics(lyrics_id)
                            print(f"Lyrics data: {lyrics_data}")
                            print(f"Lyrics data type: {type(lyrics_data)}")
                        else:
                            print("Invalid lyrics ID")
                    else:
                        print("No lyrics ID found in watch playlist")
                        
                except Exception as e:
                    print(f"Error getting lyrics: {e}")
                    lyrics_data = None
                
                if lyrics_data:
                    print("Processing lyrics data...")
                    
                    # Handle the standard ytmusicapi lyrics format
                    if isinstance(lyrics_data, dict) and 'lyrics' in lyrics_data:
                        lyrics_text = lyrics_data.get('lyrics', '')
                        source = lyrics_data.get('source', '')
                        
                        print(f"Lyrics text length: {len(lyrics_text)}")
                        print(f"Source: {source}")
                        
                        if lyrics_text and lyrics_text.strip():
                            # Accept lyrics in any language; do not filter by English only
                            self.send_json_response({
                                'lyrics': lyrics_text,
                                'synchronized': [],
                                'hasLyrics': True,
                                'source': source
                            })
                            return
                    
                    # Handle TimedLyrics format (if available)
                    elif hasattr(lyrics_data, 'hasTimestamps') and lyrics_data.get('hasTimestamps'):
                        print("Processing TimedLyrics format")
                        lyrics_lines = lyrics_data.get('lyrics', [])
                        if lyrics_lines:
                            synchronized_lyrics = []
                            lyrics_text_lines = []
                            
                            for line in lyrics_lines:
                                if hasattr(line, 'text') and hasattr(line, 'start_time') and hasattr(line, 'end_time'):
                                    # Convert milliseconds to seconds
                                    start_time = line.start_time / 1000.0
                                    end_time = line.end_time / 1000.0
                                    
                                    synchronized_lyrics.append({
                                        'text': line.text,
                                        'startTime': start_time,
                                        'endTime': end_time
                                    })
                                    lyrics_text_lines.append(line.text)
                            
                            lyrics_text = '\n'.join(lyrics_text_lines)
                            
                            if lyrics_text.strip():
                                # Accept lyrics in any language for timed lyrics as well
                                self.send_json_response({
                                    'lyrics': lyrics_text,
                                    'synchronized': [],
                                    'hasLyrics': True
                                })
                                return
                
                # No lyrics found - return simple message
                print(f"No real lyrics found for video ID: {video_id}")
                
                self.send_json_response({
                    'lyrics': 'No lyrics available for this song',
                    'synchronized': [],
                    'hasLyrics': False
                })
                return
                    
            except Exception as e:
                print(f"Lyrics error: {e}")
                import traceback
                traceback.print_exc()
                # Fallback to no lyrics
                self.send_json_response({
                    'lyrics': f'Unable to load lyrics for this song. Error: {str(e)}',
                    'synchronized': [],
                    'hasLyrics': False
                })
        else:
            # Demo mode - no lyrics available
            self.send_json_response({
                'lyrics': 'Lyrics not available in demo mode.',
                'synchronized': [],
                'hasLyrics': False
            })

    def send_json_response(self, data: Dict[str, Any], status_code: int = 200) -> None:
        """Send JSON response with proper headers. Ignore client-abort errors."""
        payload = json.dumps(data, ensure_ascii=False, indent=2).encode('utf-8')
        try:
            self.send_response(status_code)
            self.send_header('Content-Type', 'application/json; charset=utf-8')
            self.send_header('Content-Length', str(len(payload)))
            self.send_header('Access-Control-Allow-Origin', '*')
            self.send_header('Access-Control-Allow-Methods', 'GET, POST, OPTIONS')
            self.send_header('Access-Control-Allow-Headers', 'Content-Type')
            self.end_headers()
            self.wfile.write(payload)
        except (BrokenPipeError, ConnectionResetError):
            # Client disconnected before we could finish sending the response
            return

    def do_OPTIONS(self):  # noqa: N802 (keep stdlib naming)
        """Handle CORS preflight requests"""
        try:
            self.send_response(200)
            self.send_header('Access-Control-Allow-Origin', '*')
            self.send_header('Access-Control-Allow-Methods', 'GET, POST, OPTIONS')
            self.send_header('Access-Control-Allow-Headers', 'Content-Type')
            self.end_headers()
        except (BrokenPipeError, ConnectionResetError):
            return

if __name__ == '__main__':
    # Initialize database
    init_database()
    
    import argparse
    parser = argparse.ArgumentParser(description='Wave Music Streaming Server')
    parser.add_argument('--port', type=int, default=5000, help='Port to run the server on')
    args = parser.parse_args()
    
    port = int(os.environ.get('PORT', args.port))
    
    print(f"🎵 Wave Music Streaming Server")
    print(f"📡 Server starting on http://0.0.0.0:{port}")
    print(f"🔍 YTMusic API: {'✅ Available' if YTMUSIC_AVAILABLE else '❌ Not available (using demo mode)'}")
    print(f"💾 Database: SQLite ({DB_PATH})")
    print("🚀 Ready to serve music!")
    
    try:
        with ThreadingTCPServer(('0.0.0.0', port), YTMusicRequestHandler) as httpd:
            httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n👋 Server stopped gracefully")
    except Exception as e:
        print(f"❌ Server error: {e}")