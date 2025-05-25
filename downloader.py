import os
import re
import sys
import time
import logging
import threading
import requests
import m3u8
import ffmpeg
import platform
import subprocess
import shutil
from pathlib import Path
from bs4 import BeautifulSoup
from urllib.parse import urljoin, quote_plus
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from concurrent.futures import ThreadPoolExecutor, as_completed
from tqdm import tqdm

# Platform-specific configurations
IS_WINDOWS = platform.system() == 'Windows'
IS_MACOS = platform.system() == 'Darwin'
IS_LINUX = platform.system() == 'Linux'

# Get the directory where the executable is located
def get_base_path():
    if getattr(sys, 'frozen', False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))

# Налаштування логування
logging.basicConfig(level=logging.ERROR,
                    format='%(asctime)s - %(levelname)s - %(message)s')

# Налаштування HTTP-сесії з повторами
def create_session(retries=5, backoff=1):
    session = requests.Session()
    retry = Retry(total=retries,
                  backoff_factor=backoff,
                  status_forcelist=[500, 502, 503, 504],
                  allowed_methods=["GET", "POST"])
    adapter = HTTPAdapter(max_retries=retry)
    session.mount('http://', adapter)
    session.mount('https://', adapter)
    session.headers.update({'User-Agent': 'Mozilla/5.0'})
    return session

session = create_session()

# Отримання ID з URL
def extract_news_id(page_url):
    m = re.search(r"/(\d+)-", page_url)
    if not m:
        raise ValueError(f"Не вдалося отримати ID з {page_url}")
    return m.group(1)

# Завантаження HTML плейлистів
def fetch_playlists_html(news_id):
    timestamp = int(time.time())
    ajax_url = (
        f"https://uakino.me/engine/ajax/playlists.php"
        f"?news_id={news_id}&xfield=playlist&time={timestamp}"
    )
    headers = {
        'Accept': '*/*',
        'Content-Type': 'application/x-www-form-urlencoded; charset=UTF-8',
        'Origin': 'https://uakino.me',
        'Referer': 'https://uakino.me/',
        'X-Requested-With': 'XMLHttpRequest'
    }
    resp = session.post(ajax_url, headers=headers)
    resp.raise_for_status()
    data = resp.json()
    if not data.get('success'):
        raise RuntimeError('Не вдалося завантажити плейлисти')
    return data['response']

# Парсинг голосів і епізодів
def parse_playlists(html):
    soup = BeautifulSoup(html, 'html.parser')
    voices = []
    for li in soup.select('.playlists-lists .playlists-items li'):
        vid = li.get('data-id')
        name = re.sub(r'\s*\(.*?\)', '', li.get_text(strip=True))
        if vid and '_' in vid:
            voices.append({'id': vid, 'name': name})

    episodes = {}
    for li in soup.select('.playlists-videos .playlists-items li'):
        vid = li.get('data-id')
        title = li.get_text(strip=True)
        file_url = 'https:' + li['data-file']
        if vid and title and file_url:
            if vid not in episodes:
                episodes[vid] = {}
            episodes[vid][title] = file_url
    return voices, episodes

# Вибір найкращого варіанту M3U8
def pick_best_variant(master_url):
    playlist = m3u8.load(master_url)
    if playlist.is_variant:
        best = max(playlist.playlists, key=lambda p: p.stream_info.bandwidth or 0)
        uri = urljoin(master_url, best.uri)
        segs = m3u8.load(uri).segments
        duration = sum(s.duration for s in segs)
        return uri, duration
    else:
        duration = sum(s.duration for s in playlist.segments)
        return master_url, duration

# Завантаження та об'єднання з прогрес-баром
def sanitize_filename(filename):
    """Remove invalid characters from filename."""
    if IS_WINDOWS:
        # Windows doesn't allow: \ / : * ? " < > |
        return re.sub(r'[\\/*?:"<>|]', '', filename)
    return filename

def get_ffmpeg_path():
    """Get the path to ffmpeg executable."""
    if IS_WINDOWS:
        # On Windows, we'll use the bundled ffmpeg from imageio-ffmpeg
        try:
            return iio.get_ffmpeg_exe()
        except Exception:
            # Fallback to system PATH
            return 'ffmpeg'
    else:
        # On macOS/Linux, use system ffmpeg
        return 'ffmpeg'

