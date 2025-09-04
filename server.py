import os
import json
import posixpath
import urllib.parse
from typing import List, Dict, Any, Optional
from http.server import SimpleHTTPRequestHandler
from socketserver import ThreadingTCPServer
import time
import sqlite3

try:
    from ytmusicapi import YTMusic
    YTMUSIC_AVAILABLE = True
except ImportError:
    YTMUSIC_AVAILABLE = False
    print("Warning: ytmusicapi not installed. Using demo mode.")

ROOT_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(ROOT_DIR, 'wave_music.db')

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

def get_demo_results(query: str) -> List[Dict[str, Any]]:
    """Return demo search results when YTMusic is not available"""
    demo_songs = [
        {
            'videoId': f'demo_{i}_{int(time.time())}',
            'title': f'Demo Song {i} - {query}',
            'artist': f'Demo Artist {i}',
            'duration': f'{2 + i}:{30 + (i * 10):02d}',
            'thumbnail': f'https://via.placeholder.com/120x120/{["6366f1", "8b5cf6", "ec4899", "10b981", "f59e0b"][i % 5]}/ffffff?text=Song+{i}'
        }
        for i in range(1, 11)
    ]
    return demo_songs

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
        
        # Serve static files
        if path == '/':
            self.path = '/index.html'
        elif path == '/static/app.js':
            # Serve the original app.js or a simplified version
            self.path = '/app.js'
        
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
                    results = get_demo_results(q)
            else:
                results = get_demo_results(q)
        
        # Filter out results without video IDs
        results = [r for r in results if r.get('videoId')][:20]
        
        self.send_json_response({'results': results})

    def handle_api_trending(self) -> None:
        """Handle trending music requests"""
        if self.ytmusic:
            try:
                # Get trending music
                trending = self.ytmusic.get_charts()
                if trending and 'songs' in trending:
                    results = [map_song_result(song) for song in trending['songs'][:20]]
                else:
                    results = []
            except Exception as e:
                print(f"Trending error: {e}")
                results = get_demo_results("trending")
        else:
            results = get_demo_results("trending")
        
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
                results = get_demo_results("recommendations")
        else:
            results = get_demo_results("recommendations")
        
        self.send_json_response({'results': results})

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
            conn = sqlite3.connect(DB_PATH)
            cursor = conn.cursor()
            
            # Get playlist info
            cursor.execute('''
                SELECT p.name, p.description, p.created_at
                FROM playlists p
                WHERE p.id = ?
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
            
            conn.close()
            
            playlist_data = {
                'id': playlist_id,
                'name': playlist_row[0],
                'description': playlist_row[1],
                'createdAt': playlist_row[2],
                'songs': songs
            }
            
            self.send_json_response({'playlist': playlist_data})
            
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

    def send_json_response(self, data: Dict[str, Any], status_code: int = 200) -> None:
        """Send JSON response with proper headers"""
        payload = json.dumps(data, ensure_ascii=False, indent=2).encode('utf-8')
        
        self.send_response(status_code)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(payload)))
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Methods', 'GET, POST, OPTIONS')
        self.send_header('Access-Control-Allow-Headers', 'Content-Type')
        self.end_headers()
        self.wfile.write(payload)

    def do_OPTIONS(self):  # noqa: N802 (keep stdlib naming)
        """Handle CORS preflight requests"""
        self.send_response(200)
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Methods', 'GET, POST, OPTIONS')
        self.send_header('Access-Control-Allow-Headers', 'Content-Type')
        self.end_headers()

if __name__ == '__main__':
    # Initialize database
    init_database()
    
    port = int(os.environ.get('PORT', '5000'))
    
    print(f"🎵 Wave Music Streaming Server")
    print(f"📡 Server starting on http://localhost:{port}")
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