import io
import sys
import copy
import discord
from discord.ext import commands
from matchup_graphic import generate_matchup_sheet
from PIL import Image, ImageDraw, ImageFont
import json
import os
from dotenv import load_dotenv
load_dotenv()
import re
import asyncio
import tempfile
from datetime import datetime, timedelta

# Force stdout to flush immediately — otherwise print() output can sit in a
# buffer and never appear in a hosted/non-interactive console.
try:
    sys.stdout.reconfigure(line_buffering=True)
except Exception:
    pass

# 1. BOT SETUP
intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)

@bot.event
async def on_command_error(ctx, error):
    """Surface unhandled command errors so they never go silently missing."""
    if isinstance(error, commands.MissingRequiredArgument):
        await ctx.send(f"⚠️ Missing argument: `{error.param.name}`. Check the command usage.")
    elif isinstance(error, commands.BadArgument):
        await ctx.send(f"⚠️ Bad argument — {error}")
    elif isinstance(error, commands.MissingPermissions):
        await ctx.send("🚫 You don't have permission to use that command.")
    elif isinstance(error, commands.CommandNotFound):
        pass  # ignore unknown !commands
    else:
        # Unexpected error — print to console AND tell the channel
        import traceback
        tb = "".join(traceback.format_exception(type(error), error, error.__traceback__))
        print(f"[ERROR] Command '{ctx.command}' raised:\n{tb}")
        await ctx.send(f"❌ Unexpected error in `!{ctx.command}`: {error}")

@bot.event
async def on_ready():
    """Auto-post the scoreboard to the last known channel on startup."""
    print(f"Logged in as {bot.user}")
    try:
        data       = load_data()
        channel_id = data.get("current_matchup", {}).get("channel_id")
        if channel_id:
            channel = bot.get_channel(channel_id)
            if channel is None:
                channel = await bot.fetch_channel(channel_id)
            if channel and data.get("current_matchup", {}).get("assignments"):
                await repost_matchup(None, channel=channel, purge=False)
    except Exception as e:
        print(f"on_ready scoreboard post failed: {e}")

# Async lock to prevent concurrent read-modify-write races on the data file
_data_lock = asyncio.Lock()

# Tracks the last leaderboard message so we can replace it instead of stacking
_last_leaderboard: discord.Message | None = None

