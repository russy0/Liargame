from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from enum import Enum
import random
import re
import secrets
import unicodedata


class Phase(str, Enum):
    READY = "준비"
    DISCUSSION = "토론"
    VOTE = "투표"
    GUESS = "추측"
    ENDED = "종료"


class Winner(str, Enum):
    CITIZENS = "시민"
    LIARS = "라이어"


@dataclass(frozen=True)
class Player:
    user_id: int
    name: str
    is_liar: bool = False


@dataclass
class VoteResult:
    target: Player | None
    tied: bool
    no_votes: bool
    vote_counts: dict[int | None, int] = field(default_factory=dict)


@dataclass
class GuessResult:
    guess: str
    correct: bool
    winner: Winner


class LiarGame:
    def __init__(
        self,
        players: list[tuple[int, str]],
        word: str,
        category: str,
        liar_count: int = 1,
        *,
        aliases: list[str] | None = None,
        rng: random.Random | None = None,
    ) -> None:
        self._rng = rng if rng is not None else secrets.SystemRandom()
        self._validate(players, word, liar_count)

        shuffled_players = players[:]
        self._rng.shuffle(shuffled_players)
        liar_ids = {user_id for user_id, _ in shuffled_players[:liar_count]}

        self.players = [
            Player(user_id=user_id, name=name, is_liar=user_id in liar_ids)
            for user_id, name in shuffled_players
        ]
        self.word = word.strip()
        self.category = category.strip() or "미분류"
        self.aliases = [alias.strip() for alias in aliases or [] if alias.strip()]
        self.phase = Phase.READY
        self.winner: Winner | None = None
        self.votes: dict[int, int | None] = {}
        self.accused_liar_id: int | None = None
        self.last_vote_result: VoteResult | None = None
        self.last_guess_result: GuessResult | None = None
        self.revote_used: bool = False

    @staticmethod
    def _validate(players: list[tuple[int, str]], word: str, liar_count: int) -> None:
        if len(players) < 3:
            raise ValueError("라이어게임은 최소 3명이 필요합니다.")
        if liar_count < 1:
            raise ValueError("라이어는 최소 1명이어야 합니다.")
        if liar_count >= len(players):
            raise ValueError("라이어 수는 참가자 수보다 적어야 합니다.")
        if not word.strip():
            raise ValueError("제시어가 비어 있습니다.")
        user_ids = [user_id for user_id, _ in players]
        if len(set(user_ids)) != len(user_ids):
            raise ValueError("중복 참가자가 있습니다.")

    def start_discussion(self) -> None:
        self._ensure_phase(Phase.READY)
        self.phase = Phase.DISCUSSION

    def start_vote(self) -> None:
        if self.phase not in {Phase.READY, Phase.DISCUSSION}:
            raise ValueError("투표를 시작할 수 없는 상태입니다.")
        self.phase = Phase.VOTE
        self.votes.clear()
        self.last_vote_result = None

    def submit_vote(self, voter_id: int, target_id: int | None) -> None:
        self._ensure_phase(Phase.VOTE)
        if not self.get_player(voter_id):
            raise ValueError("참가자만 투표할 수 있습니다.")
        if target_id is not None and not self.get_player(target_id):
            raise ValueError("투표 대상이 참가자가 아닙니다.")
        self.votes[voter_id] = target_id

    def resolve_vote(self) -> VoteResult:
        self._ensure_phase(Phase.VOTE)
        if not self.votes:
            result = VoteResult(target=None, tied=False, no_votes=True)
            self._finish(Winner.LIARS)
            self.last_vote_result = result
            return result

        counts = Counter(self.votes.values())
        top_count = max(counts.values())
        top_targets = [target_id for target_id, count in counts.items() if count == top_count]
        if len(top_targets) != 1:
            result = VoteResult(target=None, tied=True, no_votes=False, vote_counts=dict(counts))
            self.last_vote_result = result
            if not self.revote_used:
                self.revote_used = True
                self.votes.clear()
                return result
            self._finish(Winner.LIARS)
            return result

        target_id = top_targets[0]
        target = self.get_player(target_id) if target_id is not None else None
        result = VoteResult(target=target, tied=False, no_votes=False, vote_counts=dict(counts))
        self.last_vote_result = result

        if target and target.is_liar:
            self.accused_liar_id = target.user_id
            self.phase = Phase.GUESS
        else:
            self._finish(Winner.LIARS)
        return result

    def resolve_guess(self, guesser_id: int, guess: str) -> GuessResult:
        self._ensure_phase(Phase.GUESS)
        if guesser_id != self.accused_liar_id:
            raise ValueError("지목된 라이어만 제시어를 추측할 수 있습니다.")
        correct = self.is_correct_guess(guess)
        winner = Winner.LIARS if correct else Winner.CITIZENS
        result = GuessResult(guess=guess.strip(), correct=correct, winner=winner)
        self.last_guess_result = result
        self._finish(winner)
        return result

    def force_guess_fail(self) -> GuessResult:
        self._ensure_phase(Phase.GUESS)
        result = GuessResult(guess="", correct=False, winner=Winner.CITIZENS)
        self.last_guess_result = result
        self._finish(Winner.CITIZENS)
        return result

    def is_correct_guess(self, guess: str) -> bool:
        normalized_guess = normalize_answer(guess)
        answers = [self.word, *self.aliases]
        return any(normalized_guess == normalize_answer(answer) for answer in answers)

    def get_player(self, user_id: int | None) -> Player | None:
        if user_id is None:
            return None
        return next((player for player in self.players if player.user_id == user_id), None)

    def liars(self) -> list[Player]:
        return [player for player in self.players if player.is_liar]

    def citizens(self) -> list[Player]:
        return [player for player in self.players if not player.is_liar]

    def all_votes_submitted(self) -> bool:
        return len(self.votes) >= len(self.players)

    def secret_text_for(self, player: Player) -> str:
        if player.is_liar:
            return (
                "당신은 **라이어**입니다.\n"
                f"주제는 **{self.category}**입니다.\n"
                "다른 사람의 발언에서 제시어를 추리하세요."
            )
        return (
            "당신은 **시민**입니다.\n"
            f"주제: **{self.category}**\n"
            f"제시어: **{self.word}**\n"
            "라이어가 눈치채지 못하게 자연스럽게 설명하세요."
        )

    def reveal_text(self) -> str:
        liar_names = ", ".join(player.name for player in self.liars())
        rows = [f"제시어: **{self.word}**", f"주제: **{self.category}**", f"라이어: **{liar_names}**"]
        return "\n".join(rows)

    def vote_summary_text(self) -> str:
        if not self.last_vote_result or not self.last_vote_result.vote_counts:
            return "투표 없음"
        rows: list[tuple[str, int]] = []
        for target_id, count in self.last_vote_result.vote_counts.items():
            target = self.get_player(target_id)
            name = target.name if target else "스킵"
            rows.append((name, count))
        rows.sort(key=lambda item: (-item[1], item[0].casefold()))
        return "\n".join(f"- {name}: {count}표" for name, count in rows)

    def _finish(self, winner: Winner) -> None:
        self.winner = winner
        self.phase = Phase.ENDED

    def _ensure_phase(self, expected: Phase) -> None:
        if self.phase != expected:
            raise ValueError(f"현재 상태에서는 사용할 수 없습니다. 현재 상태: {self.phase.value}")


def normalize_answer(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value).casefold()
    return re.sub(r"[\s\W_]+", "", normalized, flags=re.UNICODE)
