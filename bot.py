from __future__ import annotations

import asyncio
from contextlib import suppress
from dataclasses import dataclass, field, fields
import json
import os
from pathlib import Path
import random
import time

import discord
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv

from game import LiarGame, Phase, Player, VoteResult, Winner, normalize_answer


BASE_DIR = Path(__file__).resolve().parent
CONFIG_FILE = BASE_DIR / "config.json"
STATS_FILE = BASE_DIR / "liar_stats.json"

RECRUITMENT_SECONDS_FALLBACK = 60
MAX_GAME_PLAYERS = 24
GAME_NOTIFICATION_ROLE = "게임알림"
CONTINUE_VOTE_SECONDS = 30

DEFAULT_EMBED_COLOR = discord.Color.gold()
ERROR_EMBED_COLOR = discord.Color.red()
SUCCESS_EMBED_COLOR = discord.Color.green()
WARNING_EMBED_COLOR = discord.Color.orange()

DEFAULT_WORD_BANK = {
    "음식": ["김치", "떡볶이", "라면", "비빔밥", "피자", "햄버거", "초밥", "만두", "아이스크림", "샌드위치"],
    "장소": ["학교", "도서관", "병원", "영화관", "카페", "공항", "지하철역", "편의점", "놀이공원", "박물관"],
    "물건": ["우산", "스마트폰", "노트북", "지갑", "시계", "이어폰", "책가방", "칫솔", "냉장고", "카메라"],
    "직업": ["의사", "경찰", "선생님", "요리사", "기자", "변호사", "개발자", "디자이너", "가수", "배우"],
    "스포츠": ["축구", "농구", "야구", "배드민턴", "테니스", "볼링", "수영", "스키", "탁구", "골프"],
}


@dataclass
class BotConfig:
    game_enabled: bool = True
    participant_role: str = "라이어게임 참가자"
    manager_role: str = "관리자"
    default_liar_count: int = 1
    min_player_count: int = 3
    max_player_count: int = 12
    recruitment_seconds: int = 60
    discussion_seconds: int = 180
    speech_seconds: int = 25
    discussion_extension_seconds: int = 60
    max_discussion_extensions: int = 2
    vote_seconds: int = 45
    guess_seconds: int = 30
    chat_slowmode_seconds: int = 3
    continue_vote_enabled: bool = True
    word_bank: dict[str, list[str]] = field(default_factory=lambda: dict(DEFAULT_WORD_BANK))


@dataclass
class RunningGame:
    guild_id: int
    channel_id: int
    host_user_id: int
    participant_role_id: int
    game: LiarGame
    task: asyncio.Task[None] | None = None
    vote_complete_event: asyncio.Event = field(default_factory=asyncio.Event)
    participant_user_ids: set[int] = field(default_factory=set)
    original_slowmode_delay: int | None = None
    discussion_current_speaker_id: int | None = None
    discussion_current_speaker_name: str = ""
    discussion_speech_done_event: asyncio.Event = field(default_factory=asyncio.Event)
    started_at: float = field(default_factory=time.monotonic)
    stats_recorded: bool = False
    liar_count: int = 1
    round_number: int = 1
    session_scores: dict[int, int] = field(default_factory=dict)


config = BotConfig()
games: dict[int, RunningGame] = {}
recruiting: dict[int, "JoinGameView"] = {}


def load_config() -> BotConfig:
    value = BotConfig()
    if CONFIG_FILE.exists():
        try:
            data = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
        except json.JSONDecodeError as error:
            raise RuntimeError(f"config.json 파싱 실패: {error}") from error
        known_fields = {field.name for field in fields(BotConfig)}
        for key, item in data.items():
            if key in known_fields:
                setattr(value, key, item)
    sanitize_config(value)
    return value


def sanitize_config(value: BotConfig) -> None:
    value.default_liar_count = max(1, int(value.default_liar_count))
    value.min_player_count = max(3, int(value.min_player_count))
    value.max_player_count = int(value.max_player_count)
    if value.max_player_count <= 0:
        value.max_player_count = MAX_GAME_PLAYERS
    value.max_player_count = max(value.min_player_count, min(MAX_GAME_PLAYERS, value.max_player_count))
    value.default_liar_count = min(value.default_liar_count, value.max_player_count - 1)
    value.recruitment_seconds = max(10, int(value.recruitment_seconds))
    value.discussion_seconds = max(10, int(value.discussion_seconds))
    value.speech_seconds = max(5, int(value.speech_seconds))
    value.discussion_extension_seconds = max(10, int(value.discussion_extension_seconds))
    value.max_discussion_extensions = max(0, int(value.max_discussion_extensions))
    value.vote_seconds = max(10, int(value.vote_seconds))
    value.guess_seconds = max(10, int(value.guess_seconds))
    value.chat_slowmode_seconds = max(0, int(value.chat_slowmode_seconds))
    if not isinstance(value.word_bank, dict) or not value.word_bank:
        value.word_bank = dict(DEFAULT_WORD_BANK)
    clean_bank: dict[str, list[str]] = {}
    for category, words in value.word_bank.items():
        if not isinstance(category, str) or not isinstance(words, list):
            continue
        clean_words = [str(word).strip() for word in words if str(word).strip()]
        if clean_words:
            clean_bank[category.strip() or "미분류"] = clean_words
    value.word_bank = clean_bank or dict(DEFAULT_WORD_BANK)


def config_to_dict(value: BotConfig) -> dict[str, object]:
    return {field.name: getattr(value, field.name) for field in fields(BotConfig)}


