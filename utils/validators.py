"""Input validation utilities."""

from config import MIN_OVR, MAX_OVR, MIN_OPPONENT_OVR, MAX_OPPONENT_OVR, MAX_POINTS_PER_ROUND


def validate_ovr(ovr: int, is_opponent: bool = False) -> tuple[bool, str]:
    """Validate offensive OVR bounds.
    
    Args:
        ovr: OVR value to validate.
        is_opponent: If True, use opponent OVR bounds; else use player OVR bounds.
        
    Returns:
        Tuple of (is_valid, error_message).
    """
    if not isinstance(ovr, int):
        return False, "OVR must be an integer."
    
    min_val, max_val = (MIN_OPPONENT_OVR, MAX_OPPONENT_OVR) if is_opponent else (MIN_OVR, MAX_OVR)
    
    if not (min_val <= ovr <= max_val):
        return False, f"OVR must be between {min_val} and {max_val} (got {ovr})."
    
    return True, ""


def validate_points(points: int, max_points: int = MAX_POINTS_PER_ROUND) -> tuple[bool, str]:
    """Validate score points.
    
    Args:
        points: Points value to validate.
        max_points: Maximum allowed points (default 42).
        
    Returns:
        Tuple of (is_valid, error_message).
    """
    if not isinstance(points, int):
        return False, "Points must be an integer."
    
    if points < 0:
        return False, f"Points must be 0 or greater (got {points})."
    
    if points > max_points:
        return False, f"Points cannot exceed {max_points} (got {points})."
    
    return True, ""


def validate_timeframe(timeframe: str) -> tuple[bool, str]:
    """Validate stats timeframe.
    
    Args:
        timeframe: Timeframe string to validate.
        
    Returns:
        Tuple of (is_valid, error_message).
    """
    valid = ["weekly", "monthly", "yearly", "alltime"]
    if timeframe.lower() not in valid:
        return False, f"Use one of: {', '.join(valid)}"
    
    return True, ""