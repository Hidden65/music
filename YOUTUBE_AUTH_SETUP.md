
# YouTube Authentication Setup

To improve streaming reliability, you can set up YouTube cookies. Here are the steps:

## Method 1: Automatic Cookie Extraction (Recommended)

1. Make sure you have Chrome browser installed
2. Log into YouTube in Chrome
3. The server will automatically try to use Chrome cookies

## Method 2: Manual Cookie Export

1. Install a browser extension like "Get cookies.txt" or "cookies.txt"
2. Go to YouTube.com and log in
3. Export cookies to a file named 'youtube_cookies.txt'
4. Place the file in the same directory as server.py

## Method 3: Using yt-dlp directly

Run this command to test if yt-dlp can access YouTube:
```bash
yt-dlp --cookies-from-browser chrome "https://www.youtube.com/watch?v=dQw4w9WgXcQ" --get-url
```

## Troubleshooting

If you're still getting authentication errors:

1. Try clearing your browser cookies and logging in again
2. Make sure you're logged into YouTube in Chrome
3. Check if your IP is being blocked by YouTube
4. Consider using a VPN if you're in a restricted region

## Alternative Solutions

If cookie-based authentication doesn't work, the server will try multiple fallback methods:
- Different user agents
- Alternative YouTube clients
- Direct URL construction

The server is designed to be resilient and will try multiple approaches automatically.
