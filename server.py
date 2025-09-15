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
            position INTEGER DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (playlist_id) REFERENCES playlists (id),
            UNIQUE(playlist_id, video_id)
        )
    ''')
    
    conn.commit()
    conn.close()

def map_song_result(item: Dict[str, Any]) -> Dict[str, Any]:
    """Map YouTube Music API result to our song format"""
    return {
        'videoId': item.get('videoId', ''),
        'title': item.get('title', 'Unknown Title'),
        'artist': item.get('artists', [{}])[0].get('name', 'Unknown Artist') if item.get('artists') else 'Unknown Artist',
        'thumbnail': item.get('thumbnails', [{}])[-1].get('url', '') if item.get('thumbnails') else '',
        'duration': item.get('duration', '0:00')
    }

def get_demo_results(query: str) -> List[Dict[str, Any]]:
    """Return demo search results when API is not available"""
    return [
        {
            'videoId': 'dQw4w9WgXcQ',
            'title': f'Demo Result for "{query}"',
            'artist': 'Demo Artist',
            'thumbnail': 'https://via.placeholder.com/300x200?text=Demo',
            'duration': '3:32'
        }
    ]

def fetch_remote_json(path: str) -> Optional[Dict[str, Any]]:
    """Fetch JSON from remote backend if configured"""
    if not REMOTE_BASE_URL:
        return None
    
    try:
        url = f"{REMOTE_BASE_URL}{path}"
        with urllib.request.urlopen(url, timeout=10) as response:
            return json.loads(response.read().decode())
    except Exception as e:
        print(f"Remote fetch error: {e}")
        return None


class YTMusicRequestHandler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        # Initialize YouTube Music API
        if YTMUSIC_AVAILABLE:
            try:
                self.ytmusic = YTMusic()
                print("✅ YouTube Music API initialized successfully")
            except Exception as e:
                print(f"❌ Failed to initialize YouTube Music API: {e}")
                self.ytmusic = None
        else:
            self.ytmusic = None
        
        super().__init__(*args, **kwargs)

    def do_GET(self):  # noqa: N802 (keep stdlib naming)
        """Handle GET requests"""
        try:
            # Parse the path
            parsed_path = urllib.parse.urlparse(self.path)
            path = parsed_path.path
            query_string = parsed_path.query

            # Route API requests
            if path == '/api/search':
                self.handle_api_search(query_string)
            elif path.startswith('/api/playlist/'):
                playlist_id = path.split('/')[-1]
                self.handle_api_playlist(playlist_id)
            elif path == '/api/stream':
                self.handle_api_stream(query_string)
            elif path == '/api/youtube-embed':
                self.handle_api_youtube_embed(query_string)
            elif path == '/api/recommendations':
                self.handle_api_recommendations(query_string)
            else:
                # Serve static files
                self.serve_static_file(path)
        except Exception as e:
            print(f"Request handling error: {e}")
            self.send_error(500, "Internal Server Error")

    def do_POST(self):  # noqa: N802 (keep stdlib naming)
        """Handle POST requests"""
        try:
            parsed_path = urllib.parse.urlparse(self.path)
            path = parsed_path.path

            if path == '/api/like':
                self.handle_api_like()
            elif path == '/api/unlike':
                self.handle_api_unlike()
            elif path == '/api/playlist/create':
                self.handle_api_playlist_create()
            elif path == '/api/playlist/add':
                self.handle_api_playlist_add()
            elif path == '/api/playlist/remove':
                self.handle_api_playlist_remove()
            else:
                self.send_error(404, "Not Found")
        except Exception as e:
            print(f"POST request handling error: {e}")
            self.send_error(500, "Internal Server Error")

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

    def handle_api_playlist(self, playlist_id: str) -> None:
        """Handle playlist requests"""
        if not playlist_id:
            self.send_error(400, 'Playlist ID required')
            return

        print(f"🎵 Playlist request for ID: {playlist_id}")

        if self.ytmusic:
            try:
                # Get playlist from YouTube Music
                playlist = self.ytmusic.get_playlist(playlist_id)
                
                if playlist:
                    songs = []
                    for track in playlist.get('tracks', []):
                        if track.get('videoId'):
                            songs.append({
                                'videoId': track['videoId'],
                                'title': track.get('title', 'Unknown Title'),
                                'artist': track.get('artists', [{}])[0].get('name', 'Unknown Artist') if track.get('artists') else 'Unknown Artist',
                                'thumbnail': track.get('thumbnails', [{}])[-1].get('url', '') if track.get('thumbnails') else '',
                                'duration': track.get('duration', '0:00')
                            })
                    
                    response_data = {
                        'playlist': {
                            'id': playlist_id,
                            'title': playlist.get('title', 'Unknown Playlist'),
                            'description': playlist.get('description', ''),
                            'songs': songs
                        }
                    }
                    self.send_json_response(response_data)
                    return
                    
            except Exception as e:
                print(f"Playlist error: {e}")
        
        # Fallback to hosted backend
        remote = fetch_remote_json(f'/api/playlist/{playlist_id}')
        if remote and remote.get('playlist'):
            self.send_json_response(remote)
        else:
            self.send_json_response({'error': 'Playlist not found'}, 404)

    def handle_api_stream(self, query_string: str) -> None:
        """Return YouTube Iframe API configuration for a given YouTube video ID.

        Response: { embedUrl: string, videoId: string, playerConfig: object }
        """
        params = urllib.parse.parse_qs(query_string or '')
        video_id = (params.get('videoId', [''])[0] or '').strip()
        quality = (params.get('quality', ['high'])[0] or 'high').strip().lower()
        if not video_id:
            self.send_json_response({'error': 'Video ID required'}, 400)
            return

        print(f"🎵 YouTube Iframe API request for video: {video_id}, quality: {quality}")

        # Check cache first
        cache_key = f"{video_id}_{quality}"
        if cache_key in stream_cache:
            cached_data = stream_cache[cache_key]
            if time.time() - cached_data['timestamp'] < CACHE_DURATION:
                print(f"📦 Using cached YouTube Iframe API config for: {video_id}")
                self.send_json_response(cached_data['data'])
                return
            else:
                # Remove expired cache entry
                del stream_cache[cache_key]

        # Use YouTube Iframe API for audio playback
        print(f"🔧 Using YouTube Iframe API for: {video_id}")
        try:
            embed_config = self._get_youtube_embed_config(video_id, quality)
            if embed_config:
                print(f"✅ Successfully got YouTube Iframe API config for: {video_id}")
                response_data = {
                    'embedUrl': embed_config['embedUrl'],
                    'playerConfig': embed_config['playerConfig'],
                    'videoId': video_id,
                    'source': 'youtube_iframe_api'
                }
                # Cache the result
                stream_cache[cache_key] = {
                    'data': response_data,
                    'timestamp': time.time()
                }
                self.send_json_response(response_data)
                return
            else:
                print(f"❌ Failed to get YouTube Iframe API config for: {video_id}")
        except Exception as e:
            print(f"❌ YouTube Iframe API config failed for {video_id}: {e}")

        # Fallback: return error if no method works
        self.send_json_response({
            'error': 'Unable to get YouTube Iframe API configuration for this video',
            'videoId': video_id,
            'source': 'all_methods_failed'
        })

    def handle_api_youtube_embed(self, query_string: str) -> None:
        """Return YouTube iframe embed URL for a given YouTube video ID.

        Response: { embedUrl: string, videoId: string }
        """
        params = urllib.parse.parse_qs(query_string or '')
        video_id = (params.get('videoId', [''])[0] or '').strip()
        
        if not video_id:
            self.send_json_response({'error': 'Video ID required'}, 400)
            return

        print(f"🎵 YouTube embed request for video: {video_id}")

        # Create YouTube iframe embed URL
        embed_url = f"https://www.youtube.com/embed/{video_id}?autoplay=1&controls=1&modestbranding=1&rel=0&showinfo=0&iv_load_policy=3&fs=1&cc_load_policy=0&start=0&end=&wmode=opaque&enablejsapi=1&origin=https://www.youtube.com"
        
        response_data = {
            'embedUrl': embed_url,
            'videoId': video_id,
            'source': 'youtube_iframe_api'
        }
        
        self.send_json_response(response_data)

    def handle_api_recommendations(self, query_string: str) -> None:
        """Handle recommendations requests"""
        params = urllib.parse.parse_qs(query_string or '')
        video_id = (params.get('videoId', [''])[0] or '').strip()
        
        if video_id and self.ytmusic:
            try:
                # Get related videos
                related = self.ytmusic.get_watch_playlist(video_id)
                if related and related.get('tracks'):
                    results = []
                    for track in related['tracks'][:10]:  # Limit to 10 recommendations
                        if track.get('videoId'):
                            results.append(map_song_result(track))
                    self.send_json_response({'results': results})
                    return
            except Exception as e:
                print(f"Recommendations error: {e}")

        # Fallback
        self.send_json_response({'results': []})

    def _get_youtube_embed_config(self, video_id: str, quality: str) -> dict:
        """Get YouTube Iframe API configuration for a video"""
        try:
            print(f"🔧 Getting YouTube Iframe API config for: {video_id}")
            
            # Create optimized embed URL for audio playback
            embed_url = f"https://www.youtube.com/embed/{video_id}"
            
            # Player configuration optimized for hidden audio playback
            player_config = {
                'height': '0',
                'width': '0',
                'playerVars': {
                    'autoplay': 0,
                    'controls': 0,
                    'disablekb': 1,
                    'enablejsapi': 1,
                    'fs': 0,
                    'iv_load_policy': 3,
                    'modestbranding': 1,
                    'playsinline': 1,
                    'rel': 0,
                    'showinfo': 0,
                    'cc_load_policy': 0,
                    'start': 0,
                    'wmode': 'opaque',
                    'origin': 'https://www.youtube.com'
                }
            }
            
            return {
                'embedUrl': embed_url,
                'playerConfig': player_config,
                'videoId': video_id
            }
            
        except Exception as e:
            print(f"💥 YouTube Iframe API config error for {video_id}: {e}")
            return None

    def handle_api_like(self) -> None:
        """Handle like song requests"""
        try:
            content_length = int(self.headers.get('Content-Length', 0))
            post_data = self.rfile.read(content_length)
            data = json.loads(post_data.decode('utf-8'))
            
            user_id = data.get('userId')
            video_id = data.get('videoId')
            title = data.get('title', '')
            artist = data.get('artist', '')
            thumbnail = data.get('thumbnail', '')
            duration = data.get('duration', '')
            
            if not user_id or not video_id:
                self.send_json_response({'error': 'Missing required fields'}, 400)
                return
            
            # Store in database
            conn = sqlite3.connect(DB_PATH)
            cursor = conn.cursor()
            
            cursor.execute('''
                INSERT OR REPLACE INTO liked_songs 
                (user_id, video_id, title, artist, thumbnail, duration)
                VALUES (?, ?, ?, ?, ?, ?)
            ''', (user_id, video_id, title, artist, thumbnail, duration))
            
            conn.commit()
            conn.close()
            
            self.send_json_response({'success': True})
            
        except Exception as e:
            print(f"Like error: {e}")
            self.send_json_response({'error': 'Failed to like song'}, 500)

    def handle_api_unlike(self) -> None:
        """Handle unlike song requests"""
        try:
            content_length = int(self.headers.get('Content-Length', 0))
            post_data = self.rfile.read(content_length)
            data = json.loads(post_data.decode('utf-8'))
            
            user_id = data.get('userId')
            video_id = data.get('videoId')
            
            if not user_id or not video_id:
                self.send_json_response({'error': 'Missing required fields'}, 400)
                return
            
            # Remove from database
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
            print(f"Unlike error: {e}")
            self.send_json_response({'error': 'Failed to unlike song'}, 500)

    def handle_api_playlist_create(self) -> None:
        """Handle playlist creation requests"""
        try:
            content_length = int(self.headers.get('Content-Length', 0))
            post_data = self.rfile.read(content_length)
            data = json.loads(post_data.decode('utf-8'))
            
            user_id = data.get('userId')
            name = data.get('name')
            description = data.get('description', '')
            
            if not user_id or not name:
                self.send_json_response({'error': 'Missing required fields'}, 400)
                return
            
            # Generate playlist ID
            playlist_id = f"PL_{user_id}_{int(time.time())}"
            
            # Store in database
            conn = sqlite3.connect(DB_PATH)
            cursor = conn.cursor()
            
            cursor.execute('''
                INSERT INTO playlists (id, user_id, name, description)
                VALUES (?, ?, ?, ?)
            ''', (playlist_id, user_id, name, description))
            
            conn.commit()
            conn.close()
            
            self.send_json_response({
                'success': True,
                'playlistId': playlist_id
            })
            
        except Exception as e:
            print(f"Playlist creation error: {e}")
            self.send_json_response({'error': 'Failed to create playlist'}, 500)

    def handle_api_playlist_add(self) -> None:
        """Handle adding songs to playlist requests"""
        try:
            content_length = int(self.headers.get('Content-Length', 0))
            post_data = self.rfile.read(content_length)
            data = json.loads(post_data.decode('utf-8'))
            
            playlist_id = data.get('playlistId')
            video_id = data.get('videoId')
            title = data.get('title', '')
            artist = data.get('artist', '')
            thumbnail = data.get('thumbnail', '')
            duration = data.get('duration', '')
            
            if not playlist_id or not video_id:
                self.send_json_response({'error': 'Missing required fields'}, 400)
                return
            
            # Store in database
            conn = sqlite3.connect(DB_PATH)
            cursor = conn.cursor()
            
            cursor.execute('''
                INSERT OR REPLACE INTO playlist_songs 
                (playlist_id, video_id, title, artist, thumbnail, duration)
                VALUES (?, ?, ?, ?, ?, ?)
            ''', (playlist_id, video_id, title, artist, thumbnail, duration))
            
            conn.commit()
            conn.close()
            
            self.send_json_response({'success': True})
            
        except Exception as e:
            print(f"Playlist add error: {e}")
            self.send_json_response({'error': 'Failed to add song to playlist'}, 500)

    def handle_api_playlist_remove(self) -> None:
        """Handle removing songs from playlist requests"""
        try:
            content_length = int(self.headers.get('Content-Length', 0))
            post_data = self.rfile.read(content_length)
            data = json.loads(post_data.decode('utf-8'))
            
            playlist_id = data.get('playlistId')
            video_id = data.get('videoId')
            
            if not playlist_id or not video_id:
                self.send_json_response({'error': 'Missing required fields'}, 400)
                return
            
            # Remove from database
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
            print(f"Playlist remove error: {e}")
            self.send_json_response({'error': 'Failed to remove song from playlist'}, 500)

    def serve_static_file(self, path: str) -> None:
        """Serve static files"""
        if path == '/':
            path = '/index.html'
        
        # Security: prevent directory traversal
        if '..' in path or path.startswith('/'):
            path = path.lstrip('/')
        
        file_path = os.path.join(ROOT_DIR, path)
        
        if os.path.isfile(file_path):
            try:
                with open(file_path, 'rb') as f:
                    content = f.read()
                
                # Set appropriate content type
                if path.endswith('.html'):
                    self.send_response(200)
                    self.send_header('Content-Type', 'text/html; charset=utf-8')
                elif path.endswith('.js'):
                    self.send_response(200)
                    self.send_header('Content-Type', 'application/javascript')
                elif path.endswith('.css'):
                    self.send_response(200)
                    self.send_header('Content-Type', 'text/css')
                elif path.endswith('.json'):
                    self.send_response(200)
                    self.send_header('Content-Type', 'application/json')
                else:
                    self.send_response(200)
                    self.send_header('Content-Type', 'application/octet-stream')
                
                self.send_header('Content-Length', str(len(content)))
                self.end_headers()
                self.wfile.write(content)
            except Exception as e:
                print(f"Error serving file {path}: {e}")
                self.send_error(500, "Internal Server Error")
        else:
            self.send_error(404, "File Not Found")

    def send_json_response(self, data: Dict[str, Any], status_code: int = 200) -> None:
        """Send JSON response"""
        try:
            json_data = json.dumps(data, ensure_ascii=False)
            self.send_response(status_code)
            self.send_header('Content-Type', 'application/json; charset=utf-8')
            self.send_header('Access-Control-Allow-Origin', '*')
            self.send_header('Access-Control-Allow-Methods', 'GET, POST, OPTIONS')
            self.send_header('Access-Control-Allow-Headers', 'Content-Type')
            self.send_header('Content-Length', str(len(json_data.encode('utf-8'))))
            self.end_headers()
            self.wfile.write(json_data.encode('utf-8'))
        except Exception as e:
            print(f"Error sending JSON response: {e}")
            try:
                self.send_error(500, "Internal Server Error")
            except:
                pass


def main():
    """Main server function"""
    # Initialize database
    init_database()
    
    # Server configuration
    PORT = int(os.environ.get('PORT', 5000))
    HOST = os.environ.get('HOST', '0.0.0.0')
    
    print(f"🚀 Starting Wave Music Server on {HOST}:{PORT}")
    print(f"📁 Serving files from: {ROOT_DIR}")
    print(f"🎵 YouTube Music API: {'✅ Available' if YTMUSIC_AVAILABLE else '❌ Not Available'}")
    print(f"🌐 Remote backend: {'✅ Configured' if REMOTE_BASE_URL else '❌ Not Configured'}")
    
    # Create and start server
    with ThreadingTCPServer((HOST, PORT), YTMusicRequestHandler) as httpd:
        print(f"✅ Server running at http://{HOST}:{PORT}")
        print("Press Ctrl+C to stop the server")
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            print("\n🛑 Server stopped by user")
        except Exception as e:
            print(f"❌ Server error: {e}")


if __name__ == '__main__':
    main()
