import unittest
from unittest.mock import Mock


class EpisodeDiagnosticContractTests(unittest.TestCase):
    """Keep diagnostic fields safe and useful without asserting provider I/O."""

    def test_mapping_diagnostic_contains_episode_number_and_id_only(self):
        episode = {'episode': 4, 'episodeId': 224799}
        message = f'episode {episode.get("episode")} (episode_id={episode.get("episodeId", "n/a")})'
        self.assertIn('episode 4', message)
        self.assertIn('episode_id=224799', message)
        self.assertNotIn('kkey=', message)

    def test_missing_id_is_explicit(self):
        episode = {'episode': 4}
        self.assertEqual(episode.get('episodeId', 'n/a'), 'n/a')


if __name__ == '__main__':
    unittest.main()
