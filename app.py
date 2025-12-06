from flask import Flask, render_template, request, jsonify, send_file
from flask_cors import CORS
import yt_dlp
import os
import re
import tempfile
import threading
import time
import uuid

app = Flask(__name__)
CORS(app)

# Temp directory for downloads
DOWNLOAD_DIR = tempfile.mkdtemp()

# Clean up old files periodically
def cleanup_old_files():
    """Remove files older than 10 minutes"""
    while True:
        time.sleep(300)
        now = time.time()
        for filename in os.listdir(DOWNLOAD_DIR):
            filepath = os.path.join(DOWNLOAD_DIR, filename)
            if os.path.isfile(filepath):
                if now - os.path.getmtime(filepath) > 600:
                    try:
                        os.remove(filepath)
                    except:
                        pass

cleanup_thread = threading.Thread(target=cleanup_old_files, daemon=True)
cleanup_thread.start()

def sanitize_filename(filename):
    """Remove invalid characters from filename"""
    return re.sub(r'[<>:"/\\|?*]', '', filename)[:100]

def format_size(bytes_size):
    """Format bytes to human readable size"""
    if not bytes_size or bytes_size <= 0:
        return None
    if bytes_size >= 1024 * 1024 * 1024:
        return f"{bytes_size / (1024 * 1024 * 1024):.1f} GB"
    elif bytes_size >= 1024 * 1024:
        return f"{bytes_size / (1024 * 1024):.1f} MB"
    elif bytes_size >= 1024:
        return f"{bytes_size / 1024:.1f} KB"
    return f"{bytes_size} B"

def estimate_video_size(duration, height, tbr):
    """Estimate video size based on duration and bitrate"""
    if not duration:
        return None
    
    if tbr and tbr > 0:
        return int(tbr * 1000 / 8 * duration)
    
    bitrate_map = {
        2160: 20000, 1440: 12000, 1080: 5000, 720: 2500,
        480: 1500, 360: 800, 240: 400, 144: 200,
    }
    
    closest = min(bitrate_map.keys(), key=lambda x: abs(x - height))
    bitrate = bitrate_map[closest]
    return int(bitrate * 1000 / 8 * duration)

def get_ydl_opts():
    """Get yt-dlp options with anti-bot measures"""
    return {
        'quiet': True,
        'no_warnings': True,
        'extract_flat': False,
        'http_headers': {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
            'Accept-Language': 'en-us,en;q=0.5',
            'Sec-Fetch-Mode': 'navigate',
        },
        'extractor_args': {
            'youtube': {
                'player_client': ['android', 'web'],
                'player_skip': ['webpage', 'configs'],
            }
        },
        'socket_timeout': 30,
    }

@app.route('/')
def home():
    return render_template('index.html')

@app.route('/get-info', methods=['POST'])
def get_info():
    data = request.get_json()
    url = data.get('url')

    if not url:
        return jsonify({'error': 'No URL provided'}), 400

    ydl_opts = get_ydl_opts()

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)
            
            duration = info.get('duration', 0)
            
            metadata = {
                'title': info.get('title'),
                'thumbnail': info.get('thumbnail'),
                'duration': info.get('duration_string', 'N/A'),
                'duration_seconds': duration,
                'video_id': info.get('id'),
                'original_url': url,
                'formats': []
            }

            all_formats = info.get('formats', [])
            
            # Find video+audio combined formats
            seen_res = {}
            for f in all_formats:
                height = f.get('height')
                has_video = f.get('vcodec') and f.get('vcodec') != 'none'
                has_audio = f.get('acodec') and f.get('acodec') != 'none'
                
                if has_video and has_audio and height:
                    filesize = f.get('filesize') or f.get('filesize_approx')
                    tbr = f.get('tbr', 0) or 0
                    
                    if not filesize and duration > 0:
                        filesize = estimate_video_size(duration, height, tbr)
                    
                    if height not in seen_res or tbr > (seen_res[height].get('tbr', 0) or 0):
                        seen_res[height] = {
                            'format_id': f.get('format_id'),
                            'height': height,
                            'ext': f.get('ext', 'mp4'),
                            'filesize': filesize,
                            'tbr': tbr
                        }
            
            for height in sorted(seen_res.keys(), reverse=True):
                fmt_info = seen_res[height]
                size_str = format_size(fmt_info['filesize']) or f"~{height}p"
                
                metadata['formats'].append({
                    'type': 'video',
                    'label': f"{height}p",
                    'ext': fmt_info['ext'],
                    'size': size_str,
                    'format_id': fmt_info['format_id'],
                    'height': height
                })
            
            # Audio option
            if duration > 0:
                mp3_size = int((192 * 1000 / 8) * duration)
                mp3_size_str = format_size(mp3_size)
            else:
                mp3_size_str = "High Quality"
            
            metadata['formats'].append({
                'type': 'audio',
                'label': 'MP3 Audio',
                'ext': 'mp3',
                'size': mp3_size_str,
                'format_id': 'bestaudio'
            })

            return jsonify(metadata)

    except Exception as e:
        error_msg = str(e)
        if 'Sign in to confirm' in error_msg or 'bot' in error_msg.lower():
            return jsonify({'error': 'YouTube is blocking this request. Try a different video or try again later.'}), 500
        return jsonify({'error': error_msg}), 500


