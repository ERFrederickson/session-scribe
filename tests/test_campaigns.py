import tempfile
import unittest
from pathlib import Path

from session_scribe.config import Config
from session_scribe.controller import Controller


class CampaignTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        cfg = Config(Path(self.tmp.name) / "config.json")
        cfg.settings.data_dir = str(Path(self.tmp.name) / "data")
        cfg.settings.last_campaign = "Mythropolis"
        self.c = Controller(cfg)
        self.c.campaign = "Mythropolis"
        # Users seen in voice earlier
        cfg.remember_user("kira", "Kira", "kira1", False)
        cfg.remember_user("avery", "Avery", "avery", True)

    def tearDown(self):
        self.tmp.cleanup()

    def test_switching_campaign_swaps_characters(self):
        c = self.c
        self.assertIsNone(c.create_campaign("Sad Robots"))
        self.assertIsNotNone(c.create_campaign("Sad Robots"))  # duplicate
        c.set_campaign_tag("Mythropolis", "kira", character="Ash")
        c.set_campaign_tag("Sad Robots", "kira", character="Unit 7")
        c.voice["kira"] = {"id": "kira", "name": "Kira", "username": "kira1", "is_bot": False}

        self.assertEqual(c.state()["participants"][0]["character"], "Ash")
        c.set_campaign("Sad Robots")
        self.assertEqual(c.state()["participants"][0]["character"], "Unit 7")

    def test_offline_bot_defaults_to_ignored(self):
        self.c.set_campaign_tag("Mythropolis", "avery", character="")
        players = {p["id"]: p for p in self.c.campaigns()["campaigns"][0]["players"]}
        self.assertTrue(players["avery"]["ignored"])
        self.assertEqual(players["avery"]["name"], "Avery")

    def test_rename_and_delete(self):
        c = self.c
        c.set_campaign_tag("Mythropolis", "kira", character="Ash")
        self.assertIsNone(c.rename_campaign("Mythropolis", "Mythropolis Act II"))
        self.assertEqual(c.campaign, "Mythropolis Act II")
        self.assertEqual(c.config.settings.last_campaign, "Mythropolis Act II")
        self.assertEqual(c.config.tag("Mythropolis Act II", "kira")["character"], "Ash")

        c.rec_state = "recording"
        self.assertIsNotNone(c.delete_campaign("Mythropolis Act II"))
        self.assertIsNotNone(c.set_campaign("Other"))
        c.rec_state = "idle"
        c.create_campaign("Corbeil")
        self.assertIsNone(c.delete_campaign("Mythropolis Act II"))
        self.assertEqual(c.campaign, "Corbeil")

    def test_remove_player(self):
        c = self.c
        c.set_campaign_tag("Mythropolis", "kira", character="Ash")
        c.remove_campaign_tag("Mythropolis", "kira")
        self.assertEqual(c.campaigns()["campaigns"][0]["players"], [])


if __name__ == "__main__":
    unittest.main()
