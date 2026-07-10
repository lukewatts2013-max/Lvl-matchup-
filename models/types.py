"""Type definitions for the league bot using TypedDict for better type safety."""

from typing import TypedDict, NotRequired


class MatchHistoryEntry(TypedDict):
    """A single game entry in a player's match history."""
    points: int
    opp_ovr: int
    opponent: str


class Player(TypedDict, total=False):
    """Player statistics and metadata."""
    name: str
    weekly: int
    monthly: int
    yearly: int
    total_drives: int
    ppd_drives: int
    is_benched: bool
    career_avg_ppd: float
    off_ovr: int
    discord_id: str
    match_history: list[MatchHistoryEntry]
    points_allowed: int
    weekly_allowed: int
    monthly_allowed: int
    defensive_tds: int
    defensive_2pts: int
    safeties: int
    alltime_ppd: float


class Assignment(TypedDict, total=False):
    """Matchup assignment for a player."""
    opponent_name: str
    opponent_ovr: int
    last_score: NotRequired[int]
    opp_score: NotRequired[int]


class Matchup(TypedDict, total=False):
    """Current week's matchup information."""
    date: str
    opp_league: NotRequired[str]
    assignments: dict[str, Assignment]
    channel_id: NotRequired[int]


class MatchupInfo(TypedDict, total=False):
    """Matchup data for display/history."""
    our_player: str
    our_score: int
    opp_player: str
    opp_ovr: int
    opp_score: NotRequired[int]


class MatchupSnapshot(TypedDict, total=False):
    """Snapshot of a completed matchup for history."""
    date: str
    opp_league: str
    matchups: list[MatchupInfo]
    our_total: int
    opp_total: NotRequired[int]


class LeagueData(TypedDict, total=False):
    """Root data structure for league statistics."""
    players: dict[str, Player]
    history: list
    matchup_history: list[MatchupSnapshot]
    current_matchup: NotRequired[Matchup]
