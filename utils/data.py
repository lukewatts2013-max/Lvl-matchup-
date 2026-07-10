"""Data loading, saving, and player lookup utilities."""

import json
import os
import re
import tempfile
from typing import Optional

from config import DEFAULT_PLAYERS
from models.types import Player, LeagueData

DATA_FILE = os.path.join(os.path.dirname(__file__), "../league_stats.json")


def load_data() -> LeagueData:
    """Load league data from file, backfilling missing fields.
    
    Returns:
        LeagueData dict with players, history, and matchup_history.
    """
    if not os.path.exists(DATA_FILE):
        return {"players": dict(DEFAULT_PLAYERS), "history": [], "matchup_history": []}
    
    try:
        with open(DATA_FILE, "r") as f:
            data: LeagueData = json.load(f)
    except (json.JSONDecodeError, OSError):
        return {"players": dict(DEFAULT_PLAYERS), "history": [], "matchup_history": []}
    
    # Backfill career_avg_ppd onto any existing player record that's missing it
    defaults_by_name = {v["name"]: v for v in DEFAULT_PLAYERS.values()}
    for key, player in data["players"].items():
        if not player.get("career_avg_ppd"):  # catches missing key AND None/0
            ref = DEFAULT_PLAYERS.get(key) or defaults_by_name.get(player.get("name", ""))
            if ref and ref.get("career_avg_ppd"):
                player["career_avg_ppd"] = ref["career_avg_ppd"]
    
    # Ensure matchup_history key exists on older saves
    data.setdefault("matchup_history", [])
    return data


def save_data(data: LeagueData) -> None:
    """Save league data atomically using a temporary file.
    
    Args:
        data: LeagueData dict to persist.
        
    Raises:
        RuntimeError: If file write fails.
    """
    dir_name = os.path.dirname(DATA_FILE)
    try:
        with tempfile.NamedTemporaryFile("w", dir=dir_name, delete=False, suffix=".tmp") as tf:
            json.dump(data, tf, indent=4)
            tmp_path = tf.name
        os.replace(tmp_path, DATA_FILE)
    except OSError as e:
        raise RuntimeError(f"Failed to save league data: {e}")


def _norm(name: str) -> str:
    """Normalise a player name: lowercase, strip non-alphanumeric.
    
    Examples:
        D.A.G.O.A.T → dagoat
        Magicmikey66 → magicmikey66
        bohica7599 → bohica7599
    """
    return re.sub(r"[^a-z0-9]", "", name.lower())


def find_player_by_name(data: LeagueData, name: str) -> tuple[Optional[str], Optional[Player]]:
    """Find a player by name using exact, normalised, and digit-stripped matching.
    
    Matching priority:
        1. Exact case-insensitive name
        2. Normalised name (strips dots, underscores, spaces)
        3. Digit-stripped prefix (so 'Bohica' matches 'bohica7599')
    
    Args:
        data: LeagueData containing players dict.
        name: Player name to search for.
        
    Returns:
        Tuple of (player_id, player_stats) or (None, None) if not found.
    """
    name_lower = name.lower()
    name_norm = _norm(name)
    name_base = re.sub(r"\d+$", "", name_norm)  # strip trailing digits for pass 3
    
    # Pass 1: exact case-insensitive
    for p_id, p_stats in data["players"].items():
        if p_stats.get("name", "").lower() == name_lower:
            return p_id, p_stats
    
    # Pass 2: normalised (strips dots, underscores, spaces, etc.)
    for p_id, p_stats in data["players"].items():
        if _norm(p_stats.get("name", "")) == name_norm:
            return p_id, p_stats
    
    # Pass 3: digit-stripped prefix match — "Bohica" → "bohica" matches "bohica7599"
    if name_base:
        for p_id, p_stats in data["players"].items():
            stored_base = re.sub(r"\d+$", "", _norm(p_stats.get("name", "")))
            if stored_base and stored_base == name_base:
                return p_id, p_stats
    
    return None, None


def find_player_by_discord_id(data: LeagueData, discord_id: str) -> tuple[Optional[str], Optional[Player]]:
    """Find a player by stored discord_id field.
    
    Args:
        data: LeagueData containing players dict.
        discord_id: Discord user ID to search for.
        
    Returns:
        Tuple of (player_id, player_stats) or (None, None) if not found.
    """
    for p_id, p_stats in data["players"].items():
        if str(p_stats.get("discord_id", "")) == discord_id:
            return p_id, p_stats
    return None, None