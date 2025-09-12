#!/usr/bin/env python3
"""
YouTube Authentication Setup Helper

This script helps set up YouTube authentication for the music streaming server.
It provides instructions and tools to extract YouTube cookies for better streaming reliability.
"""

import os
import json
import sys
from pathlib import Path

def create_cookie_instructions():
    """Create instructions for setting up YouTube cookies"""
    instructions = """
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
"""
    
    with open('YOUTUBE_AUTH_SETUP.md', 'w') as f:
        f.write(instructions)
    
    print("Created YOUTUBE_AUTH_SETUP.md with detailed instructions")
    return instructions

def test_yt_dlp():
    """Test if yt-dlp is working with different configurations"""
    try:
        import yt_dlp
        print("✓ yt-dlp is installed")
        
        # Test basic functionality
        test_url = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
        
        configs = [
            {"name": "Basic", "opts": {"quiet": True, "no_warnings": True}},
            {"name": "With Chrome cookies", "opts": {"quiet": True, "no_warnings": True, "cookiesfrombrowser": ("chrome",)}},
            {"name": "Android client", "opts": {"quiet": True, "no_warnings": True, "extractor_args": {"youtube": {"player_client": ["android_music"]}}}},
        ]
        
        for config in configs:
            try:
                print(f"Testing {config['name']}...")
                with yt_dlp.YoutubeDL(config['opts']) as ydl:
                    info = ydl.extract_info(test_url, download=False)
                    if info and info.get('formats'):
                        print(f"✓ {config['name']} works!")
                        return True
                    else:
                        print(f"✗ {config['name']} failed - no formats found")
            except Exception as e:
                print(f"✗ {config['name']} failed: {e}")
        
        print("⚠ All configurations failed. YouTube may be blocking requests.")
        return False
        
    except ImportError:
        print("✗ yt-dlp is not installed")
        print("Install it with: pip install yt-dlp")
        return False

def main():
    print("YouTube Authentication Setup Helper")
    print("=" * 40)
    
    # Create instructions
    create_cookie_instructions()
    
    # Test yt-dlp
    print("\nTesting yt-dlp configurations...")
    if test_yt_dlp():
        print("\n✓ YouTube streaming should work!")
    else:
        print("\n⚠ YouTube streaming may have issues. Check the setup instructions.")
    
    print("\nSetup complete! Check YOUTUBE_AUTH_SETUP.md for detailed instructions.")

if __name__ == "__main__":
    main()