def download_and_mux(title, url, out_dir, pos):
    # Ensure output directory exists
    os.makedirs(out_dir, exist_ok=True)
    
    # Sanitize the title for use in filenames
    safe_title = sanitize_filename(title)
    base = safe_title
    filename = f"{base}.mp4"
    out_path = os.path.join(out_dir, filename)
    if os.path.exists(out_path):
        return title, 'exists', out_path

    try:
        r = session.get(url)
        r.raise_for_status()
        html = r.text
        m3u8_match = re.search(r'file:"(https://[^\"]+\.m3u8)"', html)
        if not m3u8_match:
            raise ValueError("Не вдалося знайти URL M3U8 на сторінці епізоду")
        master_url = m3u8_match.group(1)
        variant_url, total_duration = pick_best_variant(master_url)

        prog_file = os.path.join(out_dir, f"{base}_prog.txt")
        # Use the appropriate ffmpeg path
        ffmpeg_path = get_ffmpeg_path()
        
        # Build ffmpeg command
        stream = (
            ffmpeg.input(variant_url, protocol_whitelist='file,http,https,tcp,tls')
                  .output(out_path, c='copy', **{'bsf:a': 'aac_adtstoasc', 'y': None, 'progress': prog_file})
        )
        
        # Set the ffmpeg path in the environment
        os.environ['IMAGEIO_FFMPEG_EXE'] = ffmpeg_path
        # Initialize progress variables as nonlocal
        last_progress = 0
        last_update_time = time.time()
        last_bytes = 0
        
        bar = tqdm(total=100, desc=title, position=pos, leave=False, unit='%', 
                   bar_format='{l_bar}{bar}| {n_fmt}/{total_fmt} [{elapsed}<{remaining}, {rate_fmt}{postfix}]')

        def updater():
            nonlocal last_progress, last_update_time, last_bytes
            
            while not os.path.exists(prog_file):
                time.sleep(0.1)
            while True:
                try:
                    with open(prog_file, 'r', encoding='utf-8') as f:
                        text = f.read()
                    
                    # Check if download is complete
                    if 'progress=end' in text:
                        bar.update(100 - bar.n)
                        break
                        
                    # Parse the progress file
                    out_time = 0
                    for line in text.splitlines():
                        if 'out_time_ms' in line:
                            try:
                                out_time = int(line.split('=')[1].strip()) / 1_000_000  # Convert to seconds
                            except (ValueError, IndexError):
                                continue
                    
                    # Calculate and update progress if we have valid values
                    if out_time > 0 and total_duration > 0:
                        progress = min(int((out_time / total_duration) * 100), 100)
                        if progress > last_progress:  # Only update if progress increased
                            # Calculate download speed
                            current_time = time.time()
                            time_elapsed = current_time - last_update_time
                            
                            # Get current file size if it exists
                            current_bytes = os.path.getsize(out_path) if os.path.exists(out_path) else 0
                            if last_bytes > 0 and time_elapsed > 0:
                                speed = (current_bytes - last_bytes) / (1024 * 1024) / time_elapsed  # MB/s
                                bar.set_postfix(speed=f'{speed:.2f}MB/s')
                            
                            last_bytes = current_bytes
                            last_update_time = current_time
                            
                            bar.update(progress - last_progress)
                            last_progress = progress
                    
                    time.sleep(0.5)
                    
                except FileNotFoundError:
                    break
                except Exception as e:
                    logging.error(f"Progress update error: {str(e)}")
                    time.sleep(1)
                    continue
            
            bar.close()
            try:
                os.remove(prog_file)
            except OSError:
                pass

        thread = threading.Thread(target=updater)
        thread.start()
        # Run ffmpeg with the configured path
        ffmpeg.run(stream, cmd=ffmpeg_path, capture_stdout=True, capture_stderr=True)
        thread.join()

        return title, 'downloaded', out_path
    except Exception as e:
        logging.error(f"Помилка завантаження {title}: {str(e)}")
        return title, 'error', str(e)

# Основна логіка з покращеним використанням tqdm
def ensure_ffmpeg_installed():
    """Check if ffmpeg is installed and try to install it if not."""
    try:
        subprocess.run([get_ffmpeg_path(), '-version'], 
                      stdout=subprocess.PIPE, 
                      stderr=subprocess.PIPE)
        return True
    except (FileNotFoundError, Exception):
        print("FFmpeg не знайдено. Спробуємо встановити...")
        try:
            if IS_WINDOWS:
                # On Windows, we'll use the bundled ffmpeg from imageio-ffmpeg
                import imageio_ffmpeg
                return True
            elif IS_MACOS:
                print("Будь ласка, встановіть FFmpeg за допомогою Homebrew:")
                print("  brew install ffmpeg")
            elif IS_LINUX:
                print("Будь ласка, встановіть FFmpeg за допомогою менеджера пакетів:")
                print("  sudo apt install ffmpeg")
            return False
        except Exception as e:
            print(f"Помилка при спробі встановити FFmpeg: {e}")
            return False

def main():
    # Check for FFmpeg
    if not ensure_ffmpeg_installed():
        print("Помилка: FFmpeg не встановлено. Будь ласка, встановіть FFmpeg для продовження.")
        if IS_WINDOWS:
            print("Завантажте з: https://ffmpeg.org/download.html")
        return
        
    try:
        url = input('Вставте URL серії: ').strip()
        news_id = extract_news_id(url)
        html = fetch_playlists_html(news_id)

        voices, all_episodes = parse_playlists(html)
        if not voices:
            print("Озвучень не знайдено.")
            return

        print('Доступні озвучення:')
        for i, v in enumerate(voices, 1):
            print(f"{i}. {v['name']}")
        choice = int(input('Виберіть номер озвучення: ')) - 1
        if choice < 0 or choice >= len(voices):
            print("Неправильний вибір.")
            return
        voice = voices[choice]

        episodes = all_episodes.get(voice['id'], {})
        if not episodes:
            print("Епізодів для цього озвучення не знайдено.")
            return

        out_dir = input('Папка для завантаження: ').strip() or f"downloads_{news_id}"
        try:
            os.makedirs(out_dir, exist_ok=True)
        except OSError as e:
            print(f"Не вдалося створити директорію: {e}")
            return

        # Повідомлення про початок завантаження через tqdm.write
        tqdm.write(f"Завантаження {len(episodes)} епізодів з озвученням '{voice['name']}'...")

        with ThreadPoolExecutor(max_workers=4) as executor:
            futures = {
                executor.submit(
                    download_and_mux,
                    f"{title} ({voice['name']})",
                    episodes[title], out_dir, idx + 1
                ): title for idx, title in enumerate(sorted(episodes))
            }
            for future in as_completed(futures):
                title = futures[future]
                try:
                    name, status, path = future.result()
                    sym = '✓' if status == 'downloaded' else '~' if status == 'exists' else '✗'
                    # Виведення статусу через tqdm.write для стабільності консолі
                    tqdm.write(f"[{sym}] {name} → {path}")
                except Exception as e:
                    tqdm.write(f"[✗] {title} → Помилка: {str(e)}")
    except Exception as e:
        print(f"Виникла несподівана помилка: {str(e)}")

if __name__ == '__main__':
    main()