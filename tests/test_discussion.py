import unittest

import bot
from game import LiarGame


class DiscussionControlsTests(unittest.TestCase):
    def test_speech_done_message_ignores_spacing(self) -> None:
        self.assertTrue(bot.is_speech_done_message("발언완료"))
        self.assertTrue(bot.is_speech_done_message("발언 완료"))
        self.assertFalse(bot.is_speech_done_message("완료"))

    def test_complete_current_speech_requires_current_speaker(self) -> None:
        game = LiarGame([(1, "Alpha"), (2, "Bravo"), (3, "Charlie")], "김치", "음식")
        game.start_discussion()
        running = bot.RunningGame(
            guild_id=1,
            channel_id=1,
            host_user_id=1,
            participant_role_id=1,
            game=game,
        )
        running.discussion_current_speaker_id = 1

        self.assertFalse(bot.complete_current_speech(running, 2))
        self.assertFalse(running.discussion_speech_done_event.is_set())
        self.assertTrue(bot.complete_current_speech(running, 1))
        self.assertTrue(running.discussion_speech_done_event.is_set())


if __name__ == "__main__":
    unittest.main()