@app.route('/download', methods=['POST'])
def download_file():
    """Download video or audio"""
    data = request.get_json()
    url = data.get('url')
    format_id = data.get('format_id')
    file_type = data.get('type', 'video')
    title = sanitize_filename(data.get('title', 'download'))
    
    if not url:
        return jsonify({'error': 'No URL provided'}), 400

    unique_id = str(uuid.uuid4())[:8]
    
    try:
        base_opts = get_ydl_opts()
        
        if file_type == 'audio':
            ydl_opts = {
                **base_opts,
                'format': 'bestaudio/best',
                'outtmpl': os.path.join(DOWNLOAD_DIR, f"{unique_id}.%(ext)s"),
                'postprocessors': [{
                    'key': 'FFmpegExtractAudio',
                    'preferredcodec': 'mp3',
                    'preferredquality': '192',
                }],
            }
            
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                ydl.download([url])
            
            mp3_path = os.path.join(DOWNLOAD_DIR, f"{unique_id}.mp3")
            if os.path.exists(mp3_path):
                return jsonify({
                    'success': True,
                    'download_url': f'/serve-file/{unique_id}.mp3',
                    'filename': f"{title}.mp3"
                })
            else:
                return jsonify({'error': 'MP3 conversion failed'}), 500
        else:
            ydl_opts = {
                **base_opts,
                'format': format_id,
                'outtmpl': os.path.join(DOWNLOAD_DIR, f"{unique_id}.%(ext)s"),
            }
            
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                ydl.download([url])
            
            downloaded_file = None
            for f in os.listdir(DOWNLOAD_DIR):
                if f.startswith(unique_id):
                    downloaded_file = os.path.join(DOWNLOAD_DIR, f)
                    break
            
            if downloaded_file:
                ext = os.path.splitext(downloaded_file)[1]
                return jsonify({
                    'success': True,
                    'download_url': f'/serve-file/{os.path.basename(downloaded_file)}',
                    'filename': f"{title}{ext}"
                })
            else:
                return jsonify({'error': 'Download failed'}), 500
            
    except Exception as e:
        error_msg = str(e)
        if 'Sign in to confirm' in error_msg or 'bot' in error_msg.lower():
            return jsonify({'error': 'YouTube is blocking this request. Try again later.'}), 500
        return jsonify({'error': error_msg}), 500


@app.route('/serve-file/<filename>')
def serve_file(filename):
    """Serve the downloaded file"""
    filepath = os.path.join(DOWNLOAD_DIR, filename)
    
    if not os.path.exists(filepath):
        return "File not found", 404
    
    download_name = request.args.get('name', filename)
    
    ext = os.path.splitext(filename)[1].lower()
    mime_types = {
        '.mp4': 'video/mp4',
        '.webm': 'video/webm',
        '.mkv': 'video/x-matroska',
        '.mp3': 'audio/mpeg',
        '.m4a': 'audio/mp4',
    }
    mimetype = mime_types.get(ext, 'application/octet-stream')
    
    return send_file(
        filepath,
        mimetype=mimetype,
        as_attachment=True,
        download_name=download_name
    )


if __name__ == '__main__':
    app.run(debug=True, port=5000)