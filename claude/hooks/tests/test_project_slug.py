import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))   # runnable alone from the repo root (CI runs one file per job)

from lib.handoff_check import project_slug  # noqa: E402


class ProjectSlugTest(unittest.TestCase):
    """The slug must match the folder name the app actually writes under ~/.claude/projects."""

    def test_slashes_become_hyphens(self):
        self.assertEqual(project_slug("/Users/dom/Developer/frankendom-combat"),
                         "Users-dom-Developer-frankendom-combat")

    def test_spaces_become_hyphens_too(self):
        # The Stats lane sat in "Frankendom - Stats"; replacing only "/" left spaces in the key, so it
        # matched no directory and the receipt named an empty store while the real one was elsewhere.
        self.assertEqual(project_slug("/Users/dom/Developer/Frankendom - Stats"),
                         "Users-dom-Developer-Frankendom---Stats")

    def test_multi_word_folder(self):
        self.assertEqual(project_slug("/Users/dom/Desktop/Business/Research Agent Bot"),
                         "Users-dom-Desktop-Business-Research-Agent-Bot")

    def test_no_space_or_slash_is_unchanged(self):
        self.assertEqual(project_slug("plain"), "plain")


if __name__ == "__main__":
    unittest.main()
