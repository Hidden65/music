import os
import json
import posixpath
import urllib.parse
from typing import List, Dict, Any, Optional
from http.server import SimpleHTTPRequestHandler
from socketserver import ThreadingTCPServer
import time
import firebase_admin
from firebase_admin import credentials, firestore

try:
    from ytmusicapi import YTMusic
    YTMUSIC_AVAILABLE = True
except ImportError:
    YTMUSIC_AVAILABLE = False
    print("Warning: ytmusicapi not installed. Using demo mode.")

ROOT_DIR = os.path.dirname(os.path.abspath(__file__))

def init_firebase():
    """Initialize Firebase Admin SDK"""
    try:
        # Use service account key from environment variable or file
        cred_path = os.environ.get('FIREBASE_SERVICE_ACCOUNT_KEY')
        if cred_path and os.path.exists(cred_path):
            cred = credentials.Certificate(cred_path)
        else:
            # Fallback: use default credentials (for deployed environments)
            cred = credentials.ApplicationDefault()

        firebase_admin.initialize_app(cred, {
            'projectId': 'birthdayreminder-4415f'  # From frontend config
        })
        print("Firebase initialized successfully")
    except Exception as e:
        print(f"Firebase initialization error: {e}")
        # Fallback to demo mode without Firebase
        pass

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

        # Initialize Firestore client
        try:
            self.db = firestore.client()
        except Exception as e:
            print(f"Firestore client error: {e}")
            self.db = None

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

    def handle_api_search_multi(self, query_string: str) -> None:
        """Return songs, albums, and artists for a query"""
        params = urllib.parse.parse_qs(query_string or '')
        q_list = params.get('q', [''])
        q = (q_list[0] if q_list else '').strip()
        out = {'songs': [], 'albums': [], 'artists': []}
        if not q:
            self.send_json_response(out)
            return
        if self.ytmusic:
            try:
                songs = self.ytmusic.search(q, filter='songs', limit=15)
                out['songs'] = [map_song_result(s) for s in songs if s]
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
                artists = self.ytmusic.search(q, filter='artists', limit=15)
                out['artists'] = [
                    {
                        'artistId': ar.get('browseId') or ar.get('channelId'),
                        'name': ar.get('artist') or ar.get('title') or ar.get('name'),
                        'thumbnail': (ar.get('thumbnails') or [{}])[-1].get('url') if ar.get('thumbnails') else None
                    }
                    for ar in artists if ar
                ]
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
        # demo fallback
        self.send_json_response({'album': {
            'albumId': album_id,
            'title': f'Demo Album {album_id}',
            'artist': 'Demo Artist',
            'thumbnail': None,
            'songs': get_demo_results('album')
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
            'name': f'Demo Artist {artist_id}',
            'thumbnail': None,
            'songs': get_demo_results('artist')
        }})

    def _demo_search_multi(self, q: str) -> Dict[str, Any]:
        return {
            'songs': get_demo_results(q),
            'albums': [
                {
                    'albumId': f'alb_{i}',
                    'title': f'Demo Album {i} - {q}',
                    'artist': f'Demo Artist {i}',
                    'thumbnail': f'https://via.placeholder.com/120x120/10b981/ffffff?text=Album+{i}'
                } for i in range(1, 9)
            ],
            'artists': [
                {
                    'artistId': f'art_{i}',
                    'name': f'Demo Artist {i} - {q}',
                    'thumbnail': f'https://via.placeholder.com/120x120/f59e0b/ffffff?text=Artist+{i}'
                } for i in range(1, 9)
            ]
        }

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
            if not self.db:
                self.send_json_response({'error': 'Database not available'}, 500)
                return

            user_doc = self.db.collection('users').document(user_id).get()
            if not user_doc.exists:
                self.send_json_response({'results': []})
                return

            user_data = user_doc.to_dict()
            liked_songs = user_data.get('likedSongs', [])

            # Sort by createdAt if available
            liked_songs.sort(key=lambda x: x.get('createdAt', ''), reverse=True)

            results = []
            for song in liked_songs:
                results.append({
                    'videoId': song.get('videoId'),
                    'title': song.get('title'),
                    'artist': song.get('artist'),
                    'thumbnail': song.get('thumbnail'),
                    'duration': song.get('duration')
                })

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
            if not self.db:
                self.send_json_response({'error': 'Database not available'}, 500)
                return

            user_doc = self.db.collection('users').document(user_id).get()
            if not user_doc.exists:
                self.send_json_response({'results': []})
                return

            user_data = user_doc.to_dict()
            playlists = user_data.get('playlists', [])

            # Sort by createdAt
            playlists.sort(key=lambda x: x.get('createdAt', ''), reverse=True)

            results = []
            for playlist in playlists:
                song_count = len(playlist.get('songs', []))
                results.append({
                    'id': playlist.get('id'),
                    'name': playlist.get('name'),
                    'description': playlist.get('description'),
                    'createdAt': playlist.get('createdAt'),
                    'songCount': song_count
                })

            self.send_json_response({'results': results})

        except Exception as e:
            print(f"Error fetching playlists: {e}")
            self.send_json_response({'error': 'Internal server error'}, 500)

    def handle_api_playlist(self, playlist_id: str) -> None:
        """Handle individual playlist data"""
        try:
            if not self.db:
                self.send_json_response({'error': 'Database not available'}, 500)
                return

            # Find playlist in all users' data (this is inefficient but works for demo)
            users_ref = self.db.collection('users')
            users = users_ref.stream()

            playlist_data = None
            for user_doc in users:
                user_data = user_doc.to_dict()
                playlists = user_data.get('playlists', [])
                for playlist in playlists:
                    if playlist.get('id') == playlist_id:
                        playlist_data = playlist
                        break
                if playlist_data:
                    break

            if not playlist_data:
                self.send_json_response({'error': 'Playlist not found'}, 404)
                return

            # Sort songs by position
            songs = playlist_data.get('songs', [])
            songs.sort(key=lambda x: x.get('position', 0))

            result = {
                'id': playlist_id,
                'name': playlist_data.get('name'),
                'description': playlist_data.get('description'),
                'createdAt': playlist_data.get('createdAt'),
                'songs': songs
            }

            self.send_json_response({'playlist': result})

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

            if not self.db:
                self.send_json_response({'error': 'Database not available'}, 500)
                return

            user_ref = self.db.collection('users').document(user_id)
            user_doc = user_ref.get()

            liked_songs = []
            if user_doc.exists:
                user_data = user_doc.to_dict()
                liked_songs = user_data.get('likedSongs', [])

            # Check if song already liked
            existing = next((s for s in liked_songs if s.get('videoId') == song['videoId']), None)
            if existing:
                self.send_json_response({'success': True})  # Already liked
                return

            # Add song to liked songs
            liked_song = {
                'videoId': song['videoId'],
                'title': song['title'],
                'artist': song.get('artist'),
                'thumbnail': song.get('thumbnail'),
                'duration': song.get('duration'),
                'createdAt': firestore.SERVER_TIMESTAMP
            }
            liked_songs.append(liked_song)

            # Update user document
            user_ref.set({
                'likedSongs': liked_songs
            }, merge=True)

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

            if not self.db:
                self.send_json_response({'error': 'Database not available'}, 500)
                return

            user_ref = self.db.collection('users').document(user_id)
            user_doc = user_ref.get()

            if not user_doc.exists:
                self.send_json_response({'success': True})  # Nothing to unlike
                return

            user_data = user_doc.to_dict()
            liked_songs = user_data.get('likedSongs', [])

            # Remove the song
            liked_songs = [s for s in liked_songs if s.get('videoId') != video_id]

            # Update user document
            user_ref.set({
                'likedSongs': liked_songs
            }, merge=True)

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

            if not self.db:
                self.send_json_response({'error': 'Database not available'}, 500)
                return

            user_ref = self.db.collection('users').document(user_id)
            user_doc = user_ref.get()

            playlists = []
            if user_doc.exists:
                user_data = user_doc.to_dict()
                playlists = user_data.get('playlists', [])

            # Check if playlist ID already exists
            if any(p.get('id') == playlist_id for p in playlists):
                self.send_json_response({'error': 'Playlist ID already exists'}, 400)
                return

            # Add new playlist
            new_playlist = {
                'id': playlist_id,
                'name': name,
                'description': description,
                'songs': [],
                'createdAt': firestore.SERVER_TIMESTAMP
            }
            playlists.append(new_playlist)

            # Update user document
            user_ref.set({
                'playlists': playlists
            }, merge=True)

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

            if not self.db:
                self.send_json_response({'error': 'Database not available'}, 500)
                return

            # Find the user and playlist
            users_ref = self.db.collection('users')
            users = users_ref.stream()

            user_ref = None
            playlist = None
            user_data = None
            for user_doc in users:
                user_data = user_doc.to_dict()
                playlists = user_data.get('playlists', [])
                for p in playlists:
                    if p.get('id') == playlist_id:
                        user_ref = self.db.collection('users').document(user_doc.id)
                        playlist = p
                        break
                if playlist:
                    break

            if not playlist:
                self.send_json_response({'error': 'Playlist not found'}, 404)
                return

            songs = playlist.get('songs', [])

            # Check if song already exists
            if any(s.get('videoId') == song['videoId'] for s in songs):
                self.send_json_response({'success': True})  # Already added
                return

            # Get next position
            next_position = max([s.get('position', 0) for s in songs] + [0]) + 1

            # Add song
            new_song = {
                'videoId': song['videoId'],
                'title': song['title'],
                'artist': song.get('artist'),
                'thumbnail': song.get('thumbnail'),
                'duration': song.get('duration'),
                'position': next_position,
                'addedAt': firestore.SERVER_TIMESTAMP
            }
            songs.append(new_song)

            # Update playlist
            playlist['songs'] = songs
            playlists = user_data.get('playlists', [])
            for i, p in enumerate(playlists):
                if p.get('id') == playlist_id:
                    playlists[i] = playlist
                    break

            user_ref.set({
                'playlists': playlists
            }, merge=True)

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

            if not self.db:
                self.send_json_response({'error': 'Database not available'}, 500)
                return

            # Find the user and playlist
            users_ref = self.db.collection('users')
            users = users_ref.stream()

            user_ref = None
            playlist = None
            user_data = None
            for user_doc in users:
                user_data = user_doc.to_dict()
                playlists = user_data.get('playlists', [])
                for p in playlists:
                    if p.get('id') == playlist_id:
                        user_ref = self.db.collection('users').document(user_doc.id)
                        playlist = p
                        break
                if playlist:
                    break

            if not playlist:
                self.send_json_response({'error': 'Playlist not found'}, 404)
                return

            songs = playlist.get('songs', [])

            # Remove the song
            songs = [s for s in songs if s.get('videoId') != video_id]

            # Update playlist
            playlist['songs'] = songs
            playlists = user_data.get('playlists', [])
            for i, p in enumerate(playlists):
                if p.get('id') == playlist_id:
                    playlists[i] = playlist
                    break

            user_ref.set({
                'playlists': playlists
            }, merge=True)

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
    # Initialize Firebase
    init_firebase()

    port = int(os.environ.get('PORT', '5000'))

    print(f"Wave Music Streaming Server")
    print(f"Server starting on https://music-h3vv.onrender.com:{port}")
    print(f"YTMusic API: {'Available' if YTMUSIC_AVAILABLE else 'Not available (using demo mode)'}")
    print(f"Database: Firestore")
    print("Ready to serve music!")

    try:
        with ThreadingTCPServer(('0.0.0.0', port), YTMusicRequestHandler) as httpd:
            httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n👋 Server stopped gracefully")
    except Exception as e:
        print(f"❌ Server error: {e}")