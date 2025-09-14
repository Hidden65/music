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
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (playlist_id) REFERENCES playlists (id),
            UNIQUE(playlist_id, video_id)
        )
    ''')
    
    conn.commit()
    conn.close()

class YTMusicRequestHandler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        # Initialize YouTube Music API
        if YTMUSIC_AVAILABLE:
            try:
                self.ytmusic = YTMusic()
            except Exception as e:
                print(f"Failed to initialize YTMusic: {e}")
                self.ytmusic = None
        else:
            self.ytmusic = None
        
        super().__init__(*args, **kwargs)

    def do_GET(self):  # noqa: N802 (keep stdlib naming)
        parsed = urllib.parse.urlsplit(self.path)
        path = parsed.path
        
        # Handle API requests
        if path == '/api/search':
            self.handle_api_search(parsed.query)
            return
        elif path == '/api/search/multi':
            self.handle_api_search_multi(parsed.query)
            return
        elif path == '/api/album':
            self.handle_api_album(parsed.query)
            return
        elif path == '/api/artist':
            self.handle_api_artist(parsed.query)
            return
        elif path == '/api/trending':
            self.handle_api_trending()
            return
        elif path == '/api/recommendations':
            self.handle_api_recommendations(parsed.query)
            return
        elif path == '/api/user/liked':
            self.handle_api_user_liked(parsed.query)
            return
        elif path.startswith('/api/playlist/'):
            playlist_id = path.split('/')[-1]
            self.handle_api_playlist(playlist_id)
            return
        elif path == '/api/user/playlists':
            self.handle_api_user_playlists(parsed.query)
            return
        elif path == '/api/lyrics':
            self.handle_api_lyrics(parsed.query)
            return
        elif path == '/api/youtube/embed':
            self.handle_api_youtube_embed(parsed.query)
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
            self.send_error(404, 'Not Found')

    def handle_api_search(self, query_string: str) -> None:
        """Handle music search requests"""
        params = urllib.parse.parse_qs(query_string or '')
        q = params.get('q', [''])[0]
        
        if not q:
            self.send_json_response({'error': 'Query parameter required'}, 400)
            return
        
        print(f"Search request: {q}")
        
        try:
            if self.ytmusic:
                rate_limiter.wait_if_needed()
                results = self.ytmusic.search(q, filter='songs', limit=20)
            else:
                results = self._demo_search(q)
            
            self.send_json_response({'results': results})
        except Exception as e:
            print(f"Search error: {e}")
            self.send_json_response({'error': 'Search failed'}, 500)

    def handle_api_search_multi(self, query_string: str) -> None:
        """Return songs, albums, artists, playlists, and podcasts for a query"""
        params = urllib.parse.parse_qs(query_string or '')
        q = params.get('q', [''])[0]
        
        if not q:
            self.send_json_response({'error': 'Query parameter required'}, 400)
            return
        
        print(f"Multi search request: {q}")
        
        try:
            if self.ytmusic:
                rate_limiter.wait_if_needed()
                results = self._demo_search_multi(q)
            else:
                results = self._demo_search_multi(q)
            
            self.send_json_response({'results': results})
        except Exception as e:
            print(f"Multi search error: {e}")
            self.send_json_response({'error': 'Multi search failed'}, 500)

    def handle_api_album(self, query_string: str) -> None:
        params = urllib.parse.parse_qs(query_string or '')
        album_id = params.get('albumId', [''])[0]
        
        if not album_id:
            self.send_json_response({'error': 'Album ID required'}, 400)
            return
        
        print(f"Album request: {album_id}")
        
        try:
            if self.ytmusic:
                rate_limiter.wait_if_needed()
                album = self.ytmusic.get_album(album_id)
            else:
                album = {'error': 'Demo mode - album not available'}
            
            self.send_json_response(album)
        except Exception as e:
            print(f"Album error: {e}")
            self.send_json_response({'error': 'Album not found'}, 404)

    def handle_api_artist(self, query_string: str) -> None:
        params = urllib.parse.parse_qs(query_string or '')
        artist_id = params.get('artistId', [''])[0]
        
        if not artist_id:
            self.send_json_response({'error': 'Artist ID required'}, 400)
            return
        
        print(f"Artist request: {artist_id}")
        
        try:
            if self.ytmusic:
                rate_limiter.wait_if_needed()
                artist = self.ytmusic.get_artist(artist_id)
            else:
                artist = {'error': 'Demo mode - artist not available'}
            
            self.send_json_response(artist)
        except Exception as e:
            print(f"Artist error: {e}")
            self.send_json_response({'error': 'Artist not found'}, 404)

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
        try:
            if self.ytmusic:
                rate_limiter.wait_if_needed()
                trending = self.ytmusic.get_charts(country='US')
            else:
                trending = {'error': 'Demo mode - trending not available'}
            
            self.send_json_response(trending)
        except Exception as e:
            print(f"Trending error: {e}")
            self.send_json_response({'error': 'Trending failed'}, 500)

    def handle_api_recommendations(self, query_string: str) -> None:
        """Handle music recommendations based on a song"""
        params = urllib.parse.parse_qs(query_string or '')
        video_id = params.get('videoId', [''])[0]
        
        if not video_id:
            self.send_json_response({'error': 'Video ID required'}, 400)
            return
        
        print(f"Recommendations request for: {video_id}")
        
        try:
            if self.ytmusic:
                rate_limiter.wait_if_needed()
                # Get song details first
                song = self.ytmusic.get_song(video_id)
                if song and 'videoDetails' in song:
                    title = song['videoDetails'].get('title', '')
                    artist = song['videoDetails'].get('author', '')
                    # Search for similar songs
                    search_query = f"{title} {artist}"
                    results = self.ytmusic.search(search_query, filter='songs', limit=10)
                    # Filter out the original song
                    results = [r for r in results if r.get('videoId') != video_id]
                else:
                    results = []
            else:
                results = []
            
            self.send_json_response({'results': results})
        except Exception as e:
            print(f"Recommendations error: {e}")
            self.send_json_response({'error': 'Recommendations failed'}, 500)

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
                SELECT video_id, title, artist, thumbnail, duration, created_at
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
                    'duration': row[4],
                    'createdAt': row[5]
                })
            
            conn.close()
            self.send_json_response({'results': results})
        except Exception as e:
            print(f"User liked error: {e}")
            self.send_json_response({'error': 'Failed to get liked songs'}, 500)

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
                SELECT id, name, description, created_at
                FROM playlists 
                WHERE user_id = ? 
                ORDER BY created_at DESC
            ''', (user_id,))
            
            results = []
            for row in cursor.fetchall():
                results.append({
                    'id': row[0],
                    'name': row[1],
                    'description': row[2],
                    'createdAt': row[3],
                    'songCount': 0  # Will be calculated separately
                })
            
            conn.close()
            self.send_json_response({'results': results})
        except Exception as e:
            print(f"User playlists error: {e}")
            self.send_json_response({'error': 'Failed to get playlists'}, 500)

    def handle_api_playlist(self, playlist_id: str) -> None:
        """Handle individual playlist data"""
        if not playlist_id:
            self.send_json_response({'error': 'Playlist ID required'}, 400)
            return
        
        try:
            conn = sqlite3.connect(DB_PATH)
            cursor = conn.cursor()
            
            # Get playlist info
            cursor.execute('''
                SELECT id, name, description, created_at
                FROM playlists 
                WHERE id = ?
            ''', (playlist_id,))
            
            playlist_row = cursor.fetchone()
            if not playlist_row:
                conn.close()
                self.send_json_response({'error': 'Playlist not found'}, 404)
                return
            
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
            
            playlist_data = {
                'id': playlist_row[0],
                'name': playlist_row[1],
                'description': playlist_row[2],
                'createdAt': playlist_row[3],
                'songs': songs
            }
            
            conn.close()
            self.send_json_response(playlist_data)
        except Exception as e:
            print(f"Playlist error: {e}")
            self.send_json_response({'error': 'Failed to get playlist'}, 500)

    def handle_api_user_like(self) -> None:
        """Handle liking a song"""
        try:
            content_length = int(self.headers['Content-Length'])
            post_data = self.rfile.read(content_length)
            data = json.loads(post_data.decode('utf-8'))
            
            user_id = data.get('userId')
            video_id = data.get('videoId')
            title = data.get('title', '')
            artist = data.get('artist', '')
            thumbnail = data.get('thumbnail', '')
            duration = data.get('duration', '')
            
            if not user_id or not video_id:
                self.send_json_response({'error': 'User ID and Video ID required'}, 400)
                return
            
            conn = sqlite3.connect(DB_PATH)
            cursor = conn.cursor()
            
            # Insert or update liked song
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

    def handle_api_user_unlike(self) -> None:
        """Handle unliking a song"""
        try:
            content_length = int(self.headers['Content-Length'])
            post_data = self.rfile.read(content_length)
            data = json.loads(post_data.decode('utf-8'))
            
            user_id = data.get('userId')
            video_id = data.get('videoId')
            
            if not user_id or not video_id:
                self.send_json_response({'error': 'User ID and Video ID required'}, 400)
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
            print(f"Unlike error: {e}")
            self.send_json_response({'error': 'Failed to unlike song'}, 500)

    def handle_api_playlist_create(self) -> None:
        """Handle creating a new playlist"""
        try:
            content_length = int(self.headers['Content-Length'])
            post_data = self.rfile.read(content_length)
            data = json.loads(post_data.decode('utf-8'))
            
            user_id = data.get('userId')
            name = data.get('name')
            description = data.get('description', '')
            
            if not user_id or not name:
                self.send_json_response({'error': 'User ID and name required'}, 400)
                return
            
            import uuid
            playlist_id = str(uuid.uuid4())
            
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
            print(f"Create playlist error: {e}")
            self.send_json_response({'error': 'Failed to create playlist'}, 500)

    def handle_api_playlist_add_song(self) -> None:
        """Handle adding a song to a playlist"""
        try:
            content_length = int(self.headers['Content-Length'])
            post_data = self.rfile.read(content_length)
            data = json.loads(post_data.decode('utf-8'))
            
            playlist_id = data.get('playlistId')
            video_id = data.get('videoId')
            title = data.get('title', '')
            artist = data.get('artist', '')
            thumbnail = data.get('thumbnail', '')
            duration = data.get('duration', '')
            
            if not playlist_id or not video_id:
                self.send_json_response({'error': 'Playlist ID and Video ID required'}, 400)
                return
            
            conn = sqlite3.connect(DB_PATH)
            cursor = conn.cursor()
            
            # Get next position
            cursor.execute('''
                SELECT MAX(position) FROM playlist_songs WHERE playlist_id = ?
            ''', (playlist_id,))
            result = cursor.fetchone()
            next_position = (result[0] or 0) + 1
            
            cursor.execute('''
                INSERT OR REPLACE INTO playlist_songs 
                (playlist_id, video_id, title, artist, thumbnail, duration, position)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            ''', (playlist_id, video_id, title, artist, thumbnail, duration, next_position))
            
            conn.commit()
            conn.close()
            
            self.send_json_response({'success': True})
        except Exception as e:
            print(f"Add song error: {e}")
            self.send_json_response({'error': 'Failed to add song to playlist'}, 500)

    def handle_api_playlist_remove_song(self) -> None:
        """Handle removing a song from a playlist"""
        try:
            content_length = int(self.headers['Content-Length'])
            post_data = self.rfile.read(content_length)
            data = json.loads(post_data.decode('utf-8'))
            
            playlist_id = data.get('playlistId')
            video_id = data.get('videoId')
            
            if not playlist_id or not video_id:
                self.send_json_response({'error': 'Playlist ID and Video ID required'}, 400)
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
            print(f"Remove song error: {e}")
            self.send_json_response({'error': 'Failed to remove song from playlist'}, 500)

    def handle_api_lyrics(self, query_string: str) -> None:
        """Handle lyrics requests for songs"""
        params = urllib.parse.parse_qs(query_string or '')
        video_id = params.get('videoId', [''])[0]
        
        if not video_id:
            self.send_json_response({'error': 'Video ID required'}, 400)
            return
        
        print(f"Lyrics request for: {video_id}")
        
        try:
            if self.ytmusic:
                rate_limiter.wait_if_needed()
                lyrics = self.ytmusic.get_lyrics(video_id)
            else:
                lyrics = {'error': 'Demo mode - lyrics not available'}
            
            self.send_json_response(lyrics)
        except Exception as e:
            print(f"Lyrics error: {e}")
            self.send_json_response({'error': 'Lyrics not found'}, 404)

    def send_json_response(self, data: Dict[str, Any], status_code: int = 200) -> None:
        """Send JSON response with proper headers. Ignore client-abort errors."""
        try:
            self.send_response(status_code)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Access-Control-Allow-Origin', '*')
            self.send_header('Access-Control-Allow-Methods', 'GET, POST, OPTIONS')
            self.send_header('Access-Control-Allow-Headers', 'Content-Type')
            self.end_headers()
            
            json_data = json.dumps(data, ensure_ascii=False)
            self.wfile.write(json_data.encode('utf-8'))
        except (BrokenPipeError, ConnectionResetError):
            # Client disconnected, ignore
            pass

    def do_OPTIONS(self):  # noqa: N802 (keep stdlib naming)
        """Handle CORS preflight requests"""
        self.send_response(200)
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Methods', 'GET, POST, OPTIONS')
        self.send_header('Access-Control-Allow-Headers', 'Content-Type')
        self.end_headers()

def main():
    """Main function to start the server"""
    # Initialize database
    init_database()
    
    # Create static directory if it doesn't exist
    static_dir = os.path.join(ROOT_DIR, 'static')
    if not os.path.exists(static_dir):
        os.makedirs(static_dir)
    
    # Copy index.html to static directory if it exists in root
    index_src = os.path.join(ROOT_DIR, 'index.html')
    index_dst = os.path.join(static_dir, 'index.html')
    if os.path.exists(index_src) and not os.path.exists(index_dst):
        import shutil
        shutil.copy2(index_src, index_dst)
    
    # Start server
    port = int(os.environ.get('PORT', 5000))
    server = ThreadingTCPServer(('0.0.0.0', port), YTMusicRequestHandler)
    
    print(f"🎵 Wave Music Server starting on port {port}")
    print(f"📁 Serving static files from: {static_dir}")
    print(f"🗄️  Database: {DB_PATH}")
    print(f"🌐 Access at: http://localhost:{port}")
    
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n🛑 Server stopped")
        server.shutdown()

if __name__ == '__main__':
    main()