def save_config() -> None:
    sanitize_config(config)
    CONFIG_FILE.write_text(
        json.dumps(config_to_dict(config), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def load_stats() -> dict[str, dict[str, object]]:
    if not STATS_FILE.exists():
        return {}
    try:
        data = json.loads(STATS_FILE.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def save_stats(stats: dict[str, dict[str, object]]) -> None:
    STATS_FILE.write_text(json.dumps(stats, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def default_player_stats(name: str) -> dict[str, object]:
    return {
        "name": name,
        "games": 0,
        "wins": 0,
        "liar_games": 0,
        "liar_wins": 0,
        "citizen_games": 0,
        "citizen_wins": 0,
        "correct_votes": 0,
        "caught_as_liar": 0,
        "successful_guesses": 0,
    }


def ensure_player_stats(stats: dict[str, dict[str, object]], user_id: int, name: str) -> dict[str, object]:
    key = str(user_id)
    entry = stats.get(key)
    if not isinstance(entry, dict):
        entry = default_player_stats(name)
        stats[key] = entry
    for stat_key, default_value in default_player_stats(name).items():
        entry.setdefault(stat_key, default_value)
    entry["name"] = name
    return entry


def add_stat(entry: dict[str, object], key: str, amount: int = 1) -> None:
    entry[key] = int(entry.get(key, 0)) + amount


def player_won_game(player: Player, winner: Winner) -> bool:
    return (player.is_liar and winner == Winner.LIARS) or (not player.is_liar and winner == Winner.CITIZENS)


def update_session_scores(running: RunningGame) -> None:
    game = running.game
    if not game.winner:
        return
    for player in game.players:
        running.session_scores.setdefault(player.user_id, 0)
        if player_won_game(player, game.winner):
            running.session_scores[player.user_id] += 1


def session_scores_text(running: RunningGame) -> str:
    name_map = {p.user_id: p.name for p in running.game.players}
    rows = sorted(
        running.session_scores.items(),
        key=lambda item: (-item[1], name_map.get(item[0], "").casefold()),
    )
    return "\n".join(
        f"{rank}. **{name_map.get(uid, str(uid))}** {score}점"
        for rank, (uid, score) in enumerate(rows, start=1)
    )


def record_game_stats(running: RunningGame, winner: Winner, vote_result: VoteResult | None) -> None:
    if running.stats_recorded:
        return
    running.stats_recorded = True
    stats = load_stats()
    game = running.game
    liar_ids = {player.user_id for player in game.liars()}
    voted_liar_ids = {
        voter_id
        for voter_id, target_id in game.votes.items()
        if target_id in liar_ids
    }
    caught_liar_id = vote_result.target.user_id if vote_result and vote_result.target and vote_result.target.is_liar else None

    for player in game.players:
        entry = ensure_player_stats(stats, player.user_id, player.name)
        add_stat(entry, "games")
        if player_won_game(player, winner):
            add_stat(entry, "wins")
        if player.is_liar:
            add_stat(entry, "liar_games")
            if winner == Winner.LIARS:
                add_stat(entry, "liar_wins")
            if caught_liar_id == player.user_id:
                add_stat(entry, "caught_as_liar")
            if player.user_id == game.accused_liar_id and game.last_guess_result and game.last_guess_result.correct:
                add_stat(entry, "successful_guesses")
        else:
            add_stat(entry, "citizen_games")
            if winner == Winner.CITIZENS:
                add_stat(entry, "citizen_wins")
            if player.user_id in voted_liar_ids:
                add_stat(entry, "correct_votes")
    save_stats(stats)


def win_rate_text(wins: int, games_count: int) -> str:
    if games_count <= 0:
        return "0.0%"
    return f"{wins / games_count * 100:.1f}%"


def personal_stats_text(user_id: int, fallback_name: str) -> str:
    stats = load_stats()
    entry = ensure_player_stats(stats, user_id, fallback_name)
    games_count = int(entry.get("games", 0))
    wins = int(entry.get("wins", 0))
    return (
        f"플레이어: **{entry.get('name', fallback_name)}**\n"
        f"전체: {games_count}전 {wins}승 ({win_rate_text(wins, games_count)})\n"
        f"라이어: {entry.get('liar_games', 0)}전 {entry.get('liar_wins', 0)}승\n"
        f"시민: {entry.get('citizen_games', 0)}전 {entry.get('citizen_wins', 0)}승\n"
        f"라이어 적중 투표: {entry.get('correct_votes', 0)}회\n"
        f"제시어 추측 성공: {entry.get('successful_guesses', 0)}회"
    )


def leaderboard_value(entry: dict[str, object], metric: str) -> float:
    games_count = int(entry.get("games", 0))
    wins = int(entry.get("wins", 0))
    if metric == "wins":
        return float(wins)
    if metric == "win_rate":
        return wins / games_count if games_count else 0.0
    if metric == "correct_votes":
        return float(entry.get("correct_votes", 0))
    if metric == "successful_guesses":
        return float(entry.get("successful_guesses", 0))
    return float(games_count)


def leaderboard_text(metric: str) -> str:
    stats = load_stats()
    rows = [
        (str(user_id), entry)
        for user_id, entry in stats.items()
        if isinstance(entry, dict) and int(entry.get("games", 0)) > 0
    ]
    if not rows:
        return "아직 전적이 없습니다."
    rows.sort(key=lambda item: (-leaderboard_value(item[1], metric), str(item[1].get("name", "")).casefold()))
    lines = []
    for rank, (_, entry) in enumerate(rows[:10], start=1):
        games_count = int(entry.get("games", 0))
        wins = int(entry.get("wins", 0))
        value = leaderboard_value(entry, metric)
        if metric == "win_rate":
            value_text = win_rate_text(wins, games_count)
        else:
            value_text = str(int(value))
        lines.append(
            f"{rank}. **{entry.get('name', '알 수 없음')}** - {value_text} "
            f"({games_count}전 {wins}승)"
        )
    return "\n".join(lines)


def display_name(member: discord.abc.User) -> str:
    if isinstance(member, discord.Member):
        return member.display_name
    return member.name


def duration_text(seconds: int) -> str:
    minutes, remain = divmod(seconds, 60)
    if minutes and remain:
        return f"{minutes}분 {remain}초"
    if minutes:
        return f"{minutes}분"
    return f"{remain}초"


def make_embed(
    description: str,
    *,
    title: str = "라이어게임",
    color: discord.Color = DEFAULT_EMBED_COLOR,
) -> discord.Embed:
    return discord.Embed(title=title, description=description, color=color)


async def send_interaction_reply(
    interaction: discord.Interaction,
    description: str,
    *,
    title: str = "라이어게임",
    color: discord.Color = DEFAULT_EMBED_COLOR,
    private: bool = False,
    view: discord.ui.View | None = None,
) -> None:
    embed = make_embed(description, title=title, color=color)
    kwargs: dict[str, object] = {"embed": embed, "ephemeral": private}
    if view is not None:
        kwargs["view"] = view
    if interaction.response.is_done():
        await interaction.followup.send(**kwargs)
    else:
        await interaction.response.send_message(**kwargs)


async def send_embed(
    channel: discord.abc.Messageable,
    description: str,
    *,
    title: str = "라이어게임",
    color: discord.Color = DEFAULT_EMBED_COLOR,
    view: discord.ui.View | None = None,
) -> discord.Message | None:
    try:
        kwargs: dict[str, object] = {"embed": make_embed(description, title=title, color=color)}
        if view is not None:
            kwargs["view"] = view
        return await channel.send(**kwargs)
    except discord.DiscordException:
        return None


def member_has_role(member: discord.Member, role_name: str) -> bool:
    return any(role.name == role_name for role in member.roles)


def is_manager(interaction: discord.Interaction) -> bool:
    if not isinstance(interaction.user, discord.Member):
        return False
    if interaction.user.guild_permissions.administrator:
        return True
    return member_has_role(interaction.user, config.manager_role)


async def require_manager_reply(interaction: discord.Interaction) -> bool:
    if is_manager(interaction):
        return True
    await send_interaction_reply(
        interaction,
        f"`{config.manager_role}` 역할 또는 서버 관리자 권한이 필요합니다.",
        color=ERROR_EMBED_COLOR,
        private=True,
    )
    return False


def disable_view_items(view: discord.ui.View | None) -> None:
    if not view:
        return
    for item in view.children:
        if hasattr(item, "disabled"):
            item.disabled = True


def effective_max_player_count() -> int:
    return min(MAX_GAME_PLAYERS, max(config.min_player_count, config.max_player_count))


def matching_categories(search: str | None = None) -> list[str]:
    categories = sorted(config.word_bank.keys())
    if not search:
        return categories
    needle = normalize_answer(search)
    if not needle:
        return categories
    return [
        category
        for category in categories
        if needle in normalize_answer(category)
    ]


def resolve_category_name(category: str) -> str | None:
    clean_category = category.strip()
    if not clean_category:
        return None
    normalized_category = normalize_answer(clean_category)
    for candidate in config.word_bank:
        if normalize_answer(candidate) == normalized_category:
            return candidate
    matches = matching_categories(clean_category)
    if len(matches) == 1:
        return matches[0]
    return None


async def category_autocomplete(
    interaction: discord.Interaction,
    current: str,
) -> list[app_commands.Choice[str]]:
    matches = matching_categories(current)
    return [
        app_commands.Choice(name=f"{category} ({len(config.word_bank[category])}개)"[:100], value=category)
        for category in matches[:25]
    ]


def current_settings_text(prefix: str = "라이어게임 설정") -> str:
    categories = ", ".join(sorted(config.word_bank.keys()))
    return (
        f"{prefix}\n"
        f"참가 역할: `{config.participant_role}`\n"
        f"관리자 역할: `{config.manager_role}`\n"
        f"기본 라이어 수: `{config.default_liar_count}`\n"
        f"인원: `{config.min_player_count}`-`{effective_max_player_count()}`명\n"
        f"모집/토론/투표/추측: `{config.recruitment_seconds}`/`{config.discussion_seconds}`/"
        f"`{config.vote_seconds}`/`{config.guess_seconds}`초\n"
        f"발언/연장: `{config.speech_seconds}`/`{config.discussion_extension_seconds}`초, "
        f"최대 `{config.max_discussion_extensions}`회\n"
        f"토론 슬로우모드: `{config.chat_slowmode_seconds}`초\n"
        f"이어하기 투표: `{'켜짐' if config.continue_vote_enabled else '꺼짐'}`\n"
        f"단어 카테고리: {categories}"
    )


async def ensure_participant_role(guild: discord.Guild) -> discord.Role | None:
    role = discord.utils.get(guild.roles, name=config.participant_role)
    if role:
        return role
    try:
        return await guild.create_role(name=config.participant_role, reason="라이어게임 참가자 역할 생성")
    except discord.DiscordException:
        return None


async def clear_existing_participant_roles(guild: discord.Guild, role: discord.Role) -> list[str]:
    failed: list[str] = []
    for member in list(role.members):
        try:
            await member.remove_roles(role, reason="새 라이어게임 모집 전 참가자 역할 정리")
        except discord.DiscordException:
            failed.append(display_name(member))
    return failed


async def remove_participant_roles(guild: discord.Guild, running: RunningGame) -> None:
    role = guild.get_role(running.participant_role_id)
    if not role:
        return
    for user_id in running.participant_user_ids:
        member = guild.get_member(user_id)
        if not member:
            continue
        with suppress(discord.DiscordException):
            await member.remove_roles(role, reason="라이어게임 종료")


async def set_discussion_slowmode(channel: discord.abc.Messageable, running: RunningGame) -> None:
    if not isinstance(channel, discord.TextChannel):
        return
    if running.original_slowmode_delay is None:  # 첫 라운드에만 원본 저장
        running.original_slowmode_delay = channel.slowmode_delay
    if channel.slowmode_delay == config.chat_slowmode_seconds:
        return
    with suppress(discord.DiscordException):
        await channel.edit(slowmode_delay=config.chat_slowmode_seconds, reason="라이어게임 토론 슬로우모드 적용")


async def restore_slowmode(channel: discord.abc.Messageable, running: RunningGame) -> None:
    if not isinstance(channel, discord.TextChannel) or running.original_slowmode_delay is None:
        return
    if channel.slowmode_delay == running.original_slowmode_delay:
        return
    with suppress(discord.DiscordException):
        await channel.edit(slowmode_delay=running.original_slowmode_delay, reason="라이어게임 종료 후 슬로우모드 복구")


def choose_word(category: str | None, word: str | None) -> tuple[str, str]:
    clean_word = word.strip() if word else ""
    clean_category = category.strip() if category else ""
    if clean_word:
        resolved_category = resolve_category_name(clean_category) if clean_category else None
        return clean_word, resolved_category or clean_category or "직접 입력"
    if clean_category:
        resolved_category = resolve_category_name(clean_category)
        if not resolved_category:
            matches = matching_categories(clean_category)
            hint = f" 비슷한 주제: {', '.join(matches[:5])}" if matches else " `/라이어주제`로 전체 목록을 확인하세요."
            raise ValueError(f"`{clean_category}` 카테고리를 찾을 수 없습니다.{hint}")
        words = config.word_bank[resolved_category]
        if not words:
            raise ValueError(f"`{resolved_category}` 카테고리에 단어가 없습니다.")
        return random.choice(words), resolved_category
    category_name = random.choice(list(config.word_bank.keys()))
    return random.choice(config.word_bank[category_name]), category_name


def participant_text(names: dict[int, str]) -> str:
    if not names:
        return "아직 참가자가 없습니다."
    sorted_names = sorted(names.values(), key=str.casefold)
    return "\n".join(f"{index}. {name}" for index, name in enumerate(sorted_names, start=1))


def game_status_text(running: RunningGame) -> str:
    game = running.game
    voted = len(game.votes)
    total = len(game.players)
    elapsed = int(time.monotonic() - running.started_at)
    players = ", ".join(player.name for player in game.players)
    return (
        f"상태: **{game.phase.value}**\n"
        f"참가자: {total}명\n"
        f"투표: {voted}/{total}명\n"
        f"진행 시간: {duration_text(elapsed)}\n"
        f"목록: {players}"
    )


def is_speech_done_message(content: str) -> bool:
    return normalize_answer(content) == "발언완료"


def complete_current_speech(running: RunningGame, user_id: int) -> bool:
    if running.game.phase != Phase.DISCUSSION:
        return False
    if running.discussion_current_speaker_id != user_id:
        return False
    running.discussion_speech_done_event.set()
    return True


class JoinGameView(discord.ui.View):
    def __init__(
        self,
        guild_id: int,
        host_user_id: int,
        participant_role_id: int,
        liar_count: int,
        min_players: int,
        max_players: int,
        word_category: str,
    ) -> None:
        super().__init__(timeout=config.recruitment_seconds + 5)
        self.guild_id = guild_id
        self.host_user_id = host_user_id
        self.participant_role_id = participant_role_id
        self.liar_count = liar_count
        self.min_players = min_players
        self.max_players = max_players
        self.word_category = word_category
        self.joined_ids: set[int] = set()
        self.joined_names: dict[int, str] = {}
        self.accepting = True
        self.started = False
        self.cancelled = False
        self.done = asyncio.Event()
        self.lock = asyncio.Lock()
        self.message: discord.Message | None = None

    def embed(
        self,
        status: str,
        *,
        title: str = "라이어게임 참가자 모집",
        color: discord.Color = SUCCESS_EMBED_COLOR,
    ) -> discord.Embed:
        remain = self.max_players - len(self.joined_ids)
        return make_embed(
            f"모집 시간: **{duration_text(config.recruitment_seconds)}**\n"
            f"주제: **{self.word_category}**\n"
            f"라이어 수: **{self.liar_count}명**\n"
            f"시작 인원: **{self.min_players}-{self.max_players}명**\n"
            f"남은 자리: **{max(0, remain)}명**\n\n"
            f"현재 참가자 **{len(self.joined_ids)}명**\n"
            f"{participant_text(self.joined_names)}\n\n"
            f"{status}",
            title=title,
            color=color,
        )

    async def refresh_message(
        self,
        status: str = "참가 버튼을 눌러 참가하세요.",
        *,
        title: str = "라이어게임 참가자 모집",
        color: discord.Color = SUCCESS_EMBED_COLOR,
    ) -> None:
        if not self.message:
            return
        with suppress(discord.DiscordException):
            await self.message.edit(embed=self.embed(status, title=title, color=color), view=self)

    async def finish_from_host(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button[discord.ui.View],
        *,
        cancelled: bool,
    ) -> None:
        if interaction.user.id != self.host_user_id:
            await send_interaction_reply(interaction, "주최자만 사용할 수 있습니다.", private=True)
            return
        async with self.lock:
            if not self.accepting:
                await send_interaction_reply(interaction, "모집이 이미 종료되었습니다.", private=True)
                return
            if not cancelled and len(self.joined_ids) < self.min_players:
                await send_interaction_reply(
                    interaction,
                    f"최소 {self.min_players}명이 필요합니다. 현재 {len(self.joined_ids)}명입니다.",
                    private=True,
                )
                return
            self.accepting = False
            self.cancelled = cancelled
            self.started = not cancelled
            disable_view_items(self)
            button.label = "취소 완료" if cancelled else "시작 확정"
            status = "주최자가 모집을 취소했습니다." if cancelled else "주최자가 게임을 시작했습니다."
            title = "모집 취소" if cancelled else "모집 종료"
            color = ERROR_EMBED_COLOR if cancelled else SUCCESS_EMBED_COLOR
            await interaction.response.edit_message(embed=self.embed(status, title=title, color=color), view=self)
            self.done.set()
            self.stop()

    async def cancel_from_manager(self, reason: str) -> None:
        async with self.lock:
            if not self.accepting:
                return
            self.accepting = False
            self.cancelled = True
            disable_view_items(self)
            await self.refresh_message(reason, title="모집 취소", color=ERROR_EMBED_COLOR)
            self.done.set()
            self.stop()

    @discord.ui.button(label="참가", style=discord.ButtonStyle.success)
    async def join_game(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button[discord.ui.View],
    ) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        async with self.lock:
            if not self.accepting:
                await send_interaction_reply(interaction, "모집이 종료되었습니다.", private=True)
                return
            if interaction.guild_id != self.guild_id or not interaction.guild:
                await send_interaction_reply(interaction, "이 모집에는 참가할 수 없습니다.", private=True)
                return
            if not isinstance(interaction.user, discord.Member) or interaction.user.bot:
                await send_interaction_reply(interaction, "서버 멤버만 참가할 수 있습니다.", private=True)
                return
            if interaction.user.id in self.joined_ids:
                await send_interaction_reply(interaction, "이미 참가했습니다.", private=True)
                return
            if len(self.joined_ids) >= self.max_players:
                await send_interaction_reply(interaction, "참가 인원이 가득 찼습니다.", private=True)
                return
            role = interaction.guild.get_role(self.participant_role_id)
            if not role:
                await send_interaction_reply(interaction, "참가자 역할을 찾을 수 없습니다.", private=True)
                return
            try:
                if role not in interaction.user.roles:
                    await interaction.user.add_roles(role, reason="라이어게임 참가")
            except discord.DiscordException:
                await send_interaction_reply(
                    interaction,
                    "참가자 역할 부여에 실패했습니다. 봇 역할 순서와 역할 관리 권한을 확인하세요.",
                    color=ERROR_EMBED_COLOR,
                    private=True,
                )
                return
            self.joined_ids.add(interaction.user.id)
            self.joined_names[interaction.user.id] = display_name(interaction.user)
            await send_interaction_reply(interaction, "참가 완료.", color=SUCCESS_EMBED_COLOR, private=True)
            await self.refresh_message()

    @discord.ui.button(label="나가기", style=discord.ButtonStyle.secondary)
    async def leave_game(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button[discord.ui.View],
    ) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        async with self.lock:
            if not self.accepting:
                await send_interaction_reply(interaction, "모집이 종료되었습니다.", private=True)
                return
            if interaction.user.id not in self.joined_ids:
                await send_interaction_reply(interaction, "참가 상태가 아닙니다.", private=True)
                return
            self.joined_ids.remove(interaction.user.id)
            self.joined_names.pop(interaction.user.id, None)
            if interaction.guild and isinstance(interaction.user, discord.Member):
                role = interaction.guild.get_role(self.participant_role_id)
                if role:
                    with suppress(discord.DiscordException):
                        await interaction.user.remove_roles(role, reason="라이어게임 참가 취소")
            await send_interaction_reply(interaction, "참가 취소 완료.", private=True)
            await self.refresh_message()

    @discord.ui.button(label="시작", style=discord.ButtonStyle.primary)
    async def start_now(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button[discord.ui.View],
    ) -> None:
        await self.finish_from_host(interaction, button, cancelled=False)

    @discord.ui.button(label="취소", style=discord.ButtonStyle.danger)
    async def cancel_recruitment(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button[discord.ui.View],
    ) -> None:
        await self.finish_from_host(interaction, button, cancelled=True)


class VoteSelect(discord.ui.Select["VoteView"]):
    def __init__(self, running: RunningGame) -> None:
        options = [
            discord.SelectOption(label=player.name[:100], value=str(player.user_id))
            for player in running.game.players
        ]
        options.append(discord.SelectOption(label="스킵", value="skip"))
        super().__init__(placeholder="라이어라고 생각하는 사람을 선택하세요", min_values=1, max_values=1, options=options)

    async def callback(self, interaction: discord.Interaction) -> None:
        view = self.view
        if not isinstance(view, VoteView):
            await send_interaction_reply(interaction, "투표 UI 상태를 찾을 수 없습니다.", private=True)
            return
        await view.submit_vote(interaction, self.values[0])


class VoteView(discord.ui.View):
    def __init__(self, running: RunningGame) -> None:
        super().__init__(timeout=config.vote_seconds + 5)
        self.running = running
        self.message: discord.Message | None = None
        self.add_item(VoteSelect(running))

    def status_text(self) -> str:
        game = self.running.game
        return (
            f"투표 시간: **{duration_text(config.vote_seconds)}**\n"
            f"현재 투표: **{len(game.votes)}/{len(game.players)}명**\n"
            "동률, 무투표, 스킵 최다 득표는 라이어 승리입니다."
        )

    async def submit_vote(self, interaction: discord.Interaction, raw_target: str) -> None:
        if interaction.guild_id != self.running.guild_id:
            await send_interaction_reply(interaction, "이 게임 투표가 아닙니다.", private=True)
            return
        target_id = None if raw_target == "skip" else int(raw_target)
        try:
            self.running.game.submit_vote(interaction.user.id, target_id)
        except ValueError as error:
            await send_interaction_reply(interaction, str(error), color=ERROR_EMBED_COLOR, private=True)
            return
        await send_interaction_reply(interaction, "투표 완료. 다시 선택하면 투표가 변경됩니다.", private=True)
        if self.message:
            with suppress(discord.DiscordException):
                await self.message.edit(embed=make_embed(self.status_text(), title="라이어 지목 투표"), view=self)
        if self.running.game.all_votes_submitted():
            self.running.vote_complete_event.set()


class GuessModal(discord.ui.Modal, title="제시어 추측"):
    answer = discord.ui.TextInput(label="제시어", placeholder="정답이라고 생각하는 제시어", max_length=100)

    def __init__(self, view: "GuessView") -> None:
        super().__init__()
        self.guess_view = view

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if interaction.user.id != self.guess_view.guesser_id:
            await send_interaction_reply(interaction, "지목된 라이어만 추측할 수 있습니다.", private=True)
            return
        self.guess_view.guess_text = str(self.answer.value)
        disable_view_items(self.guess_view)
        if self.guess_view.message:
            with suppress(discord.DiscordException):
                await self.guess_view.message.edit(view=self.guess_view)
        await send_interaction_reply(interaction, "추측 제출 완료.", private=True)
        self.guess_view.done.set()
        self.guess_view.stop()


class GuessView(discord.ui.View):
    def __init__(self, guesser_id: int) -> None:
        super().__init__(timeout=config.guess_seconds + 5)
        self.guesser_id = guesser_id
        self.guess_text: str | None = None
        self.done = asyncio.Event()
        self.message: discord.Message | None = None

    @discord.ui.button(label="제시어 추측", style=discord.ButtonStyle.primary)
    async def guess_word(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button[discord.ui.View],
    ) -> None:
        if interaction.user.id != self.guesser_id:
            await send_interaction_reply(interaction, "지목된 라이어만 추측할 수 있습니다.", private=True)
            return
        await interaction.response.send_modal(GuessModal(self))


class SpeechTurnView(discord.ui.View):
    def __init__(self, running: RunningGame, speaker: Player) -> None:
        super().__init__(timeout=config.speech_seconds + config.discussion_extension_seconds * config.max_discussion_extensions + 30)
        self.running = running
        self.speaker = speaker
        self.message: discord.Message | None = None

    @discord.ui.button(label="발언 완료", style=discord.ButtonStyle.primary)
    async def finish_speech(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button[discord.ui.View],
    ) -> None:
        if interaction.user.id != self.speaker.user_id:
            await send_interaction_reply(interaction, "현재 발언자만 사용할 수 있습니다.", private=True)
            return
        if not complete_current_speech(self.running, interaction.user.id):
            await send_interaction_reply(interaction, "이미 발언 차례가 끝났습니다.", private=True)
            return
        disable_view_items(self)
        if self.message:
            with suppress(discord.DiscordException):
                await self.message.edit(view=self)
        await send_interaction_reply(interaction, "발언을 완료했습니다.", color=SUCCESS_EMBED_COLOR, private=True)


class DiscussionControlView(discord.ui.View):
    def __init__(self, running: RunningGame) -> None:
        super().__init__(timeout=config.discussion_seconds + config.discussion_extension_seconds * config.max_discussion_extensions + 30)
        self.running = running
        self.message: discord.Message | None = None
        self.current_speaker_id: int | None = None
        self.current_speaker_name = "대기 중"
        self.skip_votes: set[int] = set()
        self.extension_votes: set[int] = set()
        self.extensions_used = 0
        self.speech_done_event = running.discussion_speech_done_event
        self.skip_event = asyncio.Event()
        self.extension_event = asyncio.Event()

    def required_votes(self) -> int:
        return len(self.running.game.players) // 2 + 1

    def participant(self, user_id: int) -> Player | None:
        return self.running.game.get_player(user_id)

    def status_text(self) -> str:
        extension_status = (
            f"{len(self.extension_votes)}/{self.required_votes()}표 "
            f"({self.extensions_used}/{config.max_discussion_extensions}회 사용)"
        )
        return (
            f"현재 발언자: **{self.current_speaker_name}**\n"
            f"발언 완료는 현재 발언자만 누를 수 있습니다.\n\n"
            f"토론 스킵 투표: **{len(self.skip_votes)}/{self.required_votes()}표**\n"
            f"토론 연장 투표: **{extension_status}**"
        )

    def set_current_speaker(self, player: Player) -> None:
        self.current_speaker_id = player.user_id
        self.current_speaker_name = player.name
        self.running.discussion_current_speaker_id = player.user_id
        self.running.discussion_current_speaker_name = player.name
        self.speech_done_event.clear()

    async def refresh_message(self) -> None:
        if not self.message:
            return
        with suppress(discord.DiscordException):
            await self.message.edit(embed=make_embed(self.status_text(), title="토론 진행"), view=self)

    async def add_vote(
        self,
        interaction: discord.Interaction,
        votes: set[int],
        event: asyncio.Event,
        success_message: str,
    ) -> None:
        player = self.participant(interaction.user.id)
        if not player:
            await send_interaction_reply(interaction, "참가자만 누를 수 있습니다.", private=True)
            return
        if interaction.user.id in votes:
            await send_interaction_reply(interaction, "이미 투표했습니다.", private=True)
            return
        votes.add(interaction.user.id)
        if len(votes) >= self.required_votes():
            event.set()
            await send_interaction_reply(interaction, success_message, color=SUCCESS_EMBED_COLOR, private=True)
        else:
            await send_interaction_reply(
                interaction,
                f"투표 완료. 현재 {len(votes)}/{self.required_votes()}표입니다.",
                private=True,
            )
        await self.refresh_message()

    @discord.ui.button(label="발언 완료", style=discord.ButtonStyle.primary)
    async def finish_speech(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button[discord.ui.View],
    ) -> None:
        if not complete_current_speech(self.running, interaction.user.id):
            await send_interaction_reply(interaction, "현재 발언자만 사용할 수 있습니다.", private=True)
            return
        await send_interaction_reply(interaction, "발언을 완료했습니다.", color=SUCCESS_EMBED_COLOR, private=True)

    @discord.ui.button(label="토론 스킵", style=discord.ButtonStyle.danger)
    async def skip_discussion(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button[discord.ui.View],
    ) -> None:
        await self.add_vote(interaction, self.skip_votes, self.skip_event, "과반수로 토론을 스킵합니다.")

    @discord.ui.button(label="토론 연장", style=discord.ButtonStyle.secondary)
    async def extend_discussion(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button[discord.ui.View],
    ) -> None:
        if self.extensions_used >= config.max_discussion_extensions:
            await send_interaction_reply(interaction, "토론 연장 횟수를 모두 사용했습니다.", private=True)
            return
        before = len(self.extension_votes)
        await self.add_vote(interaction, self.extension_votes, self.extension_event, "과반수로 토론을 연장합니다.")
        if len(self.extension_votes) >= self.required_votes() and before < self.required_votes():
            self.extensions_used += 1
            self.extension_votes.clear()
            await self.refresh_message()


class ContinueView(discord.ui.View):
    def __init__(self, running: RunningGame) -> None:
        super().__init__(timeout=CONTINUE_VOTE_SECONDS + 5)
        self.running = running
        self.yes_votes: set[int] = set()
        self.done = asyncio.Event()
        self.message: discord.Message | None = None

    def required_votes(self) -> int:
        return len(self.running.participant_user_ids) // 2 + 1

    def status_text(self) -> str:
        return (
            f"같은 멤버로 다음 라운드를 진행할까요?\n"
            f"찬성: **{len(self.yes_votes)}/{self.required_votes()}표** 필요\n"
            f"**{CONTINUE_VOTE_SECONDS}초** 안에 과반수 찬성 시 이어서 진행합니다."
        )

    @discord.ui.button(label="이어하기 ✅", style=discord.ButtonStyle.success)
    async def vote_continue(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button[discord.ui.View],
    ) -> None:
        if interaction.user.id not in self.running.participant_user_ids:
            await send_interaction_reply(interaction, "게임 참가자만 투표할 수 있습니다.", private=True)
            return
        if interaction.user.id in self.yes_votes:
            await send_interaction_reply(interaction, "이미 투표했습니다.", private=True)
            return
        self.yes_votes.add(interaction.user.id)
        if len(self.yes_votes) >= self.required_votes():
            await send_interaction_reply(interaction, "과반수 달성! 다음 라운드를 시작합니다.", color=SUCCESS_EMBED_COLOR, private=True)
            self.done.set()
            self.stop()
        else:
            remain = self.required_votes() - len(self.yes_votes)
            await send_interaction_reply(
                interaction,
                f"투표 완료. 현재 {len(self.yes_votes)}/{self.required_votes()}표 ({remain}표 더 필요)",
                private=True,
            )
        if self.message:
            with suppress(discord.DiscordException):
                await self.message.edit(embed=make_embed(self.status_text(), title="이어하기 투표"), view=self)


async def run_continue_vote(channel: discord.abc.Messageable, running: RunningGame) -> bool:
    view = ContinueView(running)
    message = await send_embed(channel, view.status_text(), title="이어하기 투표", view=view)
    view.message = message
    try:
        await asyncio.wait_for(view.done.wait(), timeout=CONTINUE_VOTE_SECONDS)
    except asyncio.TimeoutError:
        pass
    disable_view_items(view)
    reached = len(view.yes_votes) >= view.required_votes()
    if message:
        suffix = "\n\n✅ 과반수 달성! 다음 라운드를 시작합니다." if reached else f"\n\n⏰ 시간 초과. ({len(view.yes_votes)}/{view.required_votes()}표) 이어하기가 취소됩니다."
        with suppress(discord.DiscordException):
            await message.edit(
                embed=make_embed(
                    view.status_text() + suffix,
                    title="이어하기 투표",
                    color=SUCCESS_EMBED_COLOR if reached else WARNING_EMBED_COLOR,
                ),
                view=view,
            )
    return reached


class LiarBot(commands.Bot):
    async def setup_hook(self) -> None:
        await self.tree.sync()
        print("Slash commands synced.")


intents = discord.Intents.default()
intents.members = True
intents.message_content = True
bot = LiarBot(command_prefix="!", intents=intents)


@bot.event
async def on_message(message: discord.Message) -> None:
    if message.author.bot or not message.guild:
        return
    running = games.get(message.guild.id)
    if (
        running
        and message.channel.id == running.channel_id
        and is_speech_done_message(message.content)
        and complete_current_speech(running, message.author.id)
    ):
        with suppress(discord.DiscordException):
            await message.add_reaction("✅")
        return
    await bot.process_commands(message)


@bot.event
async def on_ready() -> None:
    print(f"Logged in as {bot.user} ({bot.user.id if bot.user else 'unknown'})")


@bot.tree.command(name="라이어시작", description="라이어게임 참가자를 모집하고 게임을 시작합니다.")
@app_commands.describe(
    제시어="비워두면 config.json 단어장에서 무작위 선택",
    주제="단어장 카테고리 또는 직접 입력 제시어의 주제",
    라이어수="이번 게임 라이어 수",
)
@app_commands.autocomplete(주제=category_autocomplete)
async def start_game(
    interaction: discord.Interaction,
    제시어: str | None = None,
    주제: str | None = None,
    라이어수: int | None = None,
) -> None:
    if not interaction.guild or interaction.guild_id is None or interaction.channel_id is None:
        await send_interaction_reply(interaction, "서버 채널에서만 사용할 수 있습니다.", private=True)
        return
    if not config.game_enabled:
        await send_interaction_reply(interaction, "라이어게임이 비활성화되어 있습니다.", private=True)
        return
    if interaction.guild_id in games:
        await send_interaction_reply(interaction, "이미 진행 중인 게임이 있습니다.", private=True)
        return
    if interaction.guild_id in recruiting:
        await send_interaction_reply(interaction, "이미 참가자를 모집 중입니다.", private=True)
        return

    liar_count = 라이어수 if 라이어수 is not None else config.default_liar_count
    if liar_count < 1:
        await send_interaction_reply(interaction, "라이어 수는 최소 1명입니다.", color=ERROR_EMBED_COLOR, private=True)
        return
    max_players = effective_max_player_count()
    min_players = max(config.min_player_count, liar_count + 1)
    if min_players > max_players:
        await send_interaction_reply(
            interaction,
            "현재 최대 인원으로는 이 라이어 수를 사용할 수 없습니다.",
            color=ERROR_EMBED_COLOR,
            private=True,
        )
        return

    try:
        word, category = choose_word(주제, 제시어)
    except ValueError as error:
        await send_interaction_reply(interaction, str(error), color=ERROR_EMBED_COLOR, private=True)
        return

    await interaction.response.defer(thinking=True)
    role = await ensure_participant_role(interaction.guild)
    if not role:
        await interaction.followup.send(
            embed=make_embed(
                f"`{config.participant_role}` 역할을 찾거나 만들 수 없습니다. 봇 역할 관리 권한을 확인하세요.",
                color=ERROR_EMBED_COLOR,
            ),
            ephemeral=True,
        )
        return

    await clear_existing_participant_roles(interaction.guild, role)
    join_view = JoinGameView(
        interaction.guild.id,
        interaction.user.id,
        role.id,
        liar_count,
        min_players,
        max_players,
        category,
    )
    recruiting[interaction.guild.id] = join_view
    notification_role = discord.utils.get(interaction.guild.roles, name=GAME_NOTIFICATION_ROLE)
    try:
        recruit_message = await interaction.followup.send(
            content=notification_role.mention if notification_role else None,
            embed=join_view.embed("참가 버튼을 눌러 참가하세요."),
            view=join_view,
            allowed_mentions=discord.AllowedMentions(roles=True),
            wait=True,
        )
        join_view.message = recruit_message

        try:
            await asyncio.wait_for(join_view.done.wait(), timeout=config.recruitment_seconds)
        except asyncio.TimeoutError:
            pass

        if join_view.accepting:
            async with join_view.lock:
                join_view.accepting = False
                disable_view_items(join_view)
                if len(join_view.joined_ids) < join_view.min_players:
                    join_view.cancelled = True
                    await join_view.refresh_message(
                        f"최소 {join_view.min_players}명을 채우지 못해 모집을 취소했습니다.",
                        title="모집 취소",
                        color=ERROR_EMBED_COLOR,
                    )
                else:
                    join_view.started = True
                    await join_view.refresh_message("모집 시간이 끝나 게임을 시작합니다.", title="모집 종료")
                join_view.done.set()
                join_view.stop()

        if join_view.cancelled or not join_view.started:
            await remove_recruitment_roles(interaction.guild, join_view)
            return

        players = [(user_id, join_view.joined_names[user_id]) for user_id in join_view.joined_ids]
        game = LiarGame(players, word, category, liar_count)
        running = RunningGame(
            guild_id=interaction.guild.id,
            channel_id=interaction.channel_id,
            host_user_id=interaction.user.id,
            participant_role_id=role.id,
            game=game,
            participant_user_ids=set(join_view.joined_ids),
            liar_count=liar_count,
        )
        games[interaction.guild.id] = running
        running.task = asyncio.create_task(game_loop(interaction.guild, running))
    except Exception:
        await remove_recruitment_roles(interaction.guild, join_view)
        raise
    finally:
        recruiting.pop(interaction.guild.id, None)


async def remove_recruitment_roles(guild: discord.Guild, join_view: JoinGameView) -> None:
    role = guild.get_role(join_view.participant_role_id)
    if not role:
        return
    for user_id in join_view.joined_ids:
        member = guild.get_member(user_id)
        if member:
            with suppress(discord.DiscordException):
                await member.remove_roles(role, reason="라이어게임 모집 취소")


async def wait_for_discussion_event(view: DiscussionControlView, seconds: int) -> str:
    events = {
        asyncio.create_task(view.speech_done_event.wait()): "done",
        asyncio.create_task(view.skip_event.wait()): "skip",
        asyncio.create_task(view.extension_event.wait()): "extend",
    }
    done, pending = await asyncio.wait(events.keys(), timeout=seconds, return_when=asyncio.FIRST_COMPLETED)
    for task in pending:
        task.cancel()
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)
    if not done:
        return "timeout"
    if view.skip_event.is_set():
        return "skip"
    if view.extension_event.is_set():
        view.extension_event.clear()
        return "extend"
    if view.speech_done_event.is_set():
        view.speech_done_event.clear()
        return "done"
    return "timeout"


async def run_discussion_phase(channel: discord.abc.Messageable, running: RunningGame) -> None:
    game = running.game
    game.start_discussion()
    await set_discussion_slowmode(channel, running)

    speaking_order = game.players[:]
    random.shuffle(speaking_order)
    order_text = "\n".join(
        f"{index}. {player.name}"
        for index, player in enumerate(speaking_order, start=1)
    )
    await send_embed(
        channel,
        f"토론 시간: **{duration_text(config.discussion_seconds)}**\n"
        f"발언 시간: 1인당 **{duration_text(config.speech_seconds)}**\n"
        f"연장: **{duration_text(config.discussion_extension_seconds)}**, 최대 **{config.max_discussion_extensions}회**\n\n"
        f"발언 순서\n{order_text}",
        title="토론 시작",
    )

    control_view = DiscussionControlView(running)
    control_message = await send_embed(channel, control_view.status_text(), title="토론 진행", view=control_view)
    control_view.message = control_message

    deadline = time.monotonic() + config.discussion_seconds
    speaker_index = 0
    round_number = 1
    try:
        while time.monotonic() < deadline and not control_view.skip_event.is_set():
            player = speaking_order[speaker_index % len(speaking_order)]
            if speaker_index and speaker_index % len(speaking_order) == 0:
                round_number += 1
                await send_embed(channel, f"발언 순서 {round_number}라운드를 시작합니다.", title="다음 라운드")

            turn_seconds = config.speech_seconds
            control_view.set_current_speaker(player)
            await control_view.refresh_message()
            turn_view = SpeechTurnView(running, player)
            turn_message = await send_embed(
                channel,
                f"현재 발언자: **{player.name}**\n"
                f"제한 시간: **{duration_text(turn_seconds)}**\n"
                "발언을 마치면 이 메시지의 `발언 완료` 버튼을 누르거나 `발언완료`라고 보내세요.",
                title="발언 차례",
                view=turn_view,
            )
            turn_view.message = turn_message

            turn_deadline = time.monotonic() + turn_seconds
            try:
                while time.monotonic() < turn_deadline:
                    remaining_turn = max(1, int(turn_deadline - time.monotonic()))
                    result = await wait_for_discussion_event(control_view, remaining_turn)
                    if result == "extend":
                        deadline += config.discussion_extension_seconds
                        await send_embed(
                            channel,
                            f"토론 시간이 **{duration_text(config.discussion_extension_seconds)}** 연장되었습니다.",
                            title="토론 연장",
                            color=SUCCESS_EMBED_COLOR,
                        )
                        await control_view.refresh_message()
                        continue
                    if result == "skip":
                        await send_embed(channel, "과반수 요청으로 토론을 종료하고 투표로 넘어갑니다.", title="토론 스킵")
                        return
                    break
            finally:
                disable_view_items(turn_view)
                if turn_message:
                    with suppress(discord.DiscordException):
                        await turn_message.edit(view=turn_view)

            speaker_index += 1
    finally:
        disable_view_items(control_view)
        if control_message:
            with suppress(discord.DiscordException):
                await control_message.edit(view=control_view)
        running.discussion_current_speaker_id = None
        running.discussion_current_speaker_name = ""

    await send_embed(channel, "토론 시간이 끝났습니다. 투표로 넘어갑니다.", title="토론 종료")


async def game_loop(guild: discord.Guild, running: RunningGame) -> None:
    channel = guild.get_channel(running.channel_id)
    if not isinstance(channel, discord.abc.Messageable):
        games.pop(running.guild_id, None)
        return
    try:
        while True:
            game = running.game
            round_label = f"라운드 {running.round_number} " if running.round_number > 1 else ""
            scores_section = (
                f"\n\n**점수판**\n{session_scores_text(running)}"
                if running.round_number > 1 and running.session_scores
                else ""
            )
            await send_embed(
                channel,
                f"참가자 **{len(game.players)}명**, 라이어 **{len(game.liars())}명**으로 시작합니다.\n"
                f"각자 DM으로 받은 비밀 정보를 확인하세요.{scores_section}",
                title=f"{round_label}라이어게임 시작",
                color=SUCCESS_EMBED_COLOR,
            )
            failed_dm = await send_secret_messages(guild, game)
            if failed_dm:
                await send_embed(
                    channel,
                    "DM 전송 실패: " + ", ".join(failed_dm) + "\n개인 DM 허용 설정을 확인하세요.",
                    title="비밀 정보 전송 실패",
                    color=WARNING_EMBED_COLOR,
                )

            await run_discussion_phase(channel, running)

            game.start_vote()
            running.vote_complete_event.clear()
            vote_view = VoteView(running)
            vote_message = await send_embed(channel, vote_view.status_text(), title="라이어 지목 투표", view=vote_view)
            vote_view.message = vote_message
            try:
                await asyncio.wait_for(running.vote_complete_event.wait(), timeout=config.vote_seconds)
            except asyncio.TimeoutError:
                pass
            disable_view_items(vote_view)
            if vote_message:
                with suppress(discord.DiscordException):
                    await vote_message.edit(view=vote_view)

            vote_result = game.resolve_vote()

            if vote_result.tied and game.winner is None:
                await send_embed(
                    channel,
                    f"투표가 동률입니다. 1회 재투표를 진행합니다.\n\n투표 집계\n{game.vote_summary_text()}",
                    title="동률 — 재투표",
                    color=WARNING_EMBED_COLOR,
                )
                running.vote_complete_event.clear()
                revote_view = VoteView(running)
                revote_message = await send_embed(
                    channel, revote_view.status_text(), title="재투표 — 라이어 지목", view=revote_view
                )
                revote_view.message = revote_message
                try:
                    await asyncio.wait_for(running.vote_complete_event.wait(), timeout=config.vote_seconds)
                except asyncio.TimeoutError:
                    pass
                disable_view_items(revote_view)
                if revote_message:
                    with suppress(discord.DiscordException):
                        await revote_message.edit(view=revote_view)
                vote_result = game.resolve_vote()

            await announce_vote_result(channel, running, vote_result)

            if game.phase == Phase.GUESS and vote_result.target:
                await run_guess_phase(channel, running, vote_result.target)

            if game.winner:
                record_game_stats(running, game.winner, vote_result)
                update_session_scores(running)
                await announce_final_result(channel, running)

            if not game.winner or not config.continue_vote_enabled or not await run_continue_vote(channel, running):
                break

            # 다음 라운드 준비
            running.round_number += 1
            running.stats_recorded = False
            running.vote_complete_event = asyncio.Event()
            running.discussion_speech_done_event = asyncio.Event()
            running.started_at = time.monotonic()
            players = [(p.user_id, p.name) for p in game.players]
            word, category = choose_word(None, None)
            running.game = LiarGame(players, word, category, running.liar_count)

    except asyncio.CancelledError:
        await send_embed(channel, "게임이 중지되었습니다.", title="라이어게임 중지", color=ERROR_EMBED_COLOR)
    except Exception as error:
        print(f"Liar game loop error: {error!r}")
        await send_embed(
            channel,
            f"게임 진행 중 오류가 발생했습니다.\n오류: `{type(error).__name__}: {error}`",
            title="게임 오류",
            color=ERROR_EMBED_COLOR,
        )
    finally:
        await restore_slowmode(channel, running)
        await remove_participant_roles(guild, running)
        if games.get(running.guild_id) is running:
            games.pop(running.guild_id, None)


async def send_secret_messages(guild: discord.Guild, game: LiarGame) -> list[str]:
    failed: list[str] = []
    for player in game.players:
        member = guild.get_member(player.user_id)
        if not member:
            failed.append(player.name)
            continue
        try:
            await member.send(embed=make_embed(game.secret_text_for(player), title="라이어게임 비밀 정보"))
        except discord.DiscordException:
            failed.append(player.name)
    return failed


async def announce_vote_result(
    channel: discord.abc.Messageable,
    running: RunningGame,
    vote_result: VoteResult,
) -> None:
    game = running.game
    if vote_result.no_votes:
        await send_embed(
            channel,
            f"아무도 투표하지 않았습니다. 라이어 승리입니다.\n\n투표 집계\n{game.vote_summary_text()}",
            title="투표 결과",
            color=WARNING_EMBED_COLOR,
        )
        return
    if vote_result.tied:
        await send_embed(
            channel,
            f"투표가 동률입니다. 라이어 승리입니다.\n\n투표 집계\n{game.vote_summary_text()}",
            title="투표 결과",
            color=WARNING_EMBED_COLOR,
        )
        return
    if not vote_result.target:
        await send_embed(
            channel,
            f"스킵이 최다 득표했습니다. 라이어 승리입니다.\n\n투표 집계\n{game.vote_summary_text()}",
            title="투표 결과",
            color=WARNING_EMBED_COLOR,
        )
        return
    if vote_result.target.is_liar:
        await send_embed(
            channel,
            f"**{vote_result.target.name}** 님이 라이어로 지목됐습니다.\n"
            f"라이어는 **{duration_text(config.guess_seconds)}** 안에 제시어를 맞히면 역전 승리합니다.\n\n"
            f"투표 집계\n{game.vote_summary_text()}",
            title="라이어 지목 성공",
            color=SUCCESS_EMBED_COLOR,
        )
    else:
        await send_embed(
            channel,
            f"**{vote_result.target.name}** 님은 라이어가 아니었습니다. 라이어 승리입니다.\n\n"
            f"투표 집계\n{game.vote_summary_text()}",
            title="라이어 지목 실패",
            color=ERROR_EMBED_COLOR,
        )


async def run_guess_phase(channel: discord.abc.Messageable, running: RunningGame, accused: Player) -> None:
    guess_view = GuessView(accused.user_id)
    message = await send_embed(
        channel,
        f"**{accused.name}** 님, 버튼을 눌러 제시어를 추측하세요.\n"
        f"제한 시간: **{duration_text(config.guess_seconds)}**",
        title="라이어 최종 추측",
        view=guess_view,
    )
    guess_view.message = message
    try:
        await asyncio.wait_for(guess_view.done.wait(), timeout=config.guess_seconds)
    except asyncio.TimeoutError:
        pass
    disable_view_items(guess_view)
    if message:
        with suppress(discord.DiscordException):
            await message.edit(view=guess_view)

    if guess_view.guess_text is None:
        result = running.game.force_guess_fail()
        await send_embed(
            channel,
            "제한 시간 안에 추측하지 못했습니다. 시민 승리입니다.",
            title="추측 실패",
            color=SUCCESS_EMBED_COLOR,
        )
        return

    result = running.game.resolve_guess(accused.user_id, guess_view.guess_text)
    if result.correct:
        await send_embed(
            channel,
            f"라이어가 제시어 **{running.game.word}** 를 맞혔습니다. 라이어 승리입니다.",
            title="추측 성공",
            color=SUCCESS_EMBED_COLOR,
        )
    else:
        await send_embed(
            channel,
            f"라이어의 추측 `{result.guess}` 은 정답이 아닙니다. 시민 승리입니다.",
            title="추측 실패",
            color=SUCCESS_EMBED_COLOR,
        )


async def announce_final_result(channel: discord.abc.Messageable, running: RunningGame) -> None:
    game = running.game
    winner_text = "라이어" if game.winner == Winner.LIARS else "시민"
    scores_section = (
        f"\n\n**점수판**\n{session_scores_text(running)}"
        if running.session_scores
        else ""
    )
    title = f"라운드 {running.round_number} 종료" if running.round_number > 1 else "게임 종료"
    await send_embed(
        channel,
        f"승리 팀: **{winner_text}**\n\n{game.reveal_text()}{scores_section}",
        title=title,
        color=SUCCESS_EMBED_COLOR,
    )


@bot.tree.command(name="라이어중지", description="진행 중인 라이어게임 또는 모집을 중지합니다.")
async def stop_game(interaction: discord.Interaction) -> None:
    if not interaction.guild or interaction.guild_id is None:
        await send_interaction_reply(interaction, "서버에서만 사용할 수 있습니다.", private=True)
        return
    if not await require_manager_reply(interaction):
        return
    view = recruiting.get(interaction.guild_id)
    if view:
        await view.cancel_from_manager("관리자가 모집을 중지했습니다.")
        await remove_recruitment_roles(interaction.guild, view)
        recruiting.pop(interaction.guild_id, None)
        await send_interaction_reply(interaction, "모집을 중지했습니다.", color=SUCCESS_EMBED_COLOR, private=True)
        return
    running = games.get(interaction.guild_id)
    if not running or not running.task:
        await send_interaction_reply(interaction, "진행 중인 라이어게임이 없습니다.", private=True)
        return
    running.task.cancel()
    await send_interaction_reply(interaction, "게임 중지 요청을 보냈습니다.", color=SUCCESS_EMBED_COLOR, private=True)


@bot.tree.command(name="라이어상태", description="현재 라이어게임 상태를 확인합니다.")
async def show_status(interaction: discord.Interaction) -> None:
    if not interaction.guild_id:
        await send_interaction_reply(interaction, "서버에서만 사용할 수 있습니다.", private=True)
        return
    view = recruiting.get(interaction.guild_id)
    if view:
        await send_interaction_reply(
            interaction,
            f"모집 중입니다.\n현재 참가자: **{len(view.joined_ids)}명**\n{participant_text(view.joined_names)}",
            private=True,
        )
        return
    running = games.get(interaction.guild_id)
    if not running:
        await send_interaction_reply(interaction, "진행 중인 라이어게임이 없습니다.", private=True)
        return
    await send_interaction_reply(interaction, game_status_text(running), private=True)


@bot.tree.command(name="라이어주제", description="라이어게임 단어 카테고리를 검색합니다.")
@app_commands.describe(검색어="비워두면 전체 주제를 보여줍니다. 띄어쓰기는 무시됩니다.")
@app_commands.autocomplete(검색어=category_autocomplete)
async def show_categories(interaction: discord.Interaction, 검색어: str | None = None) -> None:
    categories = matching_categories(검색어)
    if not categories:
        await send_interaction_reply(
            interaction,
            "검색 결과가 없습니다. 예: `한국음식`, `세계도시`, `화학원소`",
            title="라이어게임 주제",
            private=True,
        )
        return
    total_words = sum(len(config.word_bank[category]) for category in categories)
    lines = [
        f"- **{category}**: {len(config.word_bank[category])}개"
        for category in categories
    ]
    await send_interaction_reply(
        interaction,
        f"검색 결과: **{len(categories)}개 주제**, **{total_words}개 단어**\n"
        "주제 입력은 띄어쓰기 없이 해도 인식됩니다.\n\n"
        + "\n".join(lines),
        title="라이어게임 주제",
        private=True,
    )


@bot.tree.command(name="라이어설정", description="라이어게임 기본 설정을 변경합니다.")
@app_commands.describe(
    라이어수="기본 라이어 수",
    최소인원="게임 시작 최소 인원",
    최대인원=f"게임 최대 인원. 최대 {MAX_GAME_PLAYERS}명",
    모집초="참가자 모집 시간",
    토론초="토론 시간",
    발언초="한 사람당 발언 시간",
    연장초="토론 연장 1회당 추가 시간",
    최대연장="토론 연장 최대 횟수",
    투표초="투표 시간",
    추측초="라이어 제시어 추측 시간",
    슬로우모드초="토론 채널 슬로우모드",
    참가역할="참가자에게 부여할 역할 이름",
    관리자역할="관리 명령을 사용할 역할 이름",
    이어하기="게임 종료 후 이어하기 투표 활성화 여부",
)
async def configure_game(
    interaction: discord.Interaction,
    라이어수: int | None = None,
    최소인원: int | None = None,
    최대인원: int | None = None,
    모집초: int | None = None,
    토론초: int | None = None,
    발언초: int | None = None,
    연장초: int | None = None,
    최대연장: int | None = None,
    투표초: int | None = None,
    추측초: int | None = None,
    슬로우모드초: int | None = None,
    참가역할: str | None = None,
    관리자역할: str | None = None,
    이어하기: bool | None = None,
) -> None:
    if not await require_manager_reply(interaction):
        return
    if 라이어수 is not None:
        config.default_liar_count = 라이어수
    if 최소인원 is not None:
        config.min_player_count = 최소인원
    if 최대인원 is not None:
        config.max_player_count = 최대인원
    if 모집초 is not None:
        config.recruitment_seconds = 모집초
    if 토론초 is not None:
        config.discussion_seconds = 토론초
    if 발언초 is not None:
        config.speech_seconds = 발언초
    if 연장초 is not None:
        config.discussion_extension_seconds = 연장초
    if 최대연장 is not None:
        config.max_discussion_extensions = 최대연장
    if 투표초 is not None:
        config.vote_seconds = 투표초
    if 추측초 is not None:
        config.guess_seconds = 추측초
    if 슬로우모드초 is not None:
        config.chat_slowmode_seconds = 슬로우모드초
    if 참가역할:
        config.participant_role = 참가역할.strip()
    if 관리자역할:
        config.manager_role = 관리자역할.strip()
    if 이어하기 is not None:
        config.continue_vote_enabled = 이어하기
    save_config()
    await send_interaction_reply(
        interaction,
        current_settings_text("라이어게임 설정을 저장했습니다."),
        color=SUCCESS_EMBED_COLOR,
        private=True,
    )


@bot.tree.command(name="라이어활성화", description="라이어게임 시작을 활성화합니다.")
async def enable_game(interaction: discord.Interaction) -> None:
    if not await require_manager_reply(interaction):
        return
    config.game_enabled = True
    save_config()
    await send_interaction_reply(interaction, "라이어게임을 활성화했습니다.", color=SUCCESS_EMBED_COLOR, private=True)


@bot.tree.command(name="라이어비활성화", description="라이어게임 시작을 비활성화합니다.")
async def disable_game(interaction: discord.Interaction) -> None:
    if not await require_manager_reply(interaction):
        return
    config.game_enabled = False
    save_config()
    await send_interaction_reply(interaction, "라이어게임을 비활성화했습니다.", color=SUCCESS_EMBED_COLOR, private=True)


@bot.tree.command(name="라이어규칙", description="라이어게임 규칙을 확인합니다.")
async def show_rules(interaction: discord.Interaction) -> None:
    await send_interaction_reply(
        interaction,
        "시민은 같은 제시어를 받고, 라이어는 주제만 받습니다.\n"
        "봇이 정한 발언 순서대로 돌아가며 토론합니다.\n"
        "토론 중 과반수 버튼 투표로 스킵하거나 시간을 연장할 수 있습니다.\n"
        "토론 후 모두가 라이어라고 생각하는 사람에게 투표합니다.\n"
        "시민을 지목하거나 동률/스킵/무투표가 나오면 라이어가 승리합니다.\n"
        "라이어를 지목하면 라이어가 제시어를 추측합니다. 맞히면 라이어 승리, 틀리면 시민 승리입니다.",
        title="라이어게임 규칙",
        private=True,
    )


@bot.tree.command(name="라이어내정보", description="내 라이어게임 전적을 확인합니다.")
async def show_my_info(interaction: discord.Interaction) -> None:
    await send_interaction_reply(interaction, personal_stats_text(interaction.user.id, display_name(interaction.user)), private=True)


@bot.tree.command(name="라이어리더보드", description="라이어게임 전적 순위를 확인합니다.")
@app_commands.describe(기준="순위를 세울 기준")
@app_commands.choices(
    기준=[
        app_commands.Choice(name="승수", value="wins"),
        app_commands.Choice(name="승률", value="win_rate"),
        app_commands.Choice(name="라이어 적중 투표", value="correct_votes"),
        app_commands.Choice(name="제시어 추측 성공", value="successful_guesses"),
        app_commands.Choice(name="판수", value="games"),
    ]
)
async def show_leaderboard(
    interaction: discord.Interaction,
    기준: app_commands.Choice[str] | None = None,
) -> None:
    metric = 기준.value if 기준 else "wins"
    await send_interaction_reply(interaction, leaderboard_text(metric), title="라이어게임 리더보드")


@bot.tree.command(name="라이어전적초기화", description="라이어게임 전적을 초기화합니다.")
async def reset_stats(interaction: discord.Interaction) -> None:
    if not await require_manager_reply(interaction):
        return
    save_stats({})
    await send_interaction_reply(interaction, "라이어게임 전적을 초기화했습니다.", color=SUCCESS_EMBED_COLOR, private=True)


@bot.tree.error
async def command_error(interaction: discord.Interaction, error: app_commands.AppCommandError) -> None:
    if isinstance(error, app_commands.CommandInvokeError):
        error = error.original
    await send_interaction_reply(
        interaction,
        f"명령 처리 중 오류가 발생했습니다.\n`{type(error).__name__}: {error}`",
        title="명령 오류",
        color=ERROR_EMBED_COLOR,
        private=True,
    )


def main() -> None:
    global config
    config = load_config()
    load_dotenv(BASE_DIR / ".env")
    token = os.getenv("DISCORD_TOKEN")
    if not token:
        raise RuntimeError(".env 파일에 DISCORD_TOKEN을 설정하세요.")
    bot.run(token)


if __name__ == "__main__":
    main()
