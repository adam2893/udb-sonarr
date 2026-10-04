"""Offline start_download regressions for HLS key and initialization files."""

import importlib.util
import re
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import Mock, patch


def load_downloader():
    # Load only the implementation under test, with scoped dependency stubs.
    # Do not leave fake requests/tqdm/crypto modules behind for other tests.
    base = types.ModuleType('Utils.BaseDownloader')
    base.BaseDownloader = type('BaseDownloader', (), {})
    commons = types.ModuleType('Utils.commons')
    commons.retry = lambda *args, **kwargs: lambda function: function
    source = Path(__file__).resolve().parents[1] / 'Utils' / 'HLSDownloader.py'
    spec = importlib.util.spec_from_file_location('_hls_auxiliary_under_test', source)
    module = importlib.util.module_from_spec(spec)
    with patch.dict(sys.modules, {'Utils.BaseDownloader': base, 'Utils.commons': commons}):
        spec.loader.exec_module(module)
    return module.HLSDownloader


class HLSAuxiliaryTests(unittest.TestCase):
    playlist_url = 'https://stream.example/shows/episode/index.m3u8?playlist=secret'

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        downloader_class = load_downloader()
        self.downloader = downloader_class.__new__(downloader_class)
        self.downloader.temp_dir = self.directory.name
        self.downloader.m3u8_file = str(Path(self.directory.name) / 'uwu.m3u8')
        self.downloader.out_file = 'episode.mp4'
        self.downloader.audio = None
        self.downloader.subtitles = {}
        self.downloader.logger = Mock()
        self.downloader._create_out_dirs = Mock()
        self.downloader._remove_out_dirs = Mock()
        self.downloader._exec_cmd = Mock()
        self.downloader._save_as_video = Mock()
        self.downloader._save_as_audio = Mock()
        self.downloader._multi_threaded_download = Mock()
        self.downloader._download_segment = Mock(side_effect=self.download_file)

    def download_file(self, url):
        filename = url.rsplit('/', 1)[-1].split('?', 1)[0]
        (Path(self.directory.name) / filename).write_bytes(b'fixture')
        return ('downloaded', 1)

    def start(self, tags):
        playlist = '#EXTM3U\n' + tags + '\n#EXTINF:5,\nsegment.ts?media=token\n'
        self.downloader._get_stream_data = Mock(return_value=playlist)
        return self.downloader.start_download(self.playlist_url)

    def rewritten(self):
        return Path(self.downloader.m3u8_file).read_text(encoding='utf-8')

    def test_protocol_relative_map_is_fetched_over_https(self):
        self.start('#EXT-X-MAP:URI="//cdn.example/init.mp4?token=secret",BYTERANGE="40@0"')
        self.downloader._download_segment.assert_called_once_with(
            'https://cdn.example/init.mp4?token=secret')
        self.assertIn(f'URI="{Path(self.directory.name).as_posix()}/init.mp4",BYTERANGE="40@0"', self.rewritten())
        self.downloader._save_as_video.assert_called_once_with()

    def test_key_uri_resolves_against_full_playlist_url(self):
        for uri, expected in (
            ('../keys/key.bin?token=secret', 'https://stream.example/shows/keys/key.bin?token=secret'),
            ('/keys/key.bin?token=secret', 'https://stream.example/keys/key.bin?token=secret'),
            ('?key=secret', 'https://stream.example/shows/episode/index.m3u8?key=secret'),
        ):
            with self.subTest(uri=uri):
                self.downloader._download_segment.reset_mock()
                self.start('#EXT-X-KEY:METHOD=AES-128,URI="' + uri + '"')
                self.downloader._download_segment.assert_called_once_with(expected)

    def test_key_and_map_are_both_downloaded_and_rewritten_preserving_attributes(self):
        self.start(
            '#EXT-X-KEY:METHOD=AES-128,URI="keys/key.bin?auth=key",KEYFORMAT="identity"\n'
            '#EXT-X-MAP:URI="/init/init.mp4?auth=map",BYTERANGE="128@0"')
        self.assertCountEqual(
            [call.args[0] for call in self.downloader._download_segment.call_args_list],
            ['https://stream.example/shows/episode/keys/key.bin?auth=key',
             'https://stream.example/init/init.mp4?auth=map'])
        content = self.rewritten()
        directory = Path(self.directory.name).as_posix()
        self.assertIn(f'#EXT-X-KEY:METHOD=AES-128,URI="{directory}/key.bin",KEYFORMAT="identity"', content)
        self.assertIn(f'#EXT-X-MAP:URI="{directory}/init.mp4",BYTERANGE="128@0"', content)
        self.assertEqual(re.findall(r'URI="([^"]+)"', content), [f'{directory}/key.bin', f'{directory}/init.mp4'])
        self.assertNotIn('auth=', content)
        for filename in ('key.bin', 'init.mp4'):
            self.assertTrue((Path(self.directory.name) / filename).is_file())
        self.downloader._multi_threaded_download.assert_called_once()
        self.downloader._save_as_video.assert_called_once_with()

    def test_method_none_does_not_fetch_a_key(self):
        self.start('#EXT-X-KEY:METHOD=NONE,URI="unused.key?token=secret"')
        self.downloader._download_segment.assert_not_called()
        self.downloader._multi_threaded_download.assert_called_once()
        self.downloader._save_as_video.assert_called_once_with()

    def test_required_auxiliary_failure_stops_before_media_and_ffmpeg(self):
        for tag in ('#EXT-X-KEY:METHOD=AES-128,URI="key.bin"',
                    '#EXT-X-MAP:URI="init.mp4"'):
            with self.subTest(tag=tag):
                self.downloader._download_segment.reset_mock()
                self.downloader._download_segment.side_effect = None
                self.downloader._download_segment.return_value = ('ERROR: fixture failure', 0)
                try:
                    result = self.start(tag)
                except Exception:
                    # Both raising and returning a failure are valid stop contracts.
                    pass
                else:
                    self.assertNotEqual(result[0], 0)
                self.downloader._download_segment.assert_called_once()
                self.downloader._multi_threaded_download.assert_not_called()
                self.downloader._save_as_video.assert_not_called()
                self.downloader._save_as_audio.assert_not_called()
                self.downloader._exec_cmd.assert_not_called()


if __name__ == '__main__':
    unittest.main()
