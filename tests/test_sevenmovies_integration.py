import ast
import logging
import os
from pathlib import Path
import tempfile
import types
import unittest
from unittest.mock import Mock, patch


SOURCE = Path(__file__).resolve().parents[1] / 'udb_sonarr.py'


def daemon_type():
    tree = ast.parse(SOURCE.read_text())
    cls = next(n for n in tree.body if isinstance(n, ast.ClassDef))
    cls.body = [n for n in cls.body if isinstance(n, ast.FunctionDef) and n.name in (
        'find_series_on_clients', '_match_exact_season_episode', 'download_episode')]
    namespace = {'Dict': dict, 'List': list, 'os': os,
                 'colprint': lambda *a, **kw: None, 'DownloadController': Mock}
    exec(compile(ast.Module(body=[cls], type_ignores=[]), str(SOURCE), 'exec'), namespace)
    return namespace['UDBSonarrDaemon']


class SevenMoviesIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.daemon = daemon_type()()
        self.daemon.logger = logging.getLogger(__name__)
        self.daemon.tmdb_client = None
        self.daemon._enrich_series_origin = lambda series: series
        self.daemon._is_split_episodes = lambda episodes: False
        self.daemon.matcher = Mock()
        self.client = Mock()
        self.daemon.site_clients = {'7movies': self.client}
        self.daemon.site_client_names = ['7movies']

    def test_id_lookup_ignores_country_and_title_scoring(self):
        series = {'tmdbId': 42, 'country': 'KR', 'seasons': [{'seasonNumber': 1}, {'seasonNumber': 2}]}
        self.client.lookup_series.return_value = {'title': 'Unrelated title', 'country': 'US'}
        self.client.fetch_episodes_list.return_value = []
        result = self.daemon.find_series_on_clients('Different title', series)
        self.client.lookup_series.assert_called_once_with(series, tmdb_client=None)
        self.assertEqual(result[2]['sonarr_seasons'], [1, 2])
        self.assertEqual(result[3], [])
        self.client.search.assert_not_called()
        self.daemon.matcher.score_all_results.assert_not_called()

    def test_missing_id_result_does_not_fuzzy_fallback(self):
        self.client.lookup_series.return_value = None
        self.assertIsNone(self.daemon.find_series_on_clients('Title', {}))
        self.client.search.assert_not_called()

    def test_season_collision_and_no_flat_offset(self):
        episodes = [{'season': 1, 'episode': 1}, {'seasonNumber': 2, 'episode': 1}]
        selected = self.daemon._match_exact_season_episode({'seasonNumber': 2, 'episodeNumber': 1}, episodes)
        self.assertIs(selected, episodes[1])
        self.assertIsNone(self.daemon._match_exact_season_episode({'seasonNumber': 3, 'episodeNumber': 1}, episodes))

    def test_selected_episode_only_unknown_resolution_and_referer(self):
        selected = {'season': 2, 'episode': 1}
        self.client.fetch_episode_links.return_value = {1: {'unknown': {
            'downloadLink': 'https://cdn.test/video', 'downloadType': 'hls',
            'referer': 'https://player.test/exact'}}}
        self.client.udb_episode_dict = {}
        self.daemon.qualities = ['720']
        self.daemon.downloader_config = {}
        self.daemon.puid, self.daemon.pgid = os.getuid(), os.getgid()
        self.daemon.sonarr = Mock()
        self.daemon.sonarr.get_episode_filename.return_value = 'Show.S02E01.mp4'
        for backend, module_name, class_name in (
                ('udb', 'Utils.HLSDownloader', 'HLSDownloader'),
                ('yt-dlp', 'Utils.YtDlpDownloader', 'YtDlpDownloader')):
            with self.subTest(backend=backend), tempfile.TemporaryDirectory() as folder:
                factory = Mock()
                factory.return_value.start_download.return_value = (0, 'ok')
                module = types.ModuleType(module_name)
                setattr(module, class_name, factory)
                self.daemon.downloader_type = backend
                with patch.dict('sys.modules', {module_name: module}):
                    self.assertTrue(self.daemon.download_episode(
                        self.client, {}, {'seasonNumber': 2}, selected, {},
                        [{'season': 1, 'episode': 1}, selected], folder))
                self.assertEqual(self.client.fetch_episode_links.call_args.args[0], [selected])
                config, details = factory.call_args.args
                self.assertEqual(details['refererLink'], 'https://player.test/exact')
                if backend == 'yt-dlp':
                    self.assertEqual(config['referer'], 'https://player.test/exact')
                    self.assertEqual(config['quality'], 720)
                self.client._resolution_selector.assert_not_called()

    def test_all_defaults_and_registry(self):
        tree = ast.parse(SOURCE.read_text())
        cls = next(n for n in tree.body if isinstance(n, ast.ClassDef))
        registry = next(n for n in cls.body if isinstance(n, ast.Assign)
                        and any(isinstance(t, ast.Name) and t.id == 'CLIENT_REGISTRY' for t in n.targets))
        self.assertEqual(ast.literal_eval(registry.value)['7movies']['class_name'], 'SevenMoviesClient')
        defaults = [ast.literal_eval(n.value) for n in ast.walk(cls)
                    if isinstance(n, ast.Assign) and isinstance(n.value, ast.List)
                    and all(isinstance(e, ast.Constant) for e in n.value.elts)
                    and any(isinstance(t, ast.Attribute) and t.attr == 'site_client_names' for t in n.targets)]
        self.assertIn(['kisskh', 'animepahe', 'asiaflix'], defaults)
        self.assertTrue(all('7movies' not in value for value in defaults))


if __name__ == '__main__':
    unittest.main()