# ── NET (+/-) CHART ──────────────────────────────────────────────────────
def generate_net_chart(net_data: list, title: str) -> io.BytesIO:
    """
    Horizontal bar chart of weekly Net (+/-) = offense scored - defense allowed.
    net_data: list of (name, net) tuples, any order — this function sorts descending.
    Returns a BytesIO PNG ready for discord.File.
    """
    rows = sorted(net_data, key=lambda x: x[1], reverse=True)

    row_h      = 40
    pad_top    = 70
    pad_bottom = 30
    label_w    = 170
    chart_w    = 560
    width      = label_w + chart_w + 90
    height     = pad_top + row_h * len(rows) + pad_bottom
    zero_x     = label_w + chart_w // 2

    bg, grid   = (35, 37, 42), (60, 63, 70)
    pos_c, neg_c = (87, 242, 135), (237, 66, 69)
    text_c     = (235, 235, 240)

    img  = Image.new("RGB", (width, height), bg)
    draw = ImageDraw.Draw(img)

    def _font(size, bold=False):
        for path in (
            "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf" if bold
            else "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
            "DejaVuSans-Bold.ttf" if bold else "DejaVuSans.ttf",
        ):
            try:
                return ImageFont.truetype(path, size)
            except Exception:
                continue
        return ImageFont.load_default()

    title_font, label_font, val_font = _font(22, True), _font(15), _font(15, True)
    draw.text((20, 20), title, font=title_font, fill=text_c)

    max_abs = max((abs(n) for _, n in rows), default=1) or 1
    scale   = (chart_w // 2 - 20) / max_abs
    draw.line([(zero_x, pad_top - 10), (zero_x, height - pad_bottom + 10)], fill=grid, width=1)

    for i, (name, net) in enumerate(rows):
        y0, y1 = pad_top + i * row_h, pad_top + i * row_h + row_h - 8
        bar_len = int(abs(net) * scale)
        color   = pos_c if net >= 0 else neg_c
        if net >= 0:
            draw.rectangle([zero_x, y0, zero_x + bar_len, y1], fill=color)
        else:
            draw.rectangle([zero_x - bar_len, y0, zero_x, y1], fill=color)
        draw.text((10, y0 + (row_h - 8)//2 - 8), name[:18], font=label_font, fill=text_c)
        val_str = f"+{net}" if net >= 0 else str(net)
        vx = (zero_x + bar_len + 8) if net >= 0 else (zero_x - bar_len - 8 - draw.textlength(val_str, font=val_font))
        draw.text((vx, y0 + (row_h - 8)//2 - 8), val_str, font=val_font, fill=color)

    buf = io.BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)
    return buf


async def _try_delete(msg: discord.Message) -> None:
    """Silently delete a message; ignore if already gone or no permission."""
    try:
        await msg.delete()
    except (discord.NotFound, discord.Forbidden):
        pass

# ── SCORING / EFFICIENCY HELPERS ────────────────────────────────────────────

def _diff_factor(opp_ovr: int) -> float:
    """Difficulty multiplier for a defense OVR — same model as predict_score."""
    return max(0.78, 1.0 - max(0, opp_ovr - 220) * 0.0012)


def _game_efficiency(entry: dict) -> float:
    """Difficulty-adjusted PPD for one logged game (e.g. 7.0 PPD vs a 260-OVR
    defense counts as ~7.35 normalised — tougher defenses give partial credit)."""
    return (entry.get("points", 0) / 3.0) / _diff_factor(entry.get("opp_ovr", 220))


def _parse_date(date_str) -> "date | None":
    """Best-effort parse of a stored 'YYYY-MM-DD' string. Returns None on any
    missing/malformed value so callers can skip it instead of crashing."""
    if not date_str:
        return None
    try:
        return datetime.strptime(str(date_str), "%Y-%m-%d").date()
    except ValueError:
        return None


def calc_efficiency(player: dict) -> float:
    """
    CAREER efficiency: a player's long-run, difficulty-normalised skill level.

    Every game in match_history is difficulty-adjusted (see _game_efficiency),
    then blended with the player's manually-entered career_avg_ppd (a rough
    preseason estimate with no opponent-difficulty info baked in) using a
    Bayesian prior worth PRIOR_GAMES games. That old estimate is intentionally
    weighted lightly — it fades fast as real, opponent-aware results pile up,
    and after a handful of logged games it barely moves the number at all.

    Falls back to career_avg_ppd alone when there's no match history yet.
    """
    PRIOR_GAMES = 2          # old manual estimate ~= this many "prior" games
    history     = player.get("match_history", [])
    career_avg  = float(player.get("career_avg_ppd") or 0.0)
    if not history:
        # No match history yet — blend current weekly PPD with career avg if
        # the player has already scored this week (e.g. after a manual data restore)
        ppd_drives = player.get("ppd_drives", 0)
        weekly     = player.get("weekly", 0)
        if ppd_drives > 0:
            cur_ppd = weekly / ppd_drives
            return (1 * cur_ppd + PRIOR_GAMES * career_avg) / (1 + PRIOR_GAMES)
        return career_avg
    match_eff = sum(_game_efficiency(m) for m in history) / len(history)
    n = len(history)
    return (n * match_eff + PRIOR_GAMES * career_avg) / (n + PRIOR_GAMES)


def calc_windowed_efficiency(player: dict, days: int, today=None):
    """
    Average difficulty-adjusted efficiency for games logged in the last
    `days` days (inclusive). Returns None — not 0 — when nothing falls in
    that window, so callers can fall back to career efficiency instead of
    unfairly tanking a player's score for an empty window.
    """
    today  = today or datetime.now().date()
    cutoff = today - timedelta(days=days - 1)
    recent = [
        _game_efficiency(m)
        for m in player.get("match_history", [])
        if (d := _parse_date(m.get("date"))) and d >= cutoff
    ]
    return (sum(recent) / len(recent)) if recent else None


def composite_score(player: dict) -> float:
    """
    LADDER SCORE — the ranking used to build matchups.

    50% career efficiency + 25% 7-day efficiency + 25% 3-day efficiency,
    each difficulty-adjusted per game. This naturally smooths out a single
    bad (or great) day — it's a fraction of a fraction of the total, not a
    third of the whole score — while still letting real, sustained recent
    form move a player up or down faster than career average alone would.
    If a player has no games in the 7-day or 3-day window (new to the
    roster, or hasn't played recently), that window falls back to their
    career efficiency instead of penalising them for the gap.
    """
    career = calc_efficiency(player)
    seven  = calc_windowed_efficiency(player, 7)
    three  = calc_windowed_efficiency(player, 3)
    if seven is None:
        seven = career
    if three is None:
        three = career
    return 0.50 * career + 0.25 * seven + 0.25 * three


# ── PERSONAL DIFFICULTY SENSITIVITY ─────────────────────────────────────────
# predict_score uses one generic curve (tougher defense -> lower expected
# score) for everyone. But some players genuinely hold up better against
# tough defenses than others, and the generic curve can't see that — it
# treats a 7.0-eff player the same whether they've historically struggled
# or thrived against 250+ OVR defenses. These helpers fit a per-player
# trend from their own match_history and nudge the generic prediction,
# but only once there's enough real signal to trust it.
_MIN_GAMES_FOR_PERSONAL = 4     # need at least this many logged games
_MIN_OVR_SPREAD         = 15    # ...spanning at least this much OVR variety
_MAX_PERSONAL_SWING     = 4     # cap the nudge so a noisy sample can't overcorrect


def _personal_difficulty_slope(player: dict):
    """
    Fits a simple linear trend of difficulty-adjusted efficiency vs. opponent
    OVR across a player's own match_history. A positive slope means their
    normalised efficiency actually climbs against tougher defenses (they
    play up); negative means it falls off faster than the generic curve
    assumes (they play down). Returns None when there isn't enough data
    (too few games, or games too clustered in OVR) to trust the fit.
    """
    history = player.get("match_history", [])
    if len(history) < _MIN_GAMES_FOR_PERSONAL:
        return None
    ovrs = [m.get("opp_ovr", 220) for m in history]
    if max(ovrs) - min(ovrs) < _MIN_OVR_SPREAD:
        return None
    effs = [_game_efficiency(m) for m in history]
    n        = len(ovrs)
    mean_ovr = sum(ovrs) / n
    mean_eff = sum(effs) / n
    denom = sum((o - mean_ovr) ** 2 for o in ovrs)
    if denom == 0:
        return None
    slope = sum((o - mean_ovr) * (e - mean_eff) for o, e in zip(ovrs, effs)) / denom
    return slope, mean_ovr


def predict_score(eff_ppd: float, opp_ovr: int | None = None, player: dict | None = None) -> int:
    """
    Expected points for the drive: eff_ppd × 3 drives, difficulty-adjusted.

    Applies the generic difficulty curve first (tougher defenses compress
    the expected score for everyone). If `player` has enough match_history
    to fit a personal difficulty trend (see _personal_difficulty_slope),
    that trend nudges the result further — rewarding players who've shown
    they genuinely play up against tough competition, and tempering
    players who've shown they fade against it — capped so a small/noisy
    sample can't swing the prediction too hard.

    Returns nearest even number capped at 24.
    """
    base = eff_ppd * 3
    if opp_ovr:
        # Slight compression for very tough defenses (230+)
        factor = max(0.78, 1.0 - max(0, opp_ovr - 220) * 0.0012)
        base  *= factor

        if player is not None:
            fit = _personal_difficulty_slope(player)
            if fit is not None:
                slope, mean_ovr = fit
                nudge = slope * (opp_ovr - mean_ovr) * 3   # per-drive eff -> points
                nudge = max(-_MAX_PERSONAL_SWING, min(_MAX_PERSONAL_SWING, nudge))
                base += nudge
    return int(min(24, max(0, round(base / 2) * 2)))

DATA_FILE = os.path.join(os.path.dirname(__file__), "league_stats.json")

DEFAULT_PLAYERS = {
    # Tier 1                                                                                        career_avg_ppd from season rankings
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


def load_data():
    if not os.path.exists(DATA_FILE):
        return {"players": dict(DEFAULT_PLAYERS), "history": [], "matchup_history": []}
    try:
        with open(DATA_FILE, "r") as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError):
        return {"players": dict(DEFAULT_PLAYERS), "history": [], "matchup_history": []}
    # Backfill career_avg_ppd onto any existing player record that's missing it
    defaults_by_name = {v["name"]: v for v in DEFAULT_PLAYERS.values()}
    for key, player in data["players"].items():
        if not player.get("career_avg_ppd"):  # catches missing key AND None/0
            ref = DEFAULT_PLAYERS.get(key) or defaults_by_name.get(player.get("name", ""))
            if ref and ref.get("career_avg_ppd"):
                player["career_avg_ppd"] = ref["career_avg_ppd"]
    # Backfill missing "date" on old match_history entries (pre-dates the
    # 7-day/3-day ladder windows) — use the saved current_matchup date if
    # available, else fall back to today so they at least count as recent.
    fallback_date = (
        data.get("current_matchup", {}).get("date")
        or datetime.now().strftime("%Y-%m-%d")
    )
    for player in data["players"].values():
        for entry in player.get("match_history", []):
            entry.setdefault("date", fallback_date)
    # Ensure matchup_history key exists on older saves
    data.setdefault("matchup_history", [])
    return data

def _norm(name: str) -> str:
    """Normalise a player name for fuzzy matching: lowercase, strip all non-alphanumeric chars.
    D.A.G.O.A.T → dagoat   |   Magicmikey66 → magicmikey66   |   bohica7599 → bohica7599
    """
    import re
    return re.sub(r"[^a-z0-9]", "", name.lower())

def find_player_by_name(data: dict, name: str) -> tuple[str, dict] | tuple[None, None]:
    """Return (player_id, player_stats) matching 'name' field — exact first, then normalised,
    then digit-stripped (so 'Bohica' matches 'bohica7599', 'Mike' matches 'mike9413', etc.)."""
    import re as _re
    name_lower = name.lower()
    name_norm  = _norm(name)
    name_base  = _re.sub(r"\d+$", "", name_norm)   # strip trailing digits for pass 3
    # Pass 1: exact case-insensitive
    for p_id, p_stats in data["players"].items():
        if p_stats.get("name", "").lower() == name_lower:
            return p_id, p_stats
    # Pass 2: normalised (strips dots, underscores, spaces, etc.)
    for p_id, p_stats in data["players"].items():
        if _norm(p_stats.get("name", "")) == name_norm:
            return p_id, p_stats
    # Pass 3: digit-stripped prefix match — "Bohica" → "bohica" matches "bohica7599"
    if name_base:   # only if there's something left after stripping digits
        for p_id, p_stats in data["players"].items():
            stored_base = _re.sub(r"\d+$", "", _norm(p_stats.get("name", "")))
            if stored_base and stored_base == name_base:
                return p_id, p_stats
    return None, None

def find_player_by_discord_id(data: dict, discord_id: str) -> tuple[str, dict] | tuple[None, None]:
    """Return (player_id, player_stats) by stored discord_id field."""
    for p_id, p_stats in data["players"].items():
        if str(p_stats.get("discord_id", "")) == discord_id:
            return p_id, p_stats
    return None, None

def save_data(data):
    # Atomic write: temp file first, then replace to avoid partial writes
    dir_name = os.path.dirname(DATA_FILE)
    try:
        with tempfile.NamedTemporaryFile("w", dir=dir_name, delete=False, suffix=".tmp") as tf:
            json.dump(data, tf, indent=4)
            tmp_path = tf.name
        os.replace(tmp_path, DATA_FILE)
    except OSError as e:
        raise RuntimeError(f"Failed to save league data: {e}")

# 2. DYNAMIC MATCHUP CREATOR (BENCH-AWARE)
@bot.command(name="matchup")
@commands.has_permissions(administrator=True)
async def create_matchup(ctx, *, raw_input: str):
    """
    Usage: !matchup vs LeagueName, OppA=266, OppB=264, ...
    League name is optional — include it after 'vs' or as the first item without an '='.
    Excluded: explicitly benched players.
    """
    print(f"[DEBUG] create_matchup ENTERED, raw_input length={len(raw_input)}", flush=True)
    await ctx.send(f"🔧 DEBUG: matchup command received ({len(raw_input)} chars). Processing...")
    # ── Parse optional league name ─────────────────────────────────────────────
    opp_league = None
    # Check for "vs LeagueName," prefix
    vs_match = re.match(r'(?i)^\s*vs\.?\s+([^,\n]+?)(?:\s*[,\n]|$)', raw_input)
    if vs_match:
        opp_league  = vs_match.group(1).strip()
        raw_input   = raw_input[vs_match.end():]

    opponent_pairs = []
    # Split on commas OR newlines so users can paste one-per-line or comma-separated
    for item in re.split(r'[,\n]+', raw_input):
        item = item.strip()
        if not item:
            continue
        # Support both "Name=266" and "Name 266"
        if "=" in item:
            name, defense_raw = item.split("=", 1)
        else:
            # Try "TeamName 266" (space-separated, number at end)
            parts = item.rsplit(None, 1)
            if len(parts) == 2 and parts[1].isdigit():
                name, defense_raw = parts[0], parts[1]
            else:
                # No number found — treat as league name if we don't have one yet
                if opp_league is None:
                    opp_league = item
                continue
        defense_digits = re.sub(r'\D', '', defense_raw)
        if not defense_digits:
            continue
        opponent_pairs.append({"name": name.strip(), "defense": int(defense_digits)})
    print(f"[DEBUG] parsed {len(opponent_pairs)} opponent pairs, opp_league={opp_league!r}", flush=True)
    if not opponent_pairs:
        await ctx.send("⚠️ Invalid format! Use: `!matchup vs LeagueName, OppA=266, OppB=264, ...`")
        return

    try:
        # Toughest opponents first
        opponents_sorted = sorted(opponent_pairs, key=lambda x: x["defense"], reverse=True)

        # Build active roster: skip benched players AND players with 0 weekly drives
        async with _data_lock:
            data = load_data()
        roster_performance = []
        for p_id, p_stats in data["players"].items():
            if p_stats.get("is_benched", False):
                continue
            ppd_drives   = p_stats.get("ppd_drives", p_stats.get("total_drives", 0))
            weekly_score = p_stats.get("weekly", 0)
            # If no drives yet this week, fall back to career avg PPD for sorting
            ppd  = (weekly_score / ppd_drives) if ppd_drives > 0 else float(p_stats.get("career_avg_ppd") or 0)
            eff  = calc_efficiency(p_stats)
            comp = composite_score(p_stats)


            mention = f"<@{p_id}>" if p_id.isdigit() else f"@{p_stats.get('name', p_id)}"
            roster_performance.append({
                "mention": mention,
                "name":    p_stats.get("name", p_id),
                "ppd":     ppd,
                "eff":     eff,
                "comp":    comp,
                "_stats":  p_stats,
            })

        if not roster_performance:
            await ctx.send("⚠️ No active players found. Players need to log at least one score before running `!matchup`.")
            return

        # Best composite scorer vs toughest opponent; cap at 16 active slots
        roster_sorted = sorted(roster_performance, key=lambda x: x["comp"], reverse=True)[:16]

        # Record assignments (player_name -> {opponent_name, opponent_ovr})
        assignments = {}
        max_matches = min(len(opponents_sorted), len(roster_sorted))
        for i in range(max_matches):
            opp = opponents_sorted[i]
            ros = roster_sorted[i]
            assignments[ros["name"]] = {
                "opponent_name": opp["name"],
                "opponent_ovr":  opp["defense"],
            }

        # Persist assignments so !score / !swapmatch / !postmatchup can use them
        async with _data_lock:
            data = load_data()
            # Preserve channel_id if already set
            existing = data.get("current_matchup", {})
            data["current_matchup"] = {
                "date":        datetime.now().strftime("%Y-%m-%d"),
                "opp_league":  opp_league,
                "assignments": assignments,
            }
            if existing.get("channel_id"):
                data["current_matchup"]["channel_id"] = existing["channel_id"]
            save_data(data)

        # ── Post a text preview so admin can review / swap before !postmatchup ──
        lines = []
        for i, (ros, opp) in enumerate(
            zip(roster_sorted[:max_matches], opponents_sorted[:max_matches]), start=1
        ):
            ppd_str = f"{ros['ppd']:.2f}"
            eff_str = f"{ros['eff']:.2f}"
            exp     = predict_score(ros["eff"], opp["defense"], player=ros.get("_stats"))
            lines.append(
                f"`{i:>2}.` **{ros['name']}** (PPD {ppd_str} · EFF {eff_str} · EXP ~{exp})"
                f"  vs  **{opp['name']}** ({opp['defense']} OVR)"
            )

        # Players with no opponent (more players than opponents)
        for ros in roster_sorted[max_matches:]:
            lines.append(f"`  ` **{ros['name']}** — no opponent assigned")

        opp_str = f" vs **{opp_league}**" if opp_league else ""
        embed = discord.Embed(
            title=f"⚔️ Proposed Matchups{opp_str}",
            description=(
                "Review below, then use `!swapmatch Player1 Player2` to swap opponents.\n"
                "Run `!postmatchup` when you're ready to post the scoreboard card."
            ),
            color=discord.Color.purple(),
        )

        chunk, chunk_len, field_idx = [], 0, 0
        for line in lines:
            if chunk_len + len(line) + 1 > 1000 and chunk:
                embed.add_field(
                    name="Matchups" if field_idx == 0 else "Matchups (cont.)",
                    value="\n".join(chunk), inline=False,
                )
                chunk, chunk_len, field_idx = [], 0, field_idx + 1
            chunk.append(line)
            chunk_len += len(line) + 1
        if chunk:
            embed.add_field(
                name="Matchups" if field_idx == 0 else "Matchups (cont.)",
                value="\n".join(chunk), inline=False,
            )

        await _try_delete(ctx.message)
        await ctx.send(embed=embed)

    except Exception as e:
        # Safety net: make sure a failure is always visible in Discord, not just the console.
        import traceback
        tb = traceback.format_exc()
        print(f"[ERROR] !matchup raised:\n{tb}")
        await ctx.send(f"❌ `!matchup` failed: `{type(e).__name__}: {e}`\n(Full traceback printed to console.)")

# 2b. REPOST CURRENT MATCHUP IMAGE
@bot.command(name="postmatchup")
async def repost_matchup(ctx, *, channel=None, purge=True):
    """Reposts the current matchup card. ctx may be None when called from on_ready (pass channel= instead)."""
    channel = channel or (ctx.channel if ctx else None)
    async with _data_lock:
        data = load_data()
        # Persist channel so on_ready can re-post after restarts — do this
        # inside the same lock so we never need a second acquisition later.
        if channel:
            data.setdefault("current_matchup", {})["channel_id"] = channel.id
            save_data(data)

    matchup = data.get("current_matchup", {})
    assignments = matchup.get("assignments", {})
    if not assignments:
        if ctx:
            await ctx.send("⚠️ No matchup is set yet. Run `!matchup` first.")
        return

    # Purge all messages before posting a fresh card (skip on bot startup)
    if channel and purge and ctx is not None:
        try:
            await channel.purge(limit=1000)
        except discord.Forbidden:
            async for msg in channel.history(limit=1000):
                try:
                    await msg.delete()
                except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                    pass

    # Rebuild matchups_dict — ALL active (non-benched) players, sorted by
    # ladder score (career + 7-day + 3-day, difficulty-adjusted per game).
    opp_league    = matchup.get("opp_league")
    matchups_dict = {}

    def _sort_key(p):
        return composite_score(p)

    for p_stats in sorted(data["players"].values(), key=_sort_key, reverse=True):
        if p_stats.get("is_benched", False):
            continue
        pname = p_stats.get("name", "")
        info  = assignments.get(pname)
        if not info:
            continue   # skip players with no matchup assignment this week
        ppd_drives = p_stats.get("ppd_drives", 0)
        weekly     = p_stats.get("weekly", 0)
        ppd        = weekly / ppd_drives if ppd_drives > 0 else float(p_stats.get("career_avg_ppd") or 0)
        eff        = calc_efficiency(p_stats)
        net        = weekly - p_stats.get("weekly_allowed", 0)   # offense scored - defense allowed, week-to-date
        matchups_dict[pname] = {
            "opponent":  info["opponent_name"],
            "def":       info["opponent_ovr"],
            "ppd":       ppd,
            "eff":       eff,
            "expected":  predict_score(eff, info["opponent_ovr"], player=p_stats),
            "our_score": info.get("last_score"),   # today's round only
            "opp_score": info.get("opp_score"),
            "off_ovr":   p_stats.get("off_ovr"),
            "net":       net,                       # weekly +/- : offense - defense allowed
        }

    logo = os.path.join(os.path.dirname(__file__), "dv_logo.png")
    try:
        img_bytes = generate_matchup_sheet(matchups_dict, logo_path=logo, opp_league=opp_league)
        date_str = matchup.get("date", "")
        opp_str  = f" vs {opp_league}" if opp_league else ""
        caption  = f"📋 Current matchups{f' ({date_str}{opp_str})' if date_str else ''}:"
        if ctx:
            await _try_delete(ctx.message)
        if channel:
            await channel.send(caption, file=discord.File(fp=img_bytes, filename="matchup_card.png"))
    except Exception as e:
        if ctx:
            await ctx.send(f"⚠️ Could not generate matchup image: {e}")

# 2c. SWAP MATCHUP — swap opponents between two of our players
@bot.command(name="swapmatch")
@commands.has_permissions(administrator=True)
async def swap_match(ctx, player1: str, player2: str):
    """
    Swap the opponents assigned to two players.
    Usage: !swapmatch SunDevilTyler Thrillhouse
    """
    async with _data_lock:
        data = load_data()
        assignments = data.get("current_matchup", {}).get("assignments", {})
        if not assignments:
            await ctx.send("⚠️ No matchup is set. Run `!matchup` first.")
            return

        # Case-insensitive lookup in assignments
        key1 = next((k for k in assignments if k.lower() == player1.lower()), None)
        key2 = next((k for k in assignments if k.lower() == player2.lower()), None)

        if key1 is None:
            await ctx.send(f"❌ **{player1}** has no assignment in the current matchup.")
            return
        if key2 is None:
            await ctx.send(f"❌ **{player2}** has no assignment in the current matchup.")
            return
        if key1 == key2:
            await ctx.send(f"⚠️ **{key1}** and **{key2}** are the same player — nothing to swap.")
            return

        # Swap and capture new opponents before releasing the lock
        assignments[key1], assignments[key2] = assignments[key2], assignments[key1]
        opp1 = assignments[key1]["opponent_name"]
        opp2 = assignments[key2]["opponent_name"]
        try:
            save_data(data)
        except RuntimeError as e:
            await ctx.send(f"❌ Could not save swap — {e}")
            return

    await _try_delete(ctx.message)
    await ctx.send(
        f"🔀 Matchup swapped!\n"
        f"• **{key1}** → now hits **{opp1}**\n"
        f"• **{key2}** → now hits **{opp2}**"
    )

# 2d. SET MATCHUP — manually assign any player to any opponent
@bot.command(name="setmatch")
@commands.has_permissions(administrator=True)
async def set_match(ctx, player_name: str, opponent_name: str, opp_ovr: int):
    """
    Manually assign a player to a specific opponent.
    Usage: !setmatch SunDevilTyler SkattPack 266
    Creates the assignment if it doesn't exist yet.
    """
    if not (100 <= opp_ovr <= 400):
        await ctx.send("⚠️ OVR must be between 100 and 400.")
        return

    async with _data_lock:
        data = load_data()
        matchup = data.get("current_matchup", {})
        if not matchup:
            await ctx.send("⚠️ No matchup is set. Run `!matchup` first.")
            return

        # Case-insensitive match against roster names
        p_id, player = find_player_by_name(data, player_name)
        if player is None:
            await ctx.send(f"❌ **{player_name}** not found on the roster.")
            return

        canonical_name = player["name"]
        assignments = matchup.setdefault("assignments", {})

        old = assignments.get(canonical_name)
        assignments[canonical_name] = {
            "opponent_name": opponent_name,
            "opponent_ovr":  opp_ovr,
        }
        try:
            save_data(data)
        except RuntimeError as e:
            await ctx.send(f"❌ Could not save assignment — {e}")
            return

    await _try_delete(ctx.message)
    if old:
        await ctx.send(
            f"✏️ Updated **{canonical_name}**'s matchup:\n"
            f"• Was: **{old['opponent_name']}** ({old['opponent_ovr']} OVR)\n"
            f"• Now: **{opponent_name}** ({opp_ovr} OVR)"
        )
    else:
        await ctx.send(
            f"✅ **{canonical_name}** assigned to **{opponent_name}** ({opp_ovr} OVR)."
        )

# 2e. ROSTER PREP — roster status, rotation, add players
@bot.command(name="prep")
@commands.has_permissions(administrator=True)
async def prep(ctx):
    """Full roster overview for pre-matchup rotation planning."""
    async with _data_lock:
        data = load_data()

    cur_assignments = data.get("current_matchup", {}).get("assignments", {})
    active, benched = [], []
    for p_id, p_stats in data["players"].items():
        ppd_drives = p_stats.get("ppd_drives", 0)
        weekly     = p_stats.get("weekly", 0)
        raw_ppd    = weekly / ppd_drives if ppd_drives > 0 else 0.0
        eff        = calc_efficiency(p_stats)
        cur_asgn   = cur_assignments.get(p_stats.get("name", ""), {})
        cur_ovr    = cur_asgn.get("opponent_ovr", 0)
        comp = composite_score(p_stats)

        exp        = predict_score(eff, cur_ovr or None, player=p_stats)
        off_ovr    = p_stats.get("off_ovr", 0)
        entry = {
            "name":    p_stats.get("name", p_id),
            "off_ovr": off_ovr,
            "eff":     eff,
            "ppd":     raw_ppd,
            "comp":    comp,
            "expected": exp,
        }
        (benched if p_stats.get("is_benched", False) else active).append(entry)

    active.sort( key=lambda x: x["comp"], reverse=True)
    benched.sort(key=lambda x: x["comp"], reverse=True)
    total = len(active) + len(benched)

    def _row(i, p, show_exp=True):
        icon    = "🟢" if p["eff"] >= 6.0 else "🔴"
        ovr_str = f"OVR {p['off_ovr']} | " if p["off_ovr"] else ""
        exp_str = f" | Exp: ~{p['expected']} pts" if show_exp else ""
        return f"`{i:>2}.` {icon} **{p['name']}** | {ovr_str}Eff: {p['eff']:.2f}{exp_str}"

    active_lines  = "\n".join(_row(i, p)        for i, p in enumerate(active,  1)) or "*None*"
    benched_lines = "\n".join(_row(i, p, False) for i, p in enumerate(benched, 1)) or "*None*"

    embed = discord.Embed(
        title=f"📋 ROSTER PREP — {total}/18 Slots",
        color=discord.Color.teal(),
    )
    embed.add_field(name=f"🟢 ACTIVE ({len(active)})",  value=active_lines,  inline=False)
    embed.add_field(name=f"💤 BENCHED ({len(benched)})", value=benched_lines, inline=False)
    embed.set_footer(text="!addplayer Name [OVR]  •  !setovr Name OVR  •  !bench/@active to rotate")

    await _try_delete(ctx.message)
    await ctx.send(embed=embed)

@bot.command(name="addplayer")
@commands.has_permissions(administrator=True)
async def add_player(ctx, name: str, off_ovr: int = 0):
    """Add a new player: !addplayer PlayerName [OVR]"""
    async with _data_lock:
        data = load_data()
        if len(data["players"]) >= 18:
            await ctx.send("⚠️ Roster is full (18 max). Bench or remove someone first.")
            return
        _, existing = find_player_by_name(data, name)
        if existing is not None:
            await ctx.send(f"⚠️ **{name}** already exists on the roster.")
            return
        data["players"][name] = {
            "name": name,
            "weekly": 0, "monthly": 0, "yearly": 0,
            "total_drives": 0, "ppd_drives": 0,
            "is_benched": False,
            "off_ovr": off_ovr,
            "match_history": [],
            "weekly_allowed": 0, "monthly_allowed": 0,
        }
        save_data(data)
    await _try_delete(ctx.message)
    ovr_str = f" (OVR {off_ovr})" if off_ovr else ""
    await ctx.send(f"✅ **{name}**{ovr_str} added to the roster.")

@bot.command(name="setovr")
@commands.has_permissions(administrator=True)
async def set_ovr(ctx, player_name: str, ovr: int):
    """Admin: set any player's offensive OVR: !setovr PlayerName 265"""
    if not (50 <= ovr <= 330):
        await ctx.send("⚠️ OVR must be between 50 and 330.")
        return
    async with _data_lock:
        data = load_data()
        p_id, player = find_player_by_name(data, player_name)
        if player is None:
            await ctx.send(f"❌ Player **{player_name}** not found.")
            return
        player["off_ovr"] = ovr
        save_data(data)
    await _try_delete(ctx.message)
    await ctx.send(f"✅ **{player_name}**'s offensive OVR set to **{ovr}**.")

@bot.command(name="myovr")
async def my_ovr(ctx, ovr: int):
    """Set your own offensive OVR (no admin required): !myovr 265"""
    if not (50 <= ovr <= 330):
        await ctx.send("⚠️ OVR must be between 50 and 330.")
        return
    async with _data_lock:
        data = load_data()
        author_id = str(ctx.author.id)
        if author_id in data["players"]:
            player = data["players"][author_id]
        else:
            _, player = find_player_by_name(data, ctx.author.display_name)
            if player is None:
                _, player = find_player_by_name(data, ctx.author.name)
        if player is None:
            await ctx.send(
                f"❌ No roster entry for **{ctx.author.display_name}**. "
                "Ask an admin to add you first with `!addplayer`."
            )
            return
        player["off_ovr"] = ovr
        save_data(data)
    await _try_delete(ctx.message)
    await ctx.send(f"✅ **{player['name']}** offensive OVR set to **{ovr}**.")

# 3. BENCH / ACTIVE COMMANDS (admin only)
@bot.command(name="bench")
@commands.has_permissions(administrator=True)
async def bench_player(ctx, member: discord.Member):
    """Usage: !bench @Player — excludes them from matchups until reactivated."""
    async with _data_lock:
        data = load_data()
        user_id = str(member.id)
        # Look for an existing record by discord_id, then by roster name —
        # most players are keyed by roster NAME (from !addplayer), not by
        # Discord ID, so checking "user_id not in data['players']" alone
        # would miss them and create a blank duplicate entry instead.
        p_id, player = find_player_by_discord_id(data, user_id)
        if player is None:
            p_id, player = find_player_by_name(data, member.name)
        if player is None:
            p_id, player = find_player_by_name(data, member.display_name)

        if player is None:
            data["players"][user_id] = {
                "name": member.name,
                "weekly": 0, "monthly": 0, "yearly": 0,
                "total_drives": 0, "ppd_drives": 0, "is_benched": True
            }
            canonical = member.name
        else:
            player["is_benched"] = True
            player["discord_id"] = user_id   # stamp for future lookups
            canonical = player.get("name", member.name)
        save_data(data)
    await _try_delete(ctx.message)
    await ctx.send(f"💤 **{canonical}** has been **benched** and will be skipped in matchups.")

@bot.command(name="active")
@commands.has_permissions(administrator=True)
async def activate_player(ctx, member: discord.Member):
    """Usage: !active @Player — returns them to the active matchup pool."""
    async with _data_lock:
        data = load_data()
        user_id = str(member.id)
        p_id, player = find_player_by_discord_id(data, user_id)
        if player is None:
            p_id, player = find_player_by_name(data, member.name)
        if player is None:
            p_id, player = find_player_by_name(data, member.display_name)

        if player is None:
            data["players"][user_id] = {
                "name": member.name,
                "weekly": 0, "monthly": 0, "yearly": 0,
                "total_drives": 0, "ppd_drives": 0, "is_benched": False
            }
            canonical = member.name
        else:
            player["is_benched"] = False
            player["discord_id"] = user_id
            canonical = player.get("name", member.name)
        save_data(data)
    await _try_delete(ctx.message)
    await ctx.send(f"🟢 **{canonical}** is now **active** and will be included in matchups.")


# 4. REGISTER — players link their Discord account to their roster name once
@bot.command(name="register")
async def register(ctx, roster_name: str):
    """
    Link your Discord account to your roster name so !score / !gave work without typing your name.
    Usage: !register Thrillhouse
    """
    async with _data_lock:
        data = load_data()
        p_id, player = find_player_by_name(data, roster_name)
        if player is None:
            await ctx.send(
                f"❌ **{roster_name}** not found on the roster. "
                f"Check the spelling — roster names are case-sensitive-ish."
            )
            return
        author_id = str(ctx.author.id)
        # Check if someone else already claimed this slot
        existing_id = str(player.get("discord_id", ""))
        if existing_id and existing_id != author_id:
            await ctx.send(
                f"⚠️ **{player['name']}** is already linked to a different Discord account. "
                f"Ask an admin to fix it."
            )
            return
        player["discord_id"] = author_id
        try:
            save_data(data)
        except RuntimeError as e:
            await ctx.send(f"❌ Could not save registration — {e}")
            return
    await _try_delete(ctx.message)
    await ctx.send(
        f"✅ You're registered as **{player['name']}**! "
        f"You can now use `!score 24`, `!gave 14`, etc. without typing your name.",
        delete_after=30
    )

# 4b. LOG SCORES — OVR is auto-looked up from today's !matchup assignments
@bot.command(name="score")
async def score(ctx, *args):
    """
    Log your own score:        !score 24
    Log someone else's score:  !score PlayerName 24
    Flag a fumble (this round's drives won't count toward PPD/ladder history):
        !score 16 fumble
        !score PlayerName 16 fumble
    OVR is pulled from today's matchup automatically.
    """
    # ── Parse flexible arguments ───────────────────────────────────────────────
    fumble = False
    if len(args) == 1:
        # !score 24  — player is the command author
        try:
            points = int(args[0])
        except ValueError:
            await ctx.send("Usage: `!score 24` or `!score PlayerName 24` (add `fumble` at the end to exclude from PPD)")
            return
        lookup_name = None   # will resolve by Discord ID / username below
    elif len(args) == 2:
        if args[1].lower() == "fumble":
            # !score 24 fumble
            try:
                points = int(args[0])
            except ValueError:
                await ctx.send("Usage: `!score 24 fumble`")
                return
            lookup_name = None
            fumble = True
        else:
            # !score PlayerName 24
            lookup_name = args[0]
            try:
                points = int(args[1])
            except ValueError:
                await ctx.send("Usage: `!score PlayerName 24`")
                return
    elif len(args) == 3 and args[2].lower() == "fumble":
        # !score PlayerName 24 fumble
        lookup_name = args[0]
        try:
            points = int(args[1])
        except ValueError:
            await ctx.send("Usage: `!score PlayerName 24 fumble`")
            return
        fumble = True
    else:
        await ctx.send("Usage: `!score 24` or `!score PlayerName 24` (add `fumble` at the end to exclude from PPD)")
        return

    if points < 0 or points > 42:
        await ctx.send(f"⚠️ Invalid points ({points}). Must be between 0–42.")
        return

    async with _data_lock:
        data = load_data()

        # ── Resolve player ────────────────────────────────────────────────────
        if lookup_name is None:
            # Author self-reporting — same priority order as _resolve_self
            p_id, player = _resolve_self(ctx, data)
            if player is None:
                await ctx.send(
                    f"❌ Couldn't find a roster entry for **{ctx.author.display_name}**. "
                    f"Run `!register YourRosterName` once, or use `!score YourRosterName 24`."
                )
                return
        else:
            p_id, player = find_player_by_name(data, lookup_name)
            if player is None:
                await ctx.send(f"❌ Player '{lookup_name}' not found. Check the spelling and try again.")
                return

        # ── Matchup lookup ────────────────────────────────────────────────────
        player_name = player.get("name", lookup_name or ctx.author.display_name)
        matchup     = data.get("current_matchup", {})
        assignment  = matchup.get("assignments", {}).get(player_name)
        if not assignment:
            await ctx.send(
                f"⚠️ No matchup assignment found for **{player_name}**. "
                f"Run `!matchup` first to set today's pairings."
            )
            return
        opp_ovr  = assignment["opponent_ovr"]
        opp_name = assignment["opponent_name"]

        # ── Update stats ──────────────────────────────────────────────────────
        player["weekly"]       = player.get("weekly",       0) + points
        player["monthly"]      = player.get("monthly",      0) + points
        player["yearly"]       = player.get("yearly",       0) + points
        player["total_drives"] = player.get("total_drives", 0) + 3
        if not fumble:
            player["ppd_drives"] = player.get("ppd_drives", 0) + 3
            if "match_history" not in player:
                player["match_history"] = []
            player["match_history"].append({
                "points": points, "opp_ovr": opp_ovr, "opponent": opp_name,
                "date": datetime.now().strftime("%Y-%m-%d"),
            })
        # Store today's round score on the assignment so the card shows it,
        # even on a fumble — the round still happened, it just doesn't
        # count toward PPD/ladder history.
        assignment["last_score"] = points
        try:
            save_data(data)
        except RuntimeError as e:
            await ctx.send(f"❌ Could not save score — {e}")
            return

    await _try_delete(ctx.message)
    await repost_matchup(ctx)
    fumble_note = " *(fumble — excluded from PPD)*" if fumble else ""
    await ctx.send(f"🏈 **{player_name}** scored **{points}** vs {opp_name} ({opp_ovr} OVR){fumble_note}", delete_after=15)

@bot.command(name="combo")
async def combo(ctx, *args):
    """
    Log your offense score AND defense points allowed in one command.
    Self:   !combo 18 7          (scored 18, gave up 7)
    Admin:  !combo PlayerName 18 7
    Equivalent to running !score and !gave back to back.
    """
    if len(args) == 2:
        lookup_name = None
        try:
            off_points, def_points = int(args[0]), int(args[1])
        except ValueError:
            await ctx.send("Usage: `!combo 18 7`  or  `!combo PlayerName 18 7`")
            return
    elif len(args) == 3:
        lookup_name = args[0]
        try:
            off_points, def_points = int(args[1]), int(args[2])
        except ValueError:
            await ctx.send("Usage: `!combo PlayerName 18 7`")
            return
    else:
        await ctx.send("Usage: `!combo 18 7`  or  `!combo PlayerName 18 7`")
        return

    if off_points < 0 or off_points > 42:
        await ctx.send(f"⚠️ Invalid offense points ({off_points}). Must be between 0–42.")
        return
    if def_points < 0:
        await ctx.send(f"⚠️ Invalid defense points ({def_points}). Must be 0 or greater.")
        return

    async with _data_lock:
        data = load_data()

        if lookup_name is None:
            p_id, player = _resolve_self(ctx, data)
            if player is None:
                await ctx.send(
                    f"❌ Couldn't find a roster entry for **{ctx.author.display_name}**. "
                    f"Run `!register YourRosterName` once, or use `!combo YourRosterName 18 7`."
                )
                return
        else:
            p_id, player = find_player_by_name(data, lookup_name)
            if player is None:
                await ctx.send(f"❌ Player '{lookup_name}' not found. Check the spelling and try again.")
                return

        player_name = player.get("name", lookup_name or ctx.author.display_name)
        matchup     = data.get("current_matchup", {})
        assignment  = matchup.get("assignments", {}).get(player_name)
        if not assignment:
            await ctx.send(
                f"⚠️ No matchup assignment found for **{player_name}**. "
                f"Run `!matchup` first to set today's pairings."
            )
            return
        opp_ovr  = assignment["opponent_ovr"]
        opp_name = assignment["opponent_name"]

        # ── Offense side (same as !score) ──────────────────────────────────
        player["weekly"]       = player.get("weekly",       0) + off_points
        player["monthly"]      = player.get("monthly",      0) + off_points
        player["yearly"]       = player.get("yearly",       0) + off_points
        player["total_drives"] = player.get("total_drives", 0) + 3
        player["ppd_drives"]   = player.get("ppd_drives",   0) + 3
        player.setdefault("match_history", []).append({
            "points": off_points, "opp_ovr": opp_ovr, "opponent": opp_name,
            "date": datetime.now().strftime("%Y-%m-%d"),
        })
        assignment["last_score"] = off_points

        # ── Defense side (same as !gave) ───────────────────────────────────
        player["points_allowed"]  = player.get("points_allowed",  0) + def_points
        player["weekly_allowed"]  = player.get("weekly_allowed",  0) + def_points
        player["monthly_allowed"] = player.get("monthly_allowed", 0) + def_points
        assignment["opp_score"] = (assignment.get("opp_score") or 0) + def_points
        player.setdefault("defense_history", []).append({
            "points": def_points, "opponent": opp_name,
            "date": datetime.now().strftime("%Y-%m-%d"),
        })

        try:
            save_data(data)
        except RuntimeError as e:
            await ctx.send(f"❌ Could not save — {e}")
            return

    await _try_delete(ctx.message)
    await repost_matchup(ctx)
    await ctx.send(
        f"🏈🛡️ **{player_name}** scored **{off_points}** and gave up **{def_points}** vs {opp_name} ({opp_ovr} OVR)",
        delete_after=15
    )

@bot.command(name="fixscore")
async def fix_score(ctx, *args):
    """
    Correct a wrongly entered offensive score.
    Self:   !fixscore <old> <new>          e.g. !fixscore 14 20
    Admin:  !fixscore <PlayerName> <old> <new>
    """
    if len(args) == 2:
        lookup_name = None
        try:
            old_pts, new_pts = int(args[0]), int(args[1])
        except ValueError:
            await ctx.send("Usage: `!fixscore 14 20`  or  `!fixscore PlayerName 14 20`")
            return
    elif len(args) == 3:
        lookup_name = args[0]
        try:
            old_pts, new_pts = int(args[1]), int(args[2])
        except ValueError:
            await ctx.send("Usage: `!fixscore PlayerName 14 20`")
            return
    else:
        await ctx.send("Usage: `!fixscore 14 20`  or  `!fixscore PlayerName 14 20`")
        return

    for pts in (old_pts, new_pts):
        if pts < 0:
            await ctx.send(f"⚠️ Score {pts} is invalid — must be 0 or greater.")
            return

    # Only admins may fix someone else's score
    if lookup_name is not None and not ctx.author.guild_permissions.administrator:
        await ctx.send("⚠️ Only admins can correct another player's score.")
        return

    async with _data_lock:
        data = load_data()
        if lookup_name is None:
            p_id, player = _resolve_self(ctx, data)
            if player is None:
                await ctx.send(f"❌ Couldn't find **{ctx.author.display_name}** on the roster. Use `!fixscore PlayerName old new`.")
                return
        else:
            p_id, player = find_player_by_name(data, lookup_name)
            if player is None:
                await ctx.send(f"❌ Player **{lookup_name}** not found.")
                return

        canonical = player.get("name", lookup_name or ctx.author.display_name)
        diff = new_pts - old_pts

        # Adjust aggregates
        player["weekly"]  = max(0, player.get("weekly",  0) + diff)
        player["monthly"] = max(0, player.get("monthly", 0) + diff)
        player["yearly"]  = max(0, player.get("yearly",  0) + diff)

        # Fix the most recent match_history entry that matches old_pts
        history = player.get("match_history", [])
        history_fixed = False
        for entry in reversed(history):
            if entry.get("points") == old_pts:
                entry["points"] = new_pts
                history_fixed = True
                break

        # Fix assignment last_score if it matches
        matchup = data.get("current_matchup", {})
        asgn = matchup.get("assignments", {}).get(canonical)
        if asgn and asgn.get("last_score") == old_pts:
            asgn["last_score"] = new_pts

        try:
            save_data(data)
        except RuntimeError as e:
            await ctx.send(f"❌ Could not save — {e}")
            return

    await _try_delete(ctx.message)
    await repost_matchup(ctx)
    note = "" if history_fixed else " *(no matching history entry — totals adjusted anyway)*"
    await ctx.send(f"✏️ **{canonical}** score corrected: {old_pts} → {new_pts} pts.{note}", delete_after=15)

@bot.command(name="fixgave")
async def fix_gave(ctx, *args):
    """
    Correct a wrongly entered defensive (gave up) score.
    Self:   !fixgave <old> <new>           e.g. !fixgave 20 14
    Admin:  !fixgave <PlayerName> <old> <new>
    """
    if len(args) == 2:
        lookup_name = None
        try:
            old_pts, new_pts = int(args[0]), int(args[1])
        except ValueError:
            await ctx.send("Usage: `!fixgave 20 14`  or  `!fixgave PlayerName 20 14`")
            return
    elif len(args) == 3:
        lookup_name = args[0]
        try:
            old_pts, new_pts = int(args[1]), int(args[2])
        except ValueError:
            await ctx.send("Usage: `!fixgave PlayerName 20 14`")
            return
    else:
        await ctx.send("Usage: `!fixgave 20 14`  or  `!fixgave PlayerName 20 14`")
        return

    for pts in (old_pts, new_pts):
        if pts < 0:
            await ctx.send(f"⚠️ Score {pts} is invalid — must be 0 or greater.")
            return

    # Only admins may fix someone else's gave-up score
    if lookup_name is not None and not ctx.author.guild_permissions.administrator:
        await ctx.send("⚠️ Only admins can correct another player's gave-up score.")
        return

    async with _data_lock:
        data = load_data()
        if lookup_name is None:
            p_id, player = _resolve_self(ctx, data)
            if player is None:
                await ctx.send(f"❌ Couldn't find **{ctx.author.display_name}** on the roster. Use `!fixgave PlayerName old new`.")
                return
        else:
            p_id, player = find_player_by_name(data, lookup_name)
            if player is None:
                await ctx.send(f"❌ Player **{lookup_name}** not found.")
                return

        canonical = player.get("name", lookup_name or ctx.author.display_name)
        diff = new_pts - old_pts

        # Adjust defensive aggregates
        player["points_allowed"]  = max(0, player.get("points_allowed",  0) + diff)
        player["weekly_allowed"]  = max(0, player.get("weekly_allowed",  0) + diff)
        player["monthly_allowed"] = max(0, player.get("monthly_allowed", 0) + diff)

        # Fix assignment opp_score
        matchup = data.get("current_matchup", {})
        asgn = matchup.get("assignments", {}).get(canonical)
        if asgn is not None:
            old_opp = asgn.get("opp_score") or 0
            asgn["opp_score"] = max(0, old_opp + diff)

        # Fix the most recent matching defense_history entry so !endround's
        # persistent-history lookup reflects the correction too
        dh = player.get("defense_history", [])
        for entry in reversed(dh):
            if entry.get("points") == old_pts:
                entry["points"] = new_pts
                break

        try:
            save_data(data)
        except RuntimeError as e:
            await ctx.send(f"❌ Could not save — {e}")
            return

    await _try_delete(ctx.message)
    await repost_matchup(ctx)
    await ctx.send(f"🛡️ **{canonical}** gave-up score corrected: {old_pts} → {new_pts} pts.", delete_after=15)

# 5. FORCE SCORE (admin only) — manually log points for any player
@bot.command(name="forcescore")
@commands.has_permissions(administrator=True)
async def force_score(ctx, player_arg: str, *, rest: str):
    """
    Usage: !forcescore bohica7599 22
           !forcescore @Kirito 24
           !forcescore @Kirito 16 fumble
    Accepts a plain roster name OR a Discord @mention.
    """
    # ── Parse "22" or "16 fumble" out of `rest` ─────────────────────────────
    parts = rest.split()
    if not parts or not parts[0].lstrip("-").isdigit():
        await ctx.send("Usage: `!forcescore PlayerName 22`  or  `!forcescore PlayerName 16 fumble`")
        return
    score  = int(parts[0])
    fumble = parts[1] if len(parts) > 1 else ""

    if score > 42 or score < 0:
        await ctx.send(f"⚠️ Invalid total ({score}). Must be between 0–42.")
        return

    is_fumble = fumble.lower() == "fumble"

    async with _data_lock:
        data = load_data()

        # Resolve name: strip mention formatting if present, then match roster name
        raw_name = player_arg.strip("<@!>").strip()
        player_key = None

        def _find_by_any_name(*candidates):
            """Return first player key whose stored name normalises to any candidate norm."""
            norms = {_norm(c) for c in candidates if c}
            exact = {c.lower() for c in candidates if c}
            # exact case-insensitive first
            for k, p in data["players"].items():
                if p.get("name", "").lower() in exact:
                    return k
            # normalised fallback (handles D.A.G.O.A.T → DAGOAT etc.)
            for k, p in data["players"].items():
                if _norm(p.get("name", "")) in norms:
                    return k
            return None

        # 1. Plain name typed directly
        if not raw_name.isdigit():
            player_key = _find_by_any_name(raw_name)

        # 2. Discord mention (raw_name is now a numeric ID)
        if player_key is None and raw_name.isdigit():
            if raw_name in data["players"]:
                player_key = raw_name
            else:
                member_obj = ctx.guild.get_member(int(raw_name))
                if member_obj is None:
                    try:
                        member_obj = await ctx.guild.fetch_member(int(raw_name))
                    except Exception:
                        member_obj = None
                if member_obj:
                    player_key = _find_by_any_name(member_obj.name, member_obj.display_name)

        if player_key is None:
            await ctx.send(f"⚠️ Player **{player_arg}** not found in roster. Check the spelling and try again.")
            return

        player = data["players"][player_key]
        player["weekly"]       += score
        player["monthly"]      += score
        player["yearly"]       += score
        player["total_drives"] += 3
        if not is_fumble:
            player["ppd_drives"] += 3

        # Track in match_history so all-time PPD stays accurate
        if not is_fumble:
            matchup   = data.get("current_matchup", {})
            asgn      = matchup.get("assignments", {}).get(player.get("name", ""))
            opp_ovr   = asgn["opponent_ovr"]  if asgn else 0
            opp_name  = asgn["opponent_name"] if asgn else "Unknown"
            player.setdefault("match_history", []).append(
                {"points": score, "opp_ovr": opp_ovr, "opponent": opp_name,
                 "date": datetime.now().strftime("%Y-%m-%d")}
            )
            if asgn:
                asgn["last_score"] = score

        try:
            save_data(data)
        except RuntimeError as e:
            await ctx.send(f"❌ Could not save score — {e}")
            return

    note = f"🛠️ Admin override: **{score}** pts for **{player['name']}**"
    if is_fumble:
        note += " *(fumble — drive excluded from PPD)*"
    await _try_delete(ctx.message)
    await send_live_leaderboard(ctx, note)
    await repost_matchup(ctx)


# 6. LEADERBOARD DISPLAY
@bot.command(name="stats")
async def show_stats(ctx, timeframe: str = "weekly"):
    """
    !stats           — weekly scores & PPD
    !stats monthly   — monthly totals
    !stats yearly    — yearly totals
    !stats alltime   — all-time PPD computed from full match history
    """
    tf = timeframe.lower()
    if tf not in ["weekly", "monthly", "yearly", "alltime"]:
        await ctx.send("Use `!stats weekly`, `!stats monthly`, `!stats yearly`, or `!stats alltime`!")
        return

    data = load_data()

    if tf == "alltime":
        # Compute all-time PPD from each player's full match_history
        rows = []
        for player in data["players"].values():
            mh = player.get("match_history", [])
            total_pts = sum(m.get("points", 0) for m in mh)
            total_drv = len(mh) * 3
            at_ppd    = total_pts / total_drv if total_drv > 0 else player.get("career_avg_ppd") or 0.0
            games     = len(mh)
            rows.append((player.get("name", "?"), at_ppd, games, player.get("is_benched", False)))

        rows.sort(key=lambda r: r[1], reverse=True)
        embed = discord.Embed(
            title="📊 All-Time PPD Leaderboard",
            description="Points Per Drive across every recorded game.\n*(Use `!stats weekly` for current-week totals.)*",
            color=discord.Color.blurple(),
        )
        desc = ""
        for idx, (name, at_ppd, games, benched) in enumerate(rows, 1):
            status = " 💤" if benched else ""
            desc += f"**{idx}. {name}**{status} — PPD: **{at_ppd:.3f}** *(over {games} game{'s' if games != 1 else ''})*\n"
        embed.description = desc or "No match history recorded yet."
        await _try_delete(ctx.message)
        await ctx.send(embed=embed)
        return

    leaderboard = sorted(data["players"].values(), key=lambda x: x.get(tf, 0), reverse=True)

    if not leaderboard:
        await ctx.send("No scoring records logged yet.")
        return

    embed = discord.Embed(title=f"🏆 {tf.capitalize()} Leaderboard", color=discord.Color.gold())
    desc = ""
    for idx, player in enumerate(leaderboard, 1):
        ppd_drives  = max(player.get("ppd_drives", player.get("drives", 1)), 1)
        current_ppd = player.get(tf, 0) / ppd_drives
        # All-time PPD from match_history (fallback to stored career_avg_ppd)
        mh = player.get("match_history", [])
        if mh:
            at_pts = sum(m.get("points", 0) for m in mh)
            at_drv = len(mh) * 3
            at_ppd = at_pts / at_drv if at_drv > 0 else None
        else:
            at_ppd = player.get("career_avg_ppd")
        career_str  = f" | All-time: **{at_ppd:.2f}**" if at_ppd is not None else ""
        status      = " 💤 *(Benched)*" if player.get("is_benched", False) else ""
        desc += (
            f"**{idx}. {player.get('name', 'Unknown')}** — {player.get(tf, 0)} Pts "
            f"*(PPD: {current_ppd:.2f}{career_str})*{status}\n"
        )

    embed.description = desc
    await _try_delete(ctx.message)
    await ctx.send(embed=embed)

# 7. CLEAR STATS (admin only)
@bot.command(name="clearstats")
@commands.has_permissions(administrator=True)
async def clear_stats(ctx, timeframe: str = "weekly"):
    """Usage: !clearstats weekly | !clearstats monthly | !clearstats yearly"""
    tf = timeframe.lower()
    if tf not in ["weekly", "monthly", "yearly"]:
        await ctx.send("Use `!clearstats weekly`, `!clearstats monthly`, or `!clearstats yearly`!")
        return

    async with _data_lock:
        data = load_data()
        for player in data["players"].values():
            player[tf] = 0
            if tf == "weekly":
                player["total_drives"]   = 0
                player["ppd_drives"]     = 0
                player["weekly_allowed"] = 0
            if tf == "monthly":
                player["monthly_allowed"] = 0
        save_data(data)

    await _try_delete(ctx.message)
    await ctx.send(f"🧹 Clear complete! All **{tf}** scores and drive tracking reset to 0.")

# 7b. WEEKLY RESET — clears active week stats, preserves match_history and career PPD
@bot.command(name="resetweek")
@commands.has_permissions(administrator=True)
async def resetweek(ctx):
    """Resets weekly scoring and defensive counters for all players while preserving long-term PPD history."""
    async with _data_lock:
        data = load_data()

        # ── Snapshot completed matchup to history before clearing ──────────────
        current = data.get("current_matchup", {})
        if current.get("assignments"):
            snapshot = {
                "date":       current.get("date", datetime.now().strftime("%Y-%m-%d")),
                "opp_league": current.get("opp_league") or "Unknown",
                "matchups":   [],
            }
            our_total = 0
            opp_total = 0
            for pname, asgn in current["assignments"].items():
                _, p_stats = find_player_by_name(data, pname)
                our_sc = p_stats.get("weekly", 0) if p_stats else 0
                opp_sc = asgn.get("opp_score")
                snapshot["matchups"].append({
                    "our_player": pname,
                    "our_score":  our_sc,
                    "opp_player": asgn.get("opponent_name", "?"),
                    "opp_ovr":    asgn.get("opponent_ovr",  0),
                    "opp_score":  opp_sc,
                })
                our_total += our_sc
                if opp_sc is not None:
                    opp_total += opp_sc
            snapshot["our_total"] = our_total
            snapshot["opp_total"] = opp_total if any(
                m["opp_score"] is not None for m in snapshot["matchups"]
            ) else None
            data.setdefault("matchup_history", []).append(snapshot)

        for player in data["players"].values():
            # Active-week counters — cleared each week
            player["weekly"]          = 0
            player["total_drives"]    = 0
            player["ppd_drives"]      = 0
            player["drives"]          = 0   # legacy field, kept in sync
            player["points_allowed"]  = 0
            player["weekly_allowed"]  = 0
            player["defensive_tds"]   = 0
            player["defensive_2pts"]  = 0
            player["safeties"]        = 0
            if "match_history" not in player:
                player["match_history"] = []
        # Clear the current matchup so !score requires a fresh !matchup call
        data["current_matchup"] = {}
        try:
            save_data(data)
        except RuntimeError as e:
            await ctx.send(f"❌ Could not save reset — {e}")
            return

    await _try_delete(ctx.message)
    await ctx.send(
        "🔄 **Weekly reset complete!**\n"
        "• Matchup results saved to history (`!history` to view).\n"
        "• Weekly scores, drives, and defensive counters cleared.\n"
        "• Match history and career PPD averages are preserved.\n"
        "• Run `!matchup` to set new pairings for the week."
    )

# 8. NAMED-PLAYER DEFENSIVE COMMANDS
# These use player names (as in the roster) rather than Discord mentions.
# !gave tracks points allowed separately; !pick6 / !pick2 / !safety add bonus points to PPD score.

def _resolve_self(ctx, data) -> tuple[str, dict] | tuple[None, None]:
    """Resolve the command author to a roster entry.
    Order: stored discord_id → key match → display_name → username.
    When found by name, stamps discord_id onto the record for next time.
    """
    author_id = str(ctx.author.id)
    # 1. Stored discord_id on any player record (most reliable after first use)
    p_id, player = find_player_by_discord_id(data, author_id)
    if player:
        return p_id, player
    # 2. Key is a raw Discord ID (legacy format)
    if author_id in data["players"]:
        p = data["players"][author_id]
        p["discord_id"] = author_id   # stamp for future
        return author_id, p
    # 3. Name-based fallback — display_name then username
    for candidate in (ctx.author.display_name, ctx.author.name):
        p_id, player = find_player_by_name(data, candidate)
        if player:
            player["discord_id"] = author_id   # stamp so next lookup skips this
            return p_id, player
    return None, None

@bot.command(name="gave")
async def gave(ctx, *args):
    """
    Log how many points your defense gave up this round.
    Self-report:  !gave 14
    Admin log:    !gave SunDevilTyler 14
    """
    # Parse: !gave 14  OR  !gave PlayerName 14
    if len(args) == 1:
        try:
            points = int(args[0])
        except ValueError:
            await ctx.send("Usage: `!gave 14`  or  `!gave PlayerName 14`")
            return
        lookup_name = None
    elif len(args) == 2:
        lookup_name = args[0]
        try:
            points = int(args[1])
        except ValueError:
            await ctx.send("Usage: `!gave PlayerName 14`")
            return
    else:
        await ctx.send("Usage: `!gave 14`  or  `!gave PlayerName 14`")
        return

    if points < 0:
        await ctx.send(f"⚠️ Invalid points ({points}). Must be 0 or greater.")
        return

    async with _data_lock:
        data = load_data()
        if lookup_name is None:
            p_id, player = _resolve_self(ctx, data)
            if player is None:
                print(f"[gave] _resolve_self failed for {ctx.author} (id={ctx.author.id}, display={ctx.author.display_name!r}, name={ctx.author.name!r})")
                await ctx.send(
                    f"❌ Couldn't find **{ctx.author.display_name}** on the roster. "
                    f"Run `!register YourRosterName` first, or type `!gave YourRosterName {points}`."
                )
                return
        else:
            p_id, player = find_player_by_name(data, lookup_name)
            if player is None:
                await ctx.send(f"❌ Player **{lookup_name}** not found. Check the spelling and try again.")
                return

        canonical = player.get("name", lookup_name or ctx.author.display_name)
        player["points_allowed"]  = player.get("points_allowed",  0) + points
        player["weekly_allowed"]  = player.get("weekly_allowed",  0) + points
        player["monthly_allowed"] = player.get("monthly_allowed", 0) + points

        matchup = data.get("current_matchup", {})
        asgn    = matchup.get("assignments", {}).get(canonical)
        opp_name_for_history = asgn.get("opponent_name", "Unknown") if asgn else "Unknown"
        if asgn is not None:
            asgn["opp_score"] = (asgn.get("opp_score") or 0) + points
        player.setdefault("defense_history", []).append({
            "points": points, "opponent": opp_name_for_history,
            "date": datetime.now().strftime("%Y-%m-%d"),
        })

        try:
            save_data(data)
        except RuntimeError as e:
            await ctx.send(f"❌ Could not save — {e}")
            return

    await _try_delete(ctx.message)
    await repost_matchup(ctx)
    await ctx.send(f"🛡️ **{canonical}** gave up **{points}** pts. Logged.", delete_after=15)

@bot.command(name="pick6")
async def pick6(ctx, *args):
    """
    Log a defensive touchdown (+6 pts).
    Self-report:  !pick6
    Admin log:    !pick6 SunDevilTyler
    """
    lookup_name = args[0] if args else None
    async with _data_lock:
        data = load_data()
        if lookup_name is None:
            p_id, player = _resolve_self(ctx, data)
            if player is None:
                await ctx.send(f"❌ Couldn't find **{ctx.author.display_name}** on the roster.")
                return
        else:
            p_id, player = find_player_by_name(data, lookup_name)
            if player is None:
                await ctx.send(f"❌ Player **{lookup_name}** not found.")
                return
        canonical = player.get("name", lookup_name or ctx.author.display_name)
        player["defensive_tds"] = player.get("defensive_tds", 0) + 1
        player["weekly"]  = player.get("weekly",  0) + 6
        player["monthly"] = player.get("monthly", 0) + 6
        player["yearly"]  = player.get("yearly",  0) + 6
        # Reflect the bonus on the scoreboard card's displayed score too —
        # previously this only updated the background totals, so the card
        # looked frozen even though PPD (which reads weekly directly) moved.
        assignment = data.get("current_matchup", {}).get("assignments", {}).get(canonical)
        if assignment is not None:
            assignment["last_score"] = (assignment.get("last_score") or 0) + 6
        try:
            save_data(data)
        except RuntimeError as e:
            await ctx.send(f"❌ Could not save — {e}")
            return

    await _try_delete(ctx.message)
    await repost_matchup(ctx)
    await ctx.send(f"🔥 **PICK-6!** +6 pts to **{canonical}**", delete_after=15)

@bot.command(name="pick2")
async def pick2(ctx, *args):
    """
    Log a defensive 2-point conversion return (+2 pts).
    Self-report:  !pick2
    Admin log:    !pick2 SunDevilTyler
    """
    lookup_name = args[0] if args else None
    async with _data_lock:
        data = load_data()
        if lookup_name is None:
            p_id, player = _resolve_self(ctx, data)
            if player is None:
                await ctx.send(f"❌ Couldn't find **{ctx.author.display_name}** on the roster.")
                return
        else:
            p_id, player = find_player_by_name(data, lookup_name)
            if player is None:
                await ctx.send(f"❌ Player **{lookup_name}** not found.")
                return
        canonical = player.get("name", lookup_name or ctx.author.display_name)
        player["defensive_2pts"] = player.get("defensive_2pts", 0) + 1
        player["weekly"]  = player.get("weekly",  0) + 2
        player["monthly"] = player.get("monthly", 0) + 2
        player["yearly"]  = player.get("yearly",  0) + 2
        assignment = data.get("current_matchup", {}).get("assignments", {}).get(canonical)
        if assignment is not None:
            assignment["last_score"] = (assignment.get("last_score") or 0) + 2
        try:
            save_data(data)
        except RuntimeError as e:
            await ctx.send(f"❌ Could not save — {e}")
            return

    await _try_delete(ctx.message)
    await repost_matchup(ctx)
    await ctx.send(f"🔒 **DEF 2-PT!** +2 pts to **{canonical}**", delete_after=15)

@bot.command(name="safety")
async def safety(ctx, *args):
    """
    Log a defensive safety (+2 pts).
    Self-report:  !safety
    Admin log:    !safety SunDevilTyler
    """
    lookup_name = args[0] if args else None
    async with _data_lock:
        data = load_data()
        if lookup_name is None:
            p_id, player = _resolve_self(ctx, data)
            if player is None:
                await ctx.send(f"❌ Couldn't find **{ctx.author.display_name}** on the roster.")
                return
        else:
            p_id, player = find_player_by_name(data, lookup_name)
            if player is None:
                await ctx.send(f"❌ Player **{lookup_name}** not found.")
                return
        canonical = player.get("name", lookup_name or ctx.author.display_name)
        player["safeties"] = player.get("safeties", 0) + 1
        player["weekly"]  = player.get("weekly",  0) + 2
        player["monthly"] = player.get("monthly", 0) + 2
        player["yearly"]  = player.get("yearly",  0) + 2
        assignment = data.get("current_matchup", {}).get("assignments", {}).get(canonical)
        if assignment is not None:
            assignment["last_score"] = (assignment.get("last_score") or 0) + 2
        try:
            save_data(data)
        except RuntimeError as e:
            await ctx.send(f"❌ Could not save — {e}")
            return

    await _try_delete(ctx.message)
    await repost_matchup(ctx)
    await ctx.send(f"💥 **SAFETY!** +2 pts to **{canonical}**", delete_after=15)

@bot.command(name="gavepick6")
async def gave_pick6(ctx, *args):
    """
    Log a defensive touchdown scored AGAINST you (+6 pts allowed).
    Self-report:  !gavepick6
    Admin log:    !gavepick6 SunDevilTyler
    """
    lookup_name = args[0] if args else None
    async with _data_lock:
        data = load_data()
        if lookup_name is None:
            p_id, player = _resolve_self(ctx, data)
            if player is None:
                await ctx.send(f"❌ Couldn't find **{ctx.author.display_name}** on the roster.")
                return
        else:
            p_id, player = find_player_by_name(data, lookup_name)
            if player is None:
                await ctx.send(f"❌ Player **{lookup_name}** not found.")
                return
        canonical = player.get("name", lookup_name or ctx.author.display_name)
        player["points_allowed"]  = player.get("points_allowed",  0) + 6
        player["weekly_allowed"]  = player.get("weekly_allowed",  0) + 6
        player["monthly_allowed"] = player.get("monthly_allowed", 0) + 6

        matchup = data.get("current_matchup", {})
        asgn    = matchup.get("assignments", {}).get(canonical)
        opp_name_for_history = asgn.get("opponent_name", "Unknown") if asgn else "Unknown"
        if asgn is not None:
            asgn["opp_score"] = (asgn.get("opp_score") or 0) + 6
        player.setdefault("defense_history", []).append({
            "points": 6, "opponent": opp_name_for_history,
            "date": datetime.now().strftime("%Y-%m-%d"),
        })
        try:
            save_data(data)
        except RuntimeError as e:
            await ctx.send(f"❌ Could not save — {e}")
            return

    await _try_delete(ctx.message)
    await repost_matchup(ctx)
    await ctx.send(f"💀 **PICK-6 allowed!** +6 pts against **{canonical}**", delete_after=15)

@bot.command(name="gavepick2")
async def gave_pick2(ctx, *args):
    """
    Log a defensive 2-point conversion return scored AGAINST you (+2 pts allowed).
    Self-report:  !gavepick2
    Admin log:    !gavepick2 SunDevilTyler
    """
    lookup_name = args[0] if args else None
    async with _data_lock:
        data = load_data()
        if lookup_name is None:
            p_id, player = _resolve_self(ctx, data)
            if player is None:
                await ctx.send(f"❌ Couldn't find **{ctx.author.display_name}** on the roster.")
                return
        else:
            p_id, player = find_player_by_name(data, lookup_name)
            if player is None:
                await ctx.send(f"❌ Player **{lookup_name}** not found.")
                return
        canonical = player.get("name", lookup_name or ctx.author.display_name)
        player["points_allowed"]  = player.get("points_allowed",  0) + 2
        player["weekly_allowed"]  = player.get("weekly_allowed",  0) + 2
        player["monthly_allowed"] = player.get("monthly_allowed", 0) + 2

        matchup = data.get("current_matchup", {})
        asgn    = matchup.get("assignments", {}).get(canonical)
        opp_name_for_history = asgn.get("opponent_name", "Unknown") if asgn else "Unknown"
        if asgn is not None:
            asgn["opp_score"] = (asgn.get("opp_score") or 0) + 2
        player.setdefault("defense_history", []).append({
            "points": 2, "opponent": opp_name_for_history,
            "date": datetime.now().strftime("%Y-%m-%d"),
        })
        try:
            save_data(data)
        except RuntimeError as e:
            await ctx.send(f"❌ Could not save — {e}")
            return

    await _try_delete(ctx.message)
    await repost_matchup(ctx)
    await ctx.send(f"💀 **DEF 2-PT allowed!** +2 pts against **{canonical}**", delete_after=15)

@bot.command(name="gavesafety")
async def gave_safety(ctx, *args):
    """
    Log a safety scored AGAINST you (+2 pts allowed).
    Self-report:  !gavesafety
    Admin log:    !gavesafety SunDevilTyler
    """
    lookup_name = args[0] if args else None
    async with _data_lock:
        data = load_data()
        if lookup_name is None:
            p_id, player = _resolve_self(ctx, data)
            if player is None:
                await ctx.send(f"❌ Couldn't find **{ctx.author.display_name}** on the roster.")
                return
        else:
            p_id, player = find_player_by_name(data, lookup_name)
            if player is None:
                await ctx.send(f"❌ Player **{lookup_name}** not found.")
                return
        canonical = player.get("name", lookup_name or ctx.author.display_name)
        player["points_allowed"]  = player.get("points_allowed",  0) + 2
        player["weekly_allowed"]  = player.get("weekly_allowed",  0) + 2
        player["monthly_allowed"] = player.get("monthly_allowed", 0) + 2

        matchup = data.get("current_matchup", {})
        asgn    = matchup.get("assignments", {}).get(canonical)
        opp_name_for_history = asgn.get("opponent_name", "Unknown") if asgn else "Unknown"
        if asgn is not None:
            asgn["opp_score"] = (asgn.get("opp_score") or 0) + 2
        player.setdefault("defense_history", []).append({
            "points": 2, "opponent": opp_name_for_history,
            "date": datetime.now().strftime("%Y-%m-%d"),
        })
        try:
            save_data(data)
        except RuntimeError as e:
            await ctx.send(f"❌ Could not save — {e}")
            return

    await _try_delete(ctx.message)
    await repost_matchup(ctx)
    await ctx.send(f"💀 **Safety allowed!** +2 pts against **{canonical}**", delete_after=15)

# 9. LIVE LEADERBOARD HELPERS

async def send_live_leaderboard(ctx, update_msg=""):
    global _last_leaderboard
    data = load_data()
    matchup = data.get("current_matchup", {})
    leaderboard_data = []

    for key, stats in data["players"].items():
        display    = stats.get("name", key)
        ppd_drives = stats.get("ppd_drives", stats.get("total_drives", stats.get("drives", 0)))
        weekly_pts = stats.get("weekly", 0)
        raw_ppd    = weekly_pts / ppd_drives if ppd_drives > 0 else 0.0
        eff        = calc_efficiency(stats)
        comp       = composite_score(stats)
        career     = stats.get("career_avg_ppd")
        def_pts    = (stats.get("defensive_tds", 0) * 6
                    + stats.get("defensive_2pts", 0) * 2
                    + stats.get("safeties", 0) * 2)

        # Net score: what you scored minus what you gave up (weekly)
        weekly_allowed = stats.get("weekly_allowed", 0)
        net_weekly     = weekly_pts - weekly_allowed

        # Monthly net +/- avg per game
        monthly_pts     = stats.get("monthly", 0)
        monthly_allowed = stats.get("monthly_allowed", 0)
        monthly_net     = monthly_pts - monthly_allowed
        games_played    = ppd_drives // 3 if ppd_drives >= 3 else 0
        mo_avg_net      = monthly_net / games_played if games_played > 0 else 0.0

        # Today's expected score (needs active matchup assignment)
        assignment = matchup.get("assignments", {}).get(display)
        opp_ovr    = assignment["opponent_ovr"] if assignment else None
        exp        = predict_score(eff, opp_ovr, player=stats)

        leaderboard_data.append({
            "name": display, "ppd": raw_ppd, "eff": eff, "comp": comp,
            "drives": ppd_drives, "pts": weekly_pts, "def_pts": def_pts,
            "career": career, "net": net_weekly, "mo_avg_net": mo_avg_net,
            "expected": exp, "benched": stats.get("is_benched", False),
        })

    leaderboard_data.sort(key=lambda x: x["comp"], reverse=True)

    lines = []
    for idx, p in enumerate(leaderboard_data, 1):
        icon       = "🟢" if p["eff"] >= 6.0 else "🔴"
        net_str    = f"+{p['net']}"    if p["net"]        >= 0 else str(p["net"])
        mo_str     = f"+{p['mo_avg_net']:.1f}" if p["mo_avg_net"] >= 0 else f"{p['mo_avg_net']:.1f}"
        career_str = f" | Career {p['career']:.2f}" if p["career"] is not None else ""
        exp_str    = f"~{p['expected']}" if p["expected"] > 0 else "—"
        bench_str  = " 💤" if p["benched"] else ""
        lines.append(
            f"{icon} **#{idx} {p['name']}{bench_str}** — "
            f"Eff: **{p['eff']:.2f}**{career_str} | "
            f"PPD: {p['ppd']:.2f} | "
            f"Net: **{net_str}** (Mo avg: {mo_str}) | "
            f"Exp: **{exp_str}**"
        )

    embed = discord.Embed(
        title="🛡️ DV REBORN LIVE LEADERBOARD 🛡️",
        color=discord.Color.blue(),
        description="\n".join(lines),
    )
    if update_msg:
        embed.set_footer(text=update_msg)

    # Replace old leaderboard — only one visible at a time
    if _last_leaderboard is not None:
        try:
            await _last_leaderboard.delete()
        except (discord.NotFound, discord.Forbidden):
            pass
        _last_leaderboard = None
    _last_leaderboard = await ctx.send(embed=embed)

@bot.command(name="board")
async def board(ctx):
    """Display the live leaderboard: !board"""
    await _try_delete(ctx.message)
    await send_live_leaderboard(ctx, "Manual Board Request")

@bot.command(name="ovrstats")
async def ovrstats(ctx):
    """Show PPD broken down by opponent OVR tier: !ovrstats"""
    data = load_data()
    header = "Player         Vs 105+ OVR    Vs 100-104 OVR    Vs <100 OVR\n===========================================================\n"
    rows = ""
    for key, stats in data["players"].items():
        display = stats.get("name", key)
        history = stats.get("match_history", [])
        t1_pts, t1_dr = 0, 0
        t2_pts, t2_dr = 0, 0
        t3_pts, t3_dr = 0, 0
        for match in history:
            ovr = match["opp_ovr"]
            pts = match["points"]
            if ovr >= 105:
                t1_pts += pts
                t1_dr += 3
            elif 100 <= ovr <= 104:
                t2_pts += pts
                t2_dr += 3
            else:
                t3_pts += pts
                t3_dr += 3
        t1_ppd = f"{t1_pts/t1_dr:.2f}" if t1_dr > 0 else "-"
        t2_ppd = f"{t2_pts/t2_dr:.2f}" if t2_dr > 0 else "-"
        t3_ppd = f"{t3_pts/t3_dr:.2f}" if t3_dr > 0 else "-"
        rows += f"{display:<15}{t1_ppd:<15}{t2_ppd:<18}{t3_ppd}\n"
    await _try_delete(ctx.message)
    await ctx.send(f"📊 **DEFENSIVE OVR TIER REPORT**\n```text\n{header}{rows}```")
@bot.command(name="setscore")
@commands.has_permissions(administrator=True)
async def set_score_legacy(ctx, player_name: str, score_val: int):
    """
    Directly overwrite a player's weekly score total (admin only).
    Usage: !setscore Swarm 24
    Prefer !fixscore to correct a specific wrong entry.
    """
    async with _data_lock:
        data = load_data()
        p_id, player = find_player_by_name(data, player_name)
        if player is None:
            await ctx.send(f"❌ Player **{player_name}** not found on the roster.")
            return
        if score_val < 0:
            await ctx.send("⚠️ Score must be 0 or greater.")
            return
        canonical = player.get("name", player_name)
        old_score = player.get("weekly", 0)
        player["weekly"] = score_val
        # Keep the matchup card's displayed score in sync
        asgn = data.get("current_matchup", {}).get("assignments", {}).get(canonical)
        if asgn is not None:
            asgn["last_score"] = score_val
        try:
            save_data(data)
        except RuntimeError as e:
            await ctx.send(f"❌ Could not save — {e}")
            return

    await _try_delete(ctx.message)
    await ctx.send(f"✅ **{canonical}** weekly score set: {old_score} → {score_val} pts.", delete_after=10)
    await repost_matchup(ctx)


# 10. ROUND & WEEK MANAGEMENT

@bot.command(name="endround")
@commands.has_permissions(administrator=True)
async def end_round(ctx):
    """
    Purge ALL messages in the channel, then post the current scoreboard card.
    Scores are NOT reset — players carry their totals into the next round.
    Shows week-to-date totals on both sides (our weekly points vs their
    weekly points allowed), so the record/score comparison is apples-to-
    apples all week, not just this round — and naturally resets itself
    once !endweek zeroes out the weekly counters.
    Run !endweek when the full week is done.
    """
    # ── Delete the triggering command first ───────────────────────────────────
    await _try_delete(ctx.message)

    # ── Purge every message in the channel ───────────────────────────────────
    try:
        await ctx.channel.purge(limit=1000)
    except discord.Forbidden:
        # No Manage Messages — fall back to one-by-one
        async for msg in ctx.channel.history(limit=1000):
            try:
                await msg.delete()
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                pass

    # ── Build and post weekly stats embed ────────────────────────────────────
    data        = load_data()
    matchup     = data.get("current_matchup", {})
    assignments = matchup.get("assignments", {})
    opp_league  = matchup.get("opp_league") or "Opponents"

    if not assignments:
        await ctx.send("⚠️ No matchup is set yet. Run `!matchup` first.")
        return

    # Sort players by weekly score descending
    def _weekly(pname):
        _, ps = find_player_by_name(data, pname)
        return ps.get("weekly", 0) if ps else 0

    our_total  = 0
    opp_total  = 0
    wins = losses = ties = 0
    lines = []

    for pname, asgn in sorted(assignments.items(), key=lambda kv: _weekly(kv[0]), reverse=True):
        _, ps       = find_player_by_name(data, pname)
        our_sc      = ps.get("weekly", 0) if ps else 0
        gave_up     = ps.get("weekly_allowed", 0) if ps else 0
        ppd_drives  = ps.get("ppd_drives", 0) if ps else 0
        ppd         = round(our_sc / ppd_drives, 2) if ppd_drives > 0 else 0.0
        eff         = round(calc_efficiency(ps), 2) if ps else 0.0
        net         = our_sc - gave_up
        net_str     = f"+{net}" if net > 0 else str(net)

        # Opponent NAME still comes from each player's persistent match_history
        # (most recently played) rather than the live `assignments` dict —
        # that dict gets fully overwritten the moment !matchup runs again, so
        # if next round's pairings were already set before !endround was
        # called, `asgn` here would describe the WRONG (upcoming) opponent.
        mh = ps.get("match_history", []) if ps else []
        if mh:
            opp_nm = mh[-1].get("opponent", asgn.get("opponent_name", "?"))
        else:
            opp_nm = asgn.get("opponent_name", "?")

        # The SCORE comparison, though, uses full week-to-date totals on
        # both sides (weekly / weekly_allowed) — this is a weekly recap,
        # not a single-round one, so "our total" vs "their total" needs to
        # be the same kind of number on both sides of the dash.
        opp_sc = gave_up

        our_total += our_sc
        opp_total += opp_sc
        if our_sc > opp_sc:
            icon, wins = "✅", wins + 1
        elif our_sc < opp_sc:
            icon, losses = "❌", losses + 1
        else:
            icon, ties = "🤝", ties + 1
        sc_str = f"{our_sc}–{opp_sc}"

        # Line 1: result icon + name + score vs opp score
        # Line 2: PPD | EFF | NET (scored - allowed)
        lines.append(
            f"{icon} **{pname}** `{sc_str}` *(vs {opp_nm})*\n"
            f"  ↳ Total `{our_sc}` · PPD `{ppd}` · EFF `{eff}` · Net `{net_str}` (gave up `{gave_up}`)"
        )

    record_str = f"{wins}W–{losses}L" + (f"–{ties}T" if ties else "")
    embed = discord.Embed(
        title=f"🔔 Round Complete — DVR {our_total}  vs  {opp_league} {opp_total}",
        description=(
            f"Record this week: **{record_str}**\n"
            f"Scores carry into next round — run `!endweek` when the full week is done."
        ),
        color=discord.Color.orange(),
    )

    # Split into ≤1000-char field chunks to stay under Discord's limit
    chunk, chunk_len = [], 0
    field_idx = 0
    for line in lines:
        if chunk_len + len(line) + 1 > 1000 and chunk:
            embed.add_field(
                name="Weekly Stats" if field_idx == 0 else "Weekly Stats (cont.)",
                value="\n".join(chunk), inline=False,
            )
            chunk, chunk_len, field_idx = [], 0, field_idx + 1
        chunk.append(line)
        chunk_len += len(line) + 1
    if chunk:
        embed.add_field(
            name="Weekly Stats" if field_idx == 0 else "Weekly Stats (cont.)",
            value="\n".join(chunk), inline=False,
        )

    embed.set_footer(text=f"Snapshot {datetime.now().strftime('%Y-%m-%d %H:%M')} UTC")
    await ctx.channel.send(embed=embed)


@bot.command(name="endweek")
@commands.has_permissions(administrator=True)
async def end_week(ctx):
    """
    End the week: save results to history, update all-time PPD, reset weekly stats.
    Use !history afterwards to review results.
    """
    async with _data_lock:
        data = load_data()

        # ── Snapshot current matchup to matchup_history ──────────────────────
        current = data.get("current_matchup", {})
        if current.get("assignments"):
            snapshot = {
                "date":       current.get("date", datetime.now().strftime("%Y-%m-%d")),
                "opp_league": current.get("opp_league") or "Unknown",
                "matchups":   [],
            }
            our_total, opp_total = 0, 0
            for pname, asgn in current["assignments"].items():
                _, p_stats = find_player_by_name(data, pname)
                our_sc = p_stats.get("weekly", 0) if p_stats else 0
                opp_sc = asgn.get("opp_score")
                snapshot["matchups"].append({
                    "our_player": pname,
                    "our_score":  our_sc,
                    "opp_player": asgn.get("opponent_name", "?"),
                    "opp_ovr":    asgn.get("opponent_ovr",  0),
                    "opp_score":  opp_sc,
                })
                our_total += our_sc
                if opp_sc is not None:
                    opp_total += opp_sc
            snapshot["our_total"] = our_total
            snapshot["opp_total"] = opp_total if any(
                m["opp_score"] is not None for m in snapshot["matchups"]
            ) else None
            data.setdefault("matchup_history", []).append(snapshot)

        # ── Capture weekly Net (+/-) for the chart before we reset anything ───
        net_chart_data = []
        for player in data["players"].values():
            pname = player.get("name")
            if not pname:
                continue
            weekly, allowed = player.get("weekly", 0), player.get("weekly_allowed", 0)
            if weekly or allowed:   # only chart players who actually played this week
                net_chart_data.append((pname, weekly - allowed))

        # ── Update all-time PPD from full match_history before reset ─────────
        for player in data["players"].values():
            mh = player.get("match_history", [])
            if mh:
                total_pts = sum(m.get("points", 0) for m in mh)
                total_drv = len(mh) * 3
                if total_drv > 0:
                    player["alltime_ppd"] = round(total_pts / total_drv, 4)

        # ── Reset weekly counters ─────────────────────────────────────────────
        for player in data["players"].values():
            player["weekly"]          = 0
            player["total_drives"]    = 0
            player["ppd_drives"]      = 0
            player["drives"]          = 0
            player["points_allowed"]  = 0
            player["weekly_allowed"]  = 0
            player["defensive_tds"]   = 0
            player["defensive_2pts"]  = 0
            player["safeties"]        = 0
            player.setdefault("match_history", [])

        data["current_matchup"] = {}
        try:
            save_data(data)
        except RuntimeError as e:
            await ctx.send(f"❌ Could not save — {e}")
            return

    await _try_delete(ctx.message)
    await ctx.send(
        "✅ **Week ended!**\n"
        "• Results saved to history (`!history` to review).\n"
        "• All-time PPD updated for every player (`!stats alltime`).\n"
        "• Weekly scores and drives reset — ready for `!matchup`."
    )

    if net_chart_data:
        try:
            chart = generate_net_chart(
                net_chart_data,
                title=f"Weekly Net (+/-) — {datetime.now().strftime('%Y-%m-%d')}",
            )
            await ctx.send(file=discord.File(fp=chart, filename="weekly_net.png"))
        except Exception as e:
            await ctx.send(f"⚠️ Could not generate net chart: {e}")


# 11. MATCHUP HISTORY
@bot.command(name="history")
async def matchup_history(ctx, count: int = 5):
    """Show the last N completed matchup results: !history  or  !history 3"""
    data     = load_data()
    history  = data.get("matchup_history", [])
    if not history:
        await ctx.send("📭 No matchup history saved yet. History is stored when you run `!endweek`.")
        return

    recent = history[-min(count, len(history)):][::-1]  # newest first

    for snap in recent:
        date       = snap.get("date", "?")
        opp_league = snap.get("opp_league", "Unknown")
        our_total  = snap.get("our_total", 0)
        opp_total  = snap.get("opp_total")
        matchups   = snap.get("matchups", [])

        opp_str    = f"{opp_total}" if opp_total is not None else "?"
        if opp_total is not None:
            result = "✅ WIN" if our_total > opp_total else ("❌ LOSS" if our_total < opp_total else "🤝 TIE")
        else:
            result = "📊"

        embed = discord.Embed(
            title=f"{result}  DVR {our_total}  –  {opp_str}  {opp_league}",
            description=f"📅 {date}",
            color=discord.Color.green() if opp_total is not None and our_total > opp_total
                  else discord.Color.red() if opp_total is not None and our_total < opp_total
                  else discord.Color.gold(),
        )

        lines = []
        for m in sorted(matchups, key=lambda x: x.get("our_score", 0), reverse=True):
            our_sc  = m.get("our_score", 0)
            opp_sc  = m.get("opp_score")
            opp_nm  = m.get("opp_player", "?")[:16]
            opp_ovr = m.get("opp_ovr", 0)
            pname   = m.get("our_player", "?")[:14]

            if opp_sc is not None:
                wl = "✅" if our_sc > opp_sc else ("❌" if our_sc < opp_sc else "🤝")
                score_str = f"**{our_sc}** – {opp_sc}"
            else:
                wl = "•"
                score_str = f"**{our_sc}** – ?"

            lines.append(f"{wl} **{pname}** {score_str}  vs {opp_nm} ({opp_ovr})")

        # Chunk into ≤950-char fields to stay well under Discord's 1024 limit
        field_chunks, current, cur_len = [], [], 0
        for line in lines:
            if cur_len + len(line) + 1 > 950 and current:
                field_chunks.append("\n".join(current))
                current, cur_len = [], 0
            current.append(line)
            cur_len += len(line) + 1
        if current:
            field_chunks.append("\n".join(current))

        for i, chunk in enumerate(field_chunks or ["No data"]):
            label = "Matchups" if i == 0 else f"Matchups (cont.)"
            embed.add_field(name=label, value=chunk, inline=False)

        await ctx.send(embed=embed)

    await _try_delete(ctx.message)


# 12. SELF-TEST — exercises every command against a sandboxed copy of the data
class _FakeMessage:
    def __init__(self):
        self.id = 0
    async def delete(self):
        pass

class _FakeMember:
    """Stands in for a discord.Member — just enough attributes for !bench/!active
    to resolve against an existing roster record by name."""
    def __init__(self, name: str, member_id: int = 999999999):
        self.id           = member_id
        self.name         = name
        self.display_name = name
        self.mention      = f"@{name}"

class _FakeChannel:
    """Stands in for ctx.channel so purge()/send()/history() never touch Discord."""
    def __init__(self, real_channel):
        self.id           = getattr(real_channel, "id", 0)
        self.name         = getattr(real_channel, "name", "test")
        self.sent         = []
        self.sent_embeds  = []
    async def send(self, content=None, *args, **kwargs):
        self.sent.append(content if content is not None else "<embed/file>")
        if "embed" in kwargs and kwargs["embed"] is not None:
            self.sent_embeds.append(kwargs["embed"])
        return _FakeMessage()
    async def purge(self, *args, **kwargs):
        return []
    async def history(self, *args, **kwargs):
        return
        yield  # pragma: no cover — makes this an async generator, always empty

class _FakeCtx:
    """Stands in for discord.ext.commands.Context. Real author/guild (for permission
    checks & lookups), fake channel/message (so nothing real gets sent or deleted)."""
    def __init__(self, real_ctx):
        self.author      = real_ctx.author
        self.guild       = real_ctx.guild
        self.channel     = _FakeChannel(real_ctx.channel)
        self.message     = _FakeMessage()
        self.sent        = []
        self.sent_embeds = []
    async def send(self, content=None, **kwargs):
        self.sent.append(content if content is not None else "<embed/file>")
        if "embed" in kwargs and kwargs["embed"] is not None:
            self.sent_embeds.append(kwargs["embed"])
        return _FakeMessage()


@bot.command(name="test")
@commands.has_permissions(administrator=True)
async def test_all(ctx):
    """
    Self-test: runs every command against a sandboxed COPY of your real data.
    Nothing is saved to the live roster and no real Discord messages are sent
    or deleted — it's all captured and thrown away at the end.

    A PASS means the command ran without raising an exception — not that its
    output is exactly what you'd expect, so still spot-check anything that
    matters before trusting a change blindly.
    """
    global load_data, save_data
    await ctx.send("🧪 Running self-test against a sandbox copy of your data — your live roster is untouched...")

    async with _data_lock:
        real_data = load_data()
    sandbox = copy.deepcopy(real_data)

    _real_load, _real_save = load_data, save_data
    load_data = lambda: copy.deepcopy(sandbox)
    def _sandbox_save(d):
        sandbox.clear()
        sandbox.update(copy.deepcopy(d))
    save_data = _sandbox_save

    results = []  # (name, ok: bool, detail: str)
    def record(name, ok, detail=""):
        results.append((name, ok, detail))

    try:
        # ── Core scoring helpers (no Discord involved at all) ───────────────
        try:
            sample = next(iter(sandbox["players"].values()))
            calc_efficiency(sample)
            composite_score(sample)
            calc_windowed_efficiency(sample, 7)
            predict_score(6.5, 230)
            record("core scoring helpers", True)
        except Exception as e:
            record("core scoring helpers", False, f"{type(e).__name__}: {e}")

        try:
            for text in ("vs Test, A=200, B=210", "vs. Test2\nA=200\nB=210", "A=200, B=210"):
                re.match(r'(?i)^\s*vs\.?\s+([^,\n]+?)(?:\s*[,\n]|$)', text)
            record("!matchup regex parsing (vs / vs. / no league)", True)
        except Exception as e:
            record("!matchup regex parsing", False, f"{type(e).__name__}: {e}")

        roster_name = next(iter(sandbox["players"].values())).get("name", "TestPlayer")

        TEST_CALLS = {
            "matchup":     ((), {"raw_input": "vs TestOpp, Alpha=220, Beta=225, Gamma=230"}),
            "postmatchup": ((), {}),
            "prep":        ((), {}),
            "board":       ((), {}),
            "ovrstats":    ((), {}),
            "stats":       ((), {"timeframe": "weekly"}),
            "history":     ((), {"count": 3}),
            "addplayer":   (("__Test_Dummy__",), {"off_ovr": 200}),
            "setovr":      ((roster_name, 220), {}),
            "myovr":       ((220,), {}),
            "bench":       ((ctx.author,), {}),
            "active":      ((ctx.author,), {}),
            "register":    ((roster_name,), {}),
            "setscore":    ((roster_name, 20), {}),
            "endround":    ((), {}),
            "clearstats":  ((), {"timeframe": "weekly"}),
            "resetweek":   ((), {}),
            "endweek":     ((), {}),
        }

        async def run(name):
            cmd = bot.get_command(name)
            if cmd is None:
                record(name, False, "command not registered on bot")
                return
            call = TEST_CALLS.get(name)
            if call is None:
                record(name, False, "skipped — no valid test input available")
                return
            args, kwargs = call
            local_ctx = _FakeCtx(ctx)
            try:
                await cmd.callback(local_ctx, *args, **kwargs)
                record(name, True, f"{len(local_ctx.sent)} message(s)")
            except Exception as e:
                record(name, False, f"{type(e).__name__}: {e}")

        # !matchup first — gives us real assigned player names for everything below
        await run("matchup")
        assigned = list(sandbox.get("current_matchup", {}).get("assignments", {}).keys())
        p1 = assigned[0] if assigned else roster_name
        p2 = assigned[1] if len(assigned) > 1 else roster_name

        TEST_CALLS["swapmatch"]  = ((p1, p2), {})
        TEST_CALLS["setmatch"]   = ((p1, "TestOpp2", 225), {})
        TEST_CALLS["score"]      = ((p1, "24"), {})
        TEST_CALLS["gave"]       = ((p1, "14"), {})
        TEST_CALLS["combo"]      = ((p2, "18", "7"), {})
        TEST_CALLS["pick6"]      = ((p1,), {})
        TEST_CALLS["pick2"]      = ((p1,), {})
        TEST_CALLS["safety"]     = ((p1,), {})
        TEST_CALLS["gavepick6"]  = ((p1,), {})
        TEST_CALLS["gavepick2"]  = ((p1,), {})
        TEST_CALLS["gavesafety"] = ((p1,), {})
        TEST_CALLS["fixscore"]   = ((p1, "24", "20"), {})
        TEST_CALLS["fixgave"]    = ((p1, "14", "10"), {})
        TEST_CALLS["forcescore"] = ((p1,), {"rest": "22"})

        order = [
            "prep", "board", "ovrstats", "stats", "history",
            "addplayer", "setovr", "myovr", "bench", "active", "register",
            "postmatchup",
            "swapmatch" if p1 != p2 else None, "setmatch",
            "score", "gave", "combo", "pick6", "pick2", "safety",
            "gavepick6", "gavepick2", "gavesafety",
            "fixscore", "fixgave", "forcescore", "setscore",
            "endround",
            "clearstats", "resetweek", "endweek",
        ]
        for name in order:
            if name is None:
                continue
            await run(name)

        # ── !score ... fumble flag (verify it's excluded from PPD/history) ──
        try:
            score_cmd = bot.get_command("score")
            _, before = find_player_by_name(sandbox, p1)
            before_ppd_drives  = before.get("ppd_drives", 0)
            before_history_len = len(before.get("match_history", []))
            before_weekly      = before.get("weekly", 0)

            local_ctx = _FakeCtx(ctx)
            await score_cmd.callback(local_ctx, p1, "16", "fumble")

            _, after = find_player_by_name(sandbox, p1)
            after_ppd_drives  = after.get("ppd_drives", 0)
            after_history_len = len(after.get("match_history", []))
            after_weekly      = after.get("weekly", 0)

            ok = (
                after_weekly == before_weekly + 16
                and after_ppd_drives == before_ppd_drives
                and after_history_len == before_history_len
            )
            detail = (
                f"weekly +{after_weekly - before_weekly} (expected +16), "
                f"ppd_drives {before_ppd_drives}->{after_ppd_drives} (should be unchanged), "
                f"match_history {before_history_len}->{after_history_len} (should be unchanged)"
            )
            record("!score ... fumble excluded from PPD/history", ok, detail)
        except Exception as e:
            record("!score ... fumble excluded from PPD/history", False, f"{type(e).__name__}: {e}")

        # ── !pick6 / !gavepick6 must update the scoreboard card's score ─────
        # Regression check for the bug where bonus D plays (pick6/pick2/
        # safety) updated weekly/PPD in the background but left the card's
        # displayed last_score/opp_score frozen at the old number.
        try:
            pick6_cmd     = bot.get_command("pick6")
            gavepick6_cmd = bot.get_command("gavepick6")

            def _last_score(pname):
                asgn = sandbox.get("current_matchup", {}).get("assignments", {}).get(pname, {})
                return asgn.get("last_score") or 0, asgn.get("opp_score") or 0

            before_our, before_opp = _last_score(p1)
            local_ctx = _FakeCtx(ctx)
            await pick6_cmd.callback(local_ctx, p1)
            local_ctx2 = _FakeCtx(ctx)
            await gavepick6_cmd.callback(local_ctx2, p1)
            after_our, after_opp = _last_score(p1)

            ok = (after_our == before_our + 6) and (after_opp == before_opp + 6)
            detail = f"last_score {before_our}->{after_our} (expected +6), opp_score {before_opp}->{after_opp} (expected +6)"
            record("!pick6/!gavepick6 update the card's displayed score", ok, detail)
        except Exception as e:
            record("!pick6/!gavepick6 update the card's displayed score", False, f"{type(e).__name__}: {e}")

        # ── !endround regression check ──────────────────────────────────────
        # Reproduces the exact bug scenario: a round is played and logged,
        # then !matchup is run AGAIN (setting next round's pairings) BEFORE
        # !endround is called for the round that just finished. !endround's
        # summary must still show the round that was actually just played
        # (pulled from persistent match_history/defense_history), not the
        # new pairing that overwrote `current_matchup.assignments`.
        try:
            _, p1_before = find_player_by_name(sandbox, p1)
            round1_opp = None
            if p1_before.get("match_history"):
                round1_opp = p1_before["match_history"][-1].get("opponent")

            # Set up a brand new round's pairings for everyone, overwriting
            # the live assignments dict — this is the exact trigger condition.
            num_players = len(sandbox.get("players", {}))
            fake_opps   = ", ".join(f"ZZRegOpp{i}={220 + i}" for i in range(1, num_players + 2))
            matchup_cmd = bot.get_command("matchup")
            local_ctx   = _FakeCtx(ctx)
            await matchup_cmd.callback(local_ctx, raw_input=f"vs RegressionTestLeague, {fake_opps}")

            endround_cmd = bot.get_command("endround")
            local_ctx2   = _FakeCtx(ctx)
            await endround_cmd.callback(local_ctx2)

            p1_line = ""
            for emb in local_ctx2.sent_embeds:
                for f in emb.fields:
                    for line in f.value.split("\n"):
                        if f"**{p1}**" in line:
                            p1_line = line
                            break

            ok = bool(round1_opp) and bool(p1_line) and (round1_opp in p1_line) and ("ZZRegOpp" not in p1_line)
            detail = f"round1_opp={round1_opp!r}, p1's summary line={p1_line!r}"
            record("!endround shows round just played, not an overwritten future pairing", ok, detail)
        except Exception as e:
            record("!endround shows round just played, not an overwritten future pairing", False, f"{type(e).__name__}: {e}")

        # ── !bench / !active must not create a duplicate blank record ───────
        # Regression check: most players are keyed by roster NAME (from
        # !addplayer), not Discord ID. !bench/!active used to only check
        # for an existing record by Discord ID, so benching a name-keyed
        # player silently created a second, blank, empty-stats entry
        # instead of flagging the real one — exactly the "shows up twice,
        # once active once benched" bug this guards against.
        try:
            bench_cmd  = bot.get_command("bench")
            active_cmd = bot.get_command("active")
            before_count = len(sandbox.get("players", {}))
            _, p1_stats_before = find_player_by_name(sandbox, p1)
            eff_before = p1_stats_before.get("career_avg_ppd")

            fake_member = _FakeMember(p1)
            local_ctx = _FakeCtx(ctx)
            await bench_cmd.callback(local_ctx, fake_member)
            local_ctx2 = _FakeCtx(ctx)
            await active_cmd.callback(local_ctx2, fake_member)

            after_count = len(sandbox.get("players", {}))
            _, p1_stats_after = find_player_by_name(sandbox, p1)

            ok = (
                after_count == before_count
                and p1_stats_after is not None
                and p1_stats_after.get("career_avg_ppd") == eff_before
                and p1_stats_after.get("is_benched") is False
            )
            detail = f"player count {before_count}->{after_count} (should be equal), is_benched after=active cycle: {p1_stats_after.get('is_benched') if p1_stats_after else 'MISSING'}"
            record("!bench/!active resolve existing player, no duplicate created", ok, detail)
        except Exception as e:
            record("!bench/!active resolve existing player, no duplicate created", False, f"{type(e).__name__}: {e}")

        # ── Net chart generator (used by !endweek) ──────────────────────────
        try:
            buf = generate_net_chart([("A", 10), ("B", -4), ("C", 0)], "Self-test chart")
            record("generate_net_chart", buf.getbuffer().nbytes > 0)
        except Exception as e:
            record("generate_net_chart", False, f"{type(e).__name__}: {e}")

    finally:
        load_data = _real_load
        save_data = _real_save

    passed = sum(1 for _, ok, _ in results if ok)
    embed = discord.Embed(
        title=f"🧪 Self-Test Results — {passed}/{len(results)} passed",
        description="Every command ran against a sandbox copy — **your live roster was not touched.**",
        color=discord.Color.green() if passed == len(results) else discord.Color.orange(),
    )
    lines = [f"{'✅' if ok else '❌'} `!{name}` — {detail}" for name, ok, detail in results]
    chunk, chunk_len, idx = [], 0, 0
    for line in lines:
        if chunk_len + len(line) + 1 > 1000 and chunk:
            embed.add_field(name="Results" if idx == 0 else "Results (cont.)", value="\n".join(chunk), inline=False)
            chunk, chunk_len, idx = [], 0, idx + 1
        chunk.append(line)
        chunk_len += len(line) + 1
    if chunk:
        embed.add_field(name="Results" if idx == 0 else "Results (cont.)", value="\n".join(chunk), inline=False)

    await ctx.send(embed=embed)


# 11. CONNECT TO DISCORD
import os
token = os.environ.get("DISCORD_BOT_TOKEN")
if not token:
    # Instead of crashing, print a diagnostic message
    print("DEBUG: DISCORD_BOT_TOKEN is missing!")
    # If you still want to crash, make the error message unique
    raise RuntimeError("TOKEN_NOT_FOUND_IN_RAILWAY")


bot.run(token)
