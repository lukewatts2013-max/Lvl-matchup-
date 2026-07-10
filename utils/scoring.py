"""Scoring and efficiency calculation utilities."""

from config import (
    PRIOR_GAMES,
    MIN_DIFFICULTY_FACTOR,
    OVR_DIFFICULTY_BASE,
    OVR_DIFFICULTY_SLOPE,
    POINTS_PER_DRIVE,
    MAX_POINTS_PER_ROUND,
)
from models.types import Player


def _diff_factor(opp_ovr: int) -> float:
    """Compute difficulty multiplier for a defense OVR.
    
    Defenses rated 220+ OVR get a slight score compression factor
    to reflect increased difficulty. Uses formula:
        max(0.78, 1.0 - max(0, opp_ovr - 220) * 0.0012)
    
    Args:
        opp_ovr: Opponent defense overall rating.
        
    Returns:
        Difficulty multiplier (typically 0.78–1.0).
    """
    return max(MIN_DIFFICULTY_FACTOR, 1.0 - max(0, opp_ovr - OVR_DIFFICULTY_BASE) * OVR_DIFFICULTY_SLOPE)


def calc_efficiency(player: Player) -> float:
    """Compute difficulty-normalised PPD blended with career average as Bayesian prior.
    
    Each game's PPD is divided by the difficulty factor for that opponent, so
    scoring 7.0 PPD against a 260-OVR defense counts as ~7.35 normalised PPD.
    
    To prevent a single bad (or good) game from fully overriding a player's
    established career average, the match-history efficiency is blended with
    career_avg_ppd using a prior equivalent to PRIOR_GAMES reference games.
    As a player logs more rounds the prior fades and real history takes over.
    
    Falls back to career_avg_ppd alone when no match history exists.
    
    Args:
        player: Player stats dict with 'match_history' and 'career_avg_ppd'.
        
    Returns:
        Efficiency score (PPD) accounting for opponent difficulty and prior.
    """
    history = player.get("match_history", [])
    career_avg = float(player.get("career_avg_ppd") or 0.0)
    
    if not history:
        # No match history yet — blend current weekly PPD with career avg if
        # the player has already scored this week (e.g. after a manual data restore)
        ppd_drives = player.get("ppd_drives", 0)
        weekly = player.get("weekly", 0)
        if ppd_drives > 0:
            cur_ppd = weekly / ppd_drives
            return (1 * cur_ppd + PRIOR_GAMES * career_avg) / (1 + PRIOR_GAMES)
        return career_avg
    
    match_eff = sum(
        (m["points"] / POINTS_PER_DRIVE) / _diff_factor(m.get("opp_ovr", OVR_DIFFICULTY_BASE))
        for m in history
    ) / len(history)
    
    n = len(history)
    return (n * match_eff + PRIOR_GAMES * career_avg) / (n + PRIOR_GAMES)


def composite_score(player: Player, current_opp_ovr: int = 0) -> float:
    """Compute matchup-sort key: 50% Career Efficiency + 50% Monthly PPD.
    
    This blends long-term performance with recent monthly form, difficulty-adjusted
    for the current matchup opponent.
    
    Args:
        player: Player stats dict.
        current_opp_ovr: Opponent OVR for this week's matchup (optional).
        
    Returns:
        Composite score for sorting/matchup selection.
    """
    career_eff = calc_efficiency(player)
    
    monthly_pts = float(player.get("monthly_points", 0))
    monthly_drives = int(player.get("monthly_drives", 0))
    
    if monthly_drives > 0:
        raw_monthly_ppd = monthly_pts / monthly_drives
    else:
        raw_monthly_ppd = 0.0
    
    if raw_monthly_ppd > 0 and current_opp_ovr > 0:
        adj_monthly_ppd = raw_monthly_ppd / _diff_factor(current_opp_ovr)
    else:
        adj_monthly_ppd = raw_monthly_ppd
    
    return 0.50 * career_eff + 0.50 * adj_monthly_ppd


def predict_score(eff_ppd: float, opp_ovr: int | None = None) -> int:
    """Predict expected points for the drive.
    
    Formula: eff_ppd × 3 drives, difficulty-adjusted, rounded to nearest even, capped at 24.
    
    Args:
        eff_ppd: Player efficiency (PPD) estimate.
        opp_ovr: Opponent defense OVR (optional, used for difficulty adjustment).
        
    Returns:
        Predicted points score (even integer, 0–24).
    """
    base = eff_ppd * POINTS_PER_DRIVE
    if opp_ovr:
        factor = _diff_factor(opp_ovr)
        base *= factor
    return int(min(MAX_POINTS_PER_ROUND, max(0, round(base / 2) * 2)))