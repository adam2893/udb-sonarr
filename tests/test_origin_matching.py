import ast
import logging
from pathlib import Path
import unittest
from unittest.mock import Mock

from Clients.SeriesMatcher import SeriesMatcher


def daemon_type():
    # Execute only the matching methods: no daemon startup or optional imports.
    tree = ast.parse((Path(__file__).resolve().parents[1] / 'udb_sonarr.py').read_text())
    methods = [node for cls in tree.body if isinstance(cls, ast.ClassDef)
               for node in cls.body if isinstance(node, ast.FunctionDef)
               and node.name in ('_enrich_series_origin', 'find_series_on_clients')]
    namespace = {'SeriesMatcher': SeriesMatcher, 'Dict': dict,
                 'colprint': lambda *args, **kwargs: None}
    exec(compile(ast.Module(body=methods, type_ignores=[]), '<matching methods>', 'exec'), namespace)
    return type('MatchingDaemon', (), {method.name: namespace[method.name] for method in methods})


class OriginMatchingTests(unittest.TestCase):
    def setUp(self):
        self.daemon = daemon_type()()
        self.daemon.logger = logging.getLogger(__name__)
        self.daemon.matcher = SeriesMatcher()
        self.daemon._is_split_episodes = lambda episodes: False
        self.daemon.tmdb_client = Mock()
        self.daemon.tmdb_client.get_series_origin_countries.return_value = ['CN']
        self.daemon.tmdb_client.get_series_aliases.return_value = []
        self.daemon.tmdb_client.get_series_overview.return_value = ''
        self.daemon.site_client_names = ['test']
        self.daemon.site_clients = {'test': Mock()}
        self.series = {'title': 'Chinese Show', 'year': 2025, 'tmdbId': 123,
                       'tvdbId': 456, 'originalLanguage': 'zh'}

    def search(self, country):
        self.daemon.site_clients['test'].search.return_value = {
            1: {'title': 'Chinese Show', 'country': country, 'year': 2025}}
        return self.daemon.find_series_on_clients('Chinese Show', self.series)

    def test_chinese_origin_rejects_korean_result(self):
        self.assertIsNone(self.search('South Korea'))

    def test_chinese_origin_accepts_chinese_result_without_mutation(self):
        original = dict(self.series)
        self.assertIsNotNone(self.search('China'))
        self.assertEqual(self.series, original)
        self.daemon.tmdb_client.get_series_origin_countries.assert_called_once_with(
            tmdb_id=123, tvdb_id=456)

    def test_coproduction_accepts_either_country(self):
        self.daemon.tmdb_client.get_series_origin_countries.return_value = ['CN', 'KR']
        for country in ('China', 'South Korea'):
            with self.subTest(country=country):
                self.assertIsNotNone(self.search(country))

    def test_native_country_takes_priority(self):
        for field in ('country', 'countryCode'):
            with self.subTest(field=field):
                self.series[field] = 'KR'
                self.series['originCountries'] = ['CN']
                self.assertIsNone(self.search('China'))
                self.assertIsNotNone(self.search('South Korea'))
                del self.series[field]
        self.daemon.tmdb_client.get_series_origin_countries.assert_not_called()

    def test_unknown_origin_does_not_guess_from_language(self):
        self.daemon.tmdb_client.get_series_origin_countries.return_value = []
        self.assertIsNotNone(self.search('South Korea'))
        self.daemon.tmdb_client.get_series_origin_countries.side_effect = RuntimeError('unavailable')
        self.assertIsNotNone(self.search('South Korea'))
        self.daemon.tmdb_client = None
        self.assertIsNotNone(self.search('South Korea'))

    def test_origin_scoring_bonus_and_marginal_confirmation(self):
        series = dict(self.series, originCountries=['CN', 'KR'])
        matcher = self.daemon.matcher
        results = {1: {'title': 'Chinese Show', 'year': 2025, 'country': 'China'},
                   2: {'title': 'Chinese Show', 'year': 2025, 'country': 'South Korea'},
                   3: {'title': 'Chinese Show', 'year': 2025, 'country': 'Japan'}}
        scores = {idx: score for score, idx, result, raw in matcher.score_all_results(series, results)}
        self.assertEqual(scores[1], scores[2])
        self.assertGreater(scores[1], scores[3])
        self.assertTrue(matcher.is_qualified(series, results[1], 0.7))
        self.assertFalse(matcher.is_qualified(series, results[3], 0.7))
        self.assertFalse(matcher.is_qualified(series, dict(results[1], country=''), 0.7))


if __name__ == '__main__':
    unittest.main()
