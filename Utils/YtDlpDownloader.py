'''YtDlp download client for the UDB downloader daemon.

Alternative to the custom Base/HLSDownloader pipeline that delegates to
yt-dlp instead. Pattern borrowed from debakarr/kisskh-dl.
'''

import os
import requests
from urllib.parse import urlparse


CHROME_UA = ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
             'AppleWebKit/537.36 (KHTML, like Gecko) '
             'Chrome/108.0.0.0 Safari/537.36')


class YtDlpDownloader:
    '''Download Client for videos using yt-dlp'''

    def __init__(self, config, ep_details):
        # set downloader configuration
        self.download_dir = config.get('download_dir', '.')
        self.quality = config.get('quality', 1080)
        self.referer = config.get('referer', '')
        # optional callable(word, key, iv) -> str used to decrypt subtitles
        self._aes_decrypt = config.get('_aes_decrypt')
        # set episode details
        self.episode_name = ep_details.get('episodeName', 'output')
        self.subtitles = ep_details.get('subtitles', {})
        self.encrypted_subs_details = ep_details.get('encrypted_subs_details', {})

        # base output path (without extension) for outtmpl and subtitles
        self.base = os.path.join(self.download_dir, self.episode_name)
        if self.base.endswith('.mp4'):
            self.base = self.base[:-4]

    def start_download(self, download_link):
        '''Download the video using yt-dlp. Returns (status, message).'''
        # lazy import so the daemon still runs without yt-dlp installed
        try:
            import yt_dlp
        except ImportError:
            return (1, 'yt-dlp not installed. Install with: pip install yt-dlp')

        os.makedirs(self.download_dir, exist_ok=True)

        ydl_fmt = f'bestvideo[height<={self.quality}]+bestaudio/'
        ydl_fmt += f'best[height<={self.quality}]/best'

        ydl_opts = {
            'format': ydl_fmt,
            'concurrent_fragment_downloads': 15,
            'outtmpl': self.base + '.%(ext)s',
            'merge_output_format': 'mp4',
            'noplaylist': True,
            'retries': 10,
            'http_headers': {
                'Referer': self.referer,
                'User-Agent': CHROME_UA,
            },
            'quiet': True,
            'no_warnings': True,
        }

        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                ydl.download([download_link])
        except Exception as e:
            return (1, f'yt-dlp download failed: {e}')

        out_file = self._find_output(self.base)
        if out_file is None:
            return (1, 'yt-dlp finished but no output file found')
        return (0, os.path.basename(out_file))

    def download_subtitles(self):
        '''Download subtitles to the output directory. Returns written paths.'''
        os.makedirs(self.download_dir, exist_ok=True)
        written = []

        for label, src in self.subtitles.items():
            try:
                ext = os.path.splitext(urlparse(src).path)[1]
                response = requests.get(src, timeout=60)
                response.raise_for_status()

                if label in self.encrypted_subs_details and callable(self._aes_decrypt):
                    details = self.encrypted_subs_details[label]
                    decrypted = self._aes_decrypt(
                        response.text, details['key'], details['iv'])
                    sub_file = f'{self.base}.{label}.srt'
                    with open(sub_file, 'w', encoding='utf-8') as f:
                        f.write(decrypted)
                else:
                    sub_file = f'{self.base}.{label}{ext}'
                    with open(sub_file, 'wb') as f:
                        f.write(response.content)

                written.append(sub_file)

            except Exception:
                # skip subtitles that fail to download/decrypt
                pass

        return written

    def _find_output(self, base):
        '''Find the downloaded file starting with the base name. Prefer mp4.'''
        prefix = os.path.basename(base)
        try:
            files = os.listdir(self.download_dir)
        except OSError:
            return None

        matches = [f for f in files if f.startswith(prefix)]
        if not matches:
            return None

        for name in matches:
            if name.endswith('.mp4'):
                return os.path.join(self.download_dir, name)
        return os.path.join(self.download_dir, matches[0])
