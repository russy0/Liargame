import asyncio
import unittest

import bot


class CategoryMatchingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.original_config = bot.config
        bot.config = bot.BotConfig(
            word_bank={
                "한국 음식": ["김치"],
                "세계 도시": ["서울"],
                "화학 원소": ["수소"],
            }
        )

    def tearDown(self) -> None:
        bot.config = self.original_config

    def test_resolve_category_ignores_spacing(self) -> None:
        self.assertEqual(bot.resolve_category_name("한국음식"), "한국 음식")
        self.assertEqual(bot.resolve_category_name("세계도시"), "세계 도시")

    def test_choose_word_uses_resolved_category(self) -> None:
        word, category = bot.choose_word("화학원소", None)

        self.assertEqual(word, "수소")
        self.assertEqual(category, "화학 원소")

    def test_category_autocomplete_uses_normalized_search(self) -> None:
        async def run() -> list[str]:
            choices = await bot.category_autocomplete(None, "한국음")
            return [choice.value for choice in choices]

        self.assertEqual(asyncio.run(run()), ["한국 음식"])


if __name__ == "__main__":
    unittest.main()
