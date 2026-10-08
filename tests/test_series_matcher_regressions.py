import unittest

from Clients.SeriesMatcher import SeriesMatcher


class SeriesMatcherRegressionTests(unittest.TestCase):
    def setUp(self):
        self.matcher = SeriesMatcher()

    def test_shared_prefix_does_not_match_different_drama(self):
        series = {'title': 'My Boss i love You', 'year': 2026, 'countryCode': 'TH'}
        result = {'title': 'My Boss, My Love, My Stepbrother!', 'year': '2026',
                  'country': 'Thailand', 'description': ''}
        self.assertFalse(self.matcher.is_qualified(series, result, 0.67))

    def test_exact_title_still_matches(self):
        series = {'title': 'Pls. Love', 'year': 2026, 'countryCode': 'TH'}
        result = {'title': 'Pls Love (2026)', 'year': '2026',
                  'country': 'Thailand', 'description': ''}
        self.assertTrue(self.matcher.is_qualified(series, result, 1.0))

    def test_synopsis_can_confirm_localized_title(self):
        series = {'title': 'Player: The Series', 'year': 2024, 'countryCode': 'TH'}
        result = {'title': 'Player (uncut)', 'year': '2024',
                  'country': 'Thailand',
                  'description': 'A player must navigate love, family, and rivalry.'}
        self.assertTrue(self.matcher.is_qualified(
            series, result, 0.65,
            'A player must navigate love, family, and rivalry.'))


if __name__ == '__main__':
    unittest.main()
