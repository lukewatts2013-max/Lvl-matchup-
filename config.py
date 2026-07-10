"""Configuration constants for the league bot."""

# Scoring constants
PRIOR_GAMES = 4
MIN_DIFFICULTY_FACTOR = 0.78
OVR_DIFFICULTY_BASE = 220
OVR_DIFFICULTY_SLOPE = 0.0012

# Points constants
DEFENSE_TD_POINTS = 6
DEFENSE_2PT_POINTS = 2
SAFETY_POINTS = 2
POINTS_PER_DRIVE = 3

# Roster constants
MAX_ROSTER_SIZE = 18
MAX_ACTIVE_MATCHUPS = 16
MAX_POINTS_PER_ROUND = 42

# OVR validation bounds
MIN_OVR = 50
MAX_OVR = 330
MIN_OPPONENT_OVR = 100
MAX_OPPONENT_OVR = 400

# Default players with career averages
DEFAULT_PLAYERS = {
    # Tier 1
    "SunDevilTyler": {"name": "SunDevilTyler", "weekly": 24, "monthly": 24, "yearly": 24, "total_drives": 3, "ppd_drives": 3, "is_benched": False, "career_avg_ppd": 7.05},
    "Thrillhouse":   {"name": "Thrillhouse",   "weekly": 22, "monthly": 22, "yearly": 22, "total_drives": 3, "ppd_drives": 3, "is_benched": False, "career_avg_ppd": 7.48},
    "mike9413":      {"name": "mike9413",       "weekly": 16, "monthly": 16, "yearly": 16, "total_drives": 3, "ppd_drives": 3, "is_benched": False, "career_avg_ppd": 6.64},
    "DAGOAT":        {"name": "DAGOAT",         "weekly": 24, "monthly": 24, "yearly": 24, "total_drives": 3, "ppd_drives": 3, "is_benched": False, "career_avg_ppd": 7.22},
    # Tier 2
    "HeroOfWild":    {"name": "HeroOfWild",     "weekly": 22, "monthly": 22, "yearly": 22, "total_drives": 3, "ppd_drives": 3, "is_benched": False, "career_avg_ppd": 6.87},
    "Magicmikey66":  {"name": "Magicmikey66",   "weekly": 24, "monthly": 24, "yearly": 24, "total_drives": 3, "ppd_drives": 3, "is_benched": False, "career_avg_ppd": 6.70},
    "bohica7599":    {"name": "bohica7599",      "weekly": 24, "monthly": 24, "yearly": 24, "total_drives": 3, "ppd_drives": 3, "is_benched": False, "career_avg_ppd": 6.09},
    "Skoltrain":     {"name": "Skoltrain",      "weekly": 20, "monthly": 20, "yearly": 20, "total_drives": 3, "ppd_drives": 3, "is_benched": False, "career_avg_ppd": 6.40},
    # Tier 3
    "Raks":          {"name": "Raks",           "weekly": 0,  "monthly": 0,  "yearly": 0,  "total_drives": 0, "ppd_drives": 0, "is_benched": False, "career_avg_ppd": 6.24},
    "Ixyjakobe":     {"name": "Ixyjakobe",      "weekly": 0,  "monthly": 0,  "yearly": 0,  "total_drives": 0, "ppd_drives": 0, "is_benched": False, "career_avg_ppd": 6.13},
    "Kdaddy99":      {"name": "Kdaddy99",       "weekly": 0,  "monthly": 0,  "yearly": 0,  "total_drives": 0, "ppd_drives": 0, "is_benched": False, "career_avg_ppd": 5.86},
    "DirtyBirds559": {"name": "DirtyBirds559",  "weekly": 0,  "monthly": 0,  "yearly": 0,  "total_drives": 0, "ppd_drives": 0, "is_benched": False, "career_avg_ppd": 5.56},
    # Tier 4
    "Kirito":        {"name": "Kirito",         "weekly": 0,  "monthly": 0,  "yearly": 0,  "total_drives": 0, "ppd_drives": 0, "is_benched": False, "career_avg_ppd": 5.99},
    "Swarm":         {"name": "Swarm",          "weekly": 0,  "monthly": 0,  "yearly": 0,  "total_drives": 0, "ppd_drives": 0, "is_benched": False, "career_avg_ppd": 5.72},
    "Leroiheenok":   {"name": "Leroiheenok",    "weekly": 0,  "monthly": 0,  "yearly": 0,  "total_drives": 0, "ppd_drives": 0, "is_benched": False, "career_avg_ppd": 6.38},
}
