__author__ = 'Prudhvi PLN'

import os
import re

from Utils.commons import retry
from Utils.BaseDownloader import BaseDownloader
import http.client


class HLSDownloader(BaseDownloader):
    '''Download Client for HLS files'''
    # References: https://github.com/Oshan96/monkey-dl/blob/master/anime_downloader/util/hls_downloader.py
    # https://github.com/josephcappadona/m3u8downloader/blob/master/m3u8downloader/m3u8.py

    def __init__(self, dl_config, ep_details, session=None):
        # initialize base downloader
        super().__init__(dl_config, ep_details, session)
        # Store the dl_config for use in sub-sequent downloads (like audio)
        self.dl_config = dl_config
        # initialize HLS specific configuration
        self.m3u8_file = os.path.join(f'{self.temp_dir}', 'uwu.m3u8')
        self.thread_name_prefix = 'udb-hls-'

    def _has_uri(self, m3u8_data):
        method = re.search('URI=(.*)', m3u8_data)
        if method is None: return False
        if method.group(1) == "NONE": return False

        return True

    def _collect_uri_iv(self, m3u8_data):
        # Case-1: typical HLS using URI & IV
        uri_iv = re.search('#EXT-X-KEY:METHOD=AES-128,URI="(.*)",IV=(.*)', m3u8_data)

        # Case-2: typical HLS using URI only
        if uri_iv is None:
            uri_data = re.search('URI="(.*)"', m3u8_data)
            return uri_data.group(1), None

        uri = uri_iv.group(1)
        iv = uri_iv.group(2)

        return uri, iv

    def _collect_ts_urls(self, m3u8_link, m3u8_data):
        # Improved regex to handle all cases. (get all lines except those starting with #)
        base_url = '/'.join(m3u8_link.split('/')[:-1])
        normalize_url = lambda url, base_url: (url if url.startswith('http') else 'https:' + url if url.startswith('//') else base_url + '/' + url)
        # Some m3u8 files have duplicate urls, so using set to remove duplicates
        urls = list(set( normalize_url(url.group(0), base_url) for url in re.finditer("^(?!#).+$", m3u8_data, re.MULTILINE) ))

        return urls

    @retry()
    def _download_segment(self, ts_url):
        '''
        download segment file from url. Reuse if already downloaded.

        Returns: (download_status, progress_bar_increment)
        '''
        try:
            # some urls have query params, so we need to strip them off to get the filename
            segment_file_nm = ts_url.split('/')[-1].split('?')[0]
            segment_file = os.path.join(f"{self.temp_dir}", f"{segment_file_nm}")

            # check if the segment is already downloaded
            if os.path.isfile(segment_file) and os.path.getsize(segment_file) > 0:
                return (f'Segment file [{segment_file_nm}] already exists. Reusing.', 1)

            # Stream download so we can honor pause/cancel events
            response = self._get_raw_stream_data(ts_url, stream=True)
            size = 0
            with open(segment_file, 'wb') as ts_file:
                if isinstance(response, http.client.HTTPResponse):
                    while True:
                        if self.controller:
                            if self.controller.is_cancelled():
                                return (f'ERROR: Segment download cancelled [{segment_file_nm}]', 0)
                            self.controller.wait_if_paused()

                        chunk = response.read(1024*64)
                        if not chunk:
                            break
                        size += ts_file.write(chunk)
                else:
                    for chunk in response.iter_content(1024*64):
                        if self.controller:
                            if self.controller.is_cancelled():
                                return (f'ERROR: Segment download cancelled [{segment_file_nm}]', 0)
                            self.controller.wait_if_paused()

                        if chunk:
                            size += ts_file.write(chunk)

            return (f'Segment file [{segment_file_nm}] downloaded', 1)

        except Exception as e:
            return (f'\nERROR: Segment download failed [{segment_file_nm}] due to: {e}', 0)

    def _rewrite_m3u8_file(self, m3u8_data):
        # regex safe temp dir path
        seg_temp_dir = self.temp_dir.replace('\\', '\\\\')
        # ffmpeg doesn't accept backward slash in key file irrespective of platform
        key_temp_dir = self.temp_dir.replace('\\', '/')
        with open(self.m3u8_file, 'w', encoding='utf-8') as m3u8_f:
            m3u8_content = re.sub('URI=(.*)/', f'URI="{key_temp_dir}/', m3u8_data, count=1)
            regex_safe = '\\\\' if os.sep == '\\' else '/'
            # replace the segment urls with the downloaded segment file paths
            m3u8_content = re.sub(
                r'^(?!#)(.+)$',
                lambda m: f"{seg_temp_dir}{regex_safe}{os.path.basename(m.group(1).split('?', 1)[0].strip())}",
                m3u8_content,
                flags=re.MULTILINE
            )
            m3u8_f.write(m3u8_content)

    def _save_as_audio(self):
        # print(f'Converting {self.out_file} to aac')
        out_file = os.path.join(f'{self.out_dir}', f'{self.out_file}')
        # extension_picky 0 -allowed_extensions ALL is used to allow ffmpeg to read all types of files, including .ts files with non-standard extensions like png/jpg
        cmd = f'ffmpeg -loglevel warning -extension_picky 0 -allowed_extensions ALL -i "{self.m3u8_file}" -map 0:a -c:a copy -bsf:a aac_adtstoasc "{out_file}"'
        self._exec_cmd(cmd)

    def _save_as_video(self):
        # print(f'Converting {self.out_file} to mp4')
        out_file = os.path.join(f'{self.out_dir}', f'{self.out_file}')
        # extension_picky 0 -allowed_extensions ALL is used to allow ffmpeg to read all types of files, including .ts files with non-standard extensions like png/jpg
        command = [f'ffmpeg -loglevel warning -extension_picky 0 -allowed_extensions ALL -i "{self.m3u8_file}"']
        maps = ['-map 0:v']
        metadata = []
        stream_idx = 1

        # Add audio input if available, else use the default audio from the m3u8 file
        if self.audio:
            command.append(f'-i "{self.audio}"')
            maps.append('-map 1:a')
            stream_idx += 1
        else:
            maps.append('-map 0:a')

        # Prepare the command if subtitles are present
        for i, (lang, url) in enumerate(self.subtitles.items(), start=stream_idx):
            command.append(f'-i "{url}"')
            maps.append(f'-map {i}')
            metadata.append(f'-metadata:s:s:{i - (2 if self.audio else 1)} title="{lang}"')

        metadata.append(f'-c:v copy -c:a copy -c:s mov_text -bsf:a aac_adtstoasc "{out_file}"')

        cmd = ' '.join(command + maps + metadata)
        self._exec_cmd(cmd)

    def start_download(self, m3u8_link):
        # create output directory
        self._create_out_dirs()

        iv = None
        self.logger.debug('Fetching stream data')
        m3u8_data = self._get_stream_data(m3u8_link, True)

        self.logger.debug('Check if stream is encrypted/mapped')
        if self._has_uri(m3u8_data):
            self.logger.debug('Stream is encrypted/mapped. Collect iv data and download key')
            key_uri, iv = self._collect_uri_iv(m3u8_data)
            status = self._download_segment(key_uri)
            if status[1] == 0: self.logger.error(f'Failed to download key/map file with error: {status[0]}')

        # did not run into HLS with IV during development, so skipping it
        if iv:
            raise Exception("Current code cannot decode IV links")

        self.logger.debug('Collect m3u8 segment urls')
        ts_urls = self._collect_ts_urls(m3u8_link, m3u8_data)

        self.logger.debug('Downloading collected segments')
        metadata = {
            'type': 'segments',
            'total': len(ts_urls),
            'unit': 'seg'
        }
        self._multi_threaded_download(self._download_segment, ts_urls, **metadata)

        self.logger.debug('Rewrite m3u8 file with downloaded segments paths')
        self._rewrite_m3u8_file(m3u8_data)

        if self.audio:
            self.logger.info('Downloading HLS audio stream...')
            # Audio stream is also HLS, so we can use the a new instance of HLSDownloader to download the audio stream
            audio_file_name = self.out_file.replace('.mp4', '.aac')
            audio_full_file_name = os.path.join(f'{self.temp_dir}', f'{audio_file_name}')
            if os.path.isfile(audio_full_file_name) and os.path.getsize(audio_full_file_name) > 0:
                self.logger.debug(f'Audio file [{audio_file_name}] already exists. Reusing.')
            else:
                dl_config = self.dl_config.copy()  # Create a copy of the dl_config to avoid modifying the original
                dl_config.update({
                    'download_dir': self.temp_dir,  # Download audio to the same temp directory as video segments
                    'temp_download_dir': os.path.join(self.temp_dir, 'audio')
                })
                # propagate controller if present so audio downloader honors pause/cancel
                if isinstance(self.dl_config, dict) and self.dl_config.get('_controller'):
                    dl_config['_controller'] = self.dl_config['_controller']
                audio_downloader = HLSDownloader(dl_config, {'episodeName': audio_file_name})
                status = audio_downloader.start_download(self.audio)
                if status[0] != 0: self.logger.error(f'Failed to download the audio with error: {status[1]}')
            self.audio = audio_full_file_name

        if self.subtitles:
            self.logger.debug('Downloading subtitles')
            self._download_subtitles()

        if self.out_file.endswith('.mp4'):
            self.logger.debug('Converting m3u8 segments to .mp4')
            self._save_as_video()
        elif self.out_file.endswith('.aac'):
            self.logger.debug('Converting m3u8 segments to audio file')
            self._save_as_audio()

        # remove temp dir once completed and dir is empty
        self.logger.debug('Removing temporary directories')
        self._remove_out_dirs()

        return (0, None)
