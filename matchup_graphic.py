"""
matchup_graphic.py — Deadly Vibes Reborn scoreboard image generator.

Layout:
  • Full-width logo banner (snake image)
  • Full-width league name header  (DVR  vs  Opponent League)
  • Two equal side-by-side player panels (players 1-8 left, 9-16 right)
    – Each panel has its own column-label row (omitted; labels live inside cards)
    – Each player is a standalone MADDBOT-style card block separated by a dark gap
      Card line 1: rank  |  PLAYER NAME  |  vs OPPONENT  (DEF)
      Card line 2: SCORE   PPD   EFF   EXP   │  OPP
      Card line 3: value   val   val   val   │  val
    – EXP ≥ 18 → green, EXP < 18 → red
    – Our score: green if winning, red if losing vs logged opponent score
  • Full-width footer
"""
import io
import os
from PIL import Image, ImageDraw, ImageFont

# ── SYSTEM FONT PATHS ──────────────────────────────────────────────────────────
_DEJAVU_BOLD    = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
_DEJAVU_REGULAR = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"

# ── PALETTE ────────────────────────────────────────────────────────────────────
BG_DARK       = (8,    3,  15)
ROW_A         = (18,   8,  36)   # even card body
ROW_B         = (26,  12,  50)   # odd card body
ACCENT_A      = (105,  42, 188)  # left stripe — even
ACCENT_B      = ( 62,  20, 118)  # left stripe — odd
BORDER_TOP    = (168,  85, 247)  # NEON_VIOLET — bright top edge of each card
BORDER_BOT    = ( 30,  12,  58)  # subtle bottom edge
PANEL_DIV     = ( 55,  25, 100)  # inner divider (our stats ↔ opp stats)
NEON_VIOLET   = (168,  85, 247)
NEON_DIM      = ( 80,  35, 130)
WIN_GREEN     = (  0, 210,  80)
LOSS_RED      = (255,  65,  65)
TIE_GOLD      = (230, 190,   0)
EFF_GREEN     = (  0, 230, 118)
EFF_RED       = (255,  82,  82)
NET_POS       = (  0, 230, 118)  # weekly net +/- , positive
NET_NEG       = (255,  82,  82)  # weekly net +/- , negative
EXP_GREEN     = ( 80, 220, 130)  # EXP ≥ 18
EXP_RED       = (255,  75,  75)  # EXP < 18
TEXT_WHITE    = (243, 244, 246)
TEXT_NAME     = (255, 255, 255)
TEXT_OPP      = (175, 148, 238)
TEXT_MUTED    = (110, 125, 155)
TEXT_LABEL    = ( 85,  95, 120)  # small stat labels inside cards
TEAM_HDR_BG   = (16,   3,  40)
CENTER_GAP    = (12,   4,  24)
FOOTER_COL    = ( 90,  55, 138)

EFF_THRESHOLD = 6.0
OUR_LEAGUE    = "Deadly Vibes Reborn"


# ── FONT HELPERS ───────────────────────────────────────────────────────────────
def _font(size: int, bold: bool = False) -> ImageFont.ImageFont:
    primary  = _DEJAVU_BOLD    if bold else _DEJAVU_REGULAR
    fallback = _DEJAVU_REGULAR if bold else _DEJAVU_BOLD
    for path in (primary, fallback, "DejaVuSans-Bold.ttf", "DejaVuSans.ttf"):
        try:
            return ImageFont.truetype(path, size)
        except (IOError, OSError):
            continue
    return ImageFont.load_default()


def _trunc(s: str, max_chars: int) -> str:
    s = str(s)
    return s if len(s) <= max_chars else s[: max_chars - 1] + "…"


def _text_top(y_base: int, font) -> int:
    """Return drawing y so text baseline sits at y_base."""
    try:
        bb = font.getbbox("Ag")
        return y_base - bb[1]
    except Exception:
        return y_base


def _fill(draw, x0, y0, x1, y1, color):
    if x1 > x0 and y1 > y0:
        draw.rectangle([x0, y0, x1 - 1, y1 - 1], fill=color)


def _text_w(text: str, font) -> int:
    try:
        bb = font.getbbox(str(text))
        return bb[2] - bb[0]
    except Exception:
        return len(str(text)) * getattr(font, "size", 12)


# ── PUBLIC API ─────────────────────────────────────────────────────────────────
def generate_matchup_sheet(
    matchups_dict: dict,
    logo_path: str | None = None,
    opp_league: str | None = None,
) -> io.BytesIO:
    """
    Build the matchup scoreboard PNG.

    matchups_dict: ordered dict  player_name → {
        "opponent":  str
        "def":       int   opponent DEF OVR
        "ppd":       float raw PPD this week
        "eff":       float difficulty-normalised efficiency
        "expected":  int   expected score  (0-24)
        "our_score": int|None
        "opp_score": int|None
        "net":       int   weekly +/- (offense scored - defense allowed)
    }
    """
    entries   = list(matchups_dict.items())[:16]
    opp_label = opp_league or "Opponent League"
    left_entries  = entries[:8]
    right_entries = entries[8:]

    # ── GLOBAL DIMENSIONS ──────────────────────────────────────────────────────
    W          = 1600
    LOGO_H     = 250
    TEAM_H     = 80
    SEP        = 4
    FOOTER_H   = 46
    CENTER_W   = 44         # dark gap between the two panels

    PANEL_W    = (W - CENTER_W) // 2   # 778 px each
    LEFT_X     = 0
    RIGHT_X    = PANEL_W + CENTER_W    # 822

    # ── CARD DIMENSIONS ────────────────────────────────────────────────────────
    ACCENT_W   = 9          # left accent stripe width
    CARD_H     = 96         # total card height (incl 1px top + 1px bottom border)
    CARD_GAP   = 7          # dark gap between cards
    BORDER_T   = 2          # top border thickness
    BORDER_B   = 1          # bottom border thickness

    # Card interior vertical rhythm (y = offset from card top)
    _Y_NAME    = 10         # name-row baseline
    _Y_LBL     = 52         # stat-label baseline
    _Y_VAL     = 70         # stat-value baseline

    # ── CARD COLUMN X OFFSETS (relative to panel left edge) ───────────────────
    # Line 1: rank  |  name  |  (right) opponent info
    _RK        = ACCENT_W + 8          # rank "#N"
    _NM        = ACCENT_W + 78         # player name (wider gap so #10+ don't overlap)

    # Lines 2+3: stat columns (labels and values share same x per column)
    _PD        = _NM                   # PPD
    _EF        = _NM + 74              # EFF
    _EP        = _NM + 148             # EXP
    _NT        = _NM + 222             # NET (weekly +/-)

    # Inner panel divider (our stats ↔ opp info)
    _DIV_X     = _NM + 296             # right of NET column

    # Opponent columns  (right of divider)
    _OS        = _DIV_X + 14           # OPP score
    _OD        = _DIV_X + 78           # DEF value "(245)"

    # Large player score — right-aligned in dead space right of DEF column
    _SC_BIG_X  = PANEL_W - 12         # right-align anchor (safe right margin)

    # ── VERTICAL LAYOUT ────────────────────────────────────────────────────────
    y_logo     = 0
    y_sep0     = LOGO_H
    y_team     = LOGO_H + SEP
    y_rows     = y_team + TEAM_H + SEP + CARD_GAP
    rows_count = max(len(left_entries), len(right_entries))
    y_footer   = y_rows + rows_count * (CARD_H + CARD_GAP)
    H          = y_footer + FOOTER_H

    # ── FONTS ──────────────────────────────────────────────────────────────────
    f_league  = _font(28, bold=True)
    f_big_sc  = _font(52, bold=True)   # large score in right dead space
    f_rank    = _font(26, bold=True)
    f_name    = _font(24, bold=True)   # player name
    f_opp_hdr = _font(17)              # "vs Highlightreel  (245)"
    f_lbl     = _font(14, bold=True)   # stat labels row
    f_score   = _font(26, bold=True)   # SCORE value (prominent)
    f_val     = _font(22)              # PPD / EFF / EXP values
    f_opp_sc  = _font(22, bold=True)   # opponent score value
    f_footer  = _font(19)

    # ── CANVAS ─────────────────────────────────────────────────────────────────
    img  = Image.new("RGB", (W, H), BG_DARK)
    draw = ImageDraw.Draw(img)

    # ── LOGO BANNER ────────────────────────────────────────────────────────────
    _dir = os.path.dirname(os.path.abspath(__file__))
    for lp in [logo_path,
               os.path.join(_dir, "dv_logo.png"),
               os.path.join(_dir, "1000018208.png")]:
        if not lp or not os.path.isfile(lp):
            continue
        try:
            logo  = Image.open(lp).convert("RGBA")
            scale = max(W / logo.width, LOGO_H / logo.height)
            nw    = max(W,      int(logo.width  * scale))
            nh    = max(LOGO_H, int(logo.height * scale))
            logo  = logo.resize((nw, nh), Image.LANCZOS)
            cx    = (nw - W) // 2
            logo  = logo.crop((cx, 0, cx + W, LOGO_H))
            base  = Image.new("RGB", (W, LOGO_H), BG_DARK)
            base.paste(logo, (0, 0), logo)
            img.paste(base, (0, 0))
            break
        except Exception:
            continue
    else:
        draw.text((W // 2, LOGO_H // 2), OUR_LEAGUE,
                  fill=TEXT_WHITE, font=_font(54, bold=True), anchor="mm")

    # Logo separator
    _fill(draw, 0, y_sep0, W, y_sep0 + SEP, NEON_VIOLET)

    # ── TOTALS from matchups_dict ───────────────────────────────────────────────
    our_total = sum(v["our_score"] for v in matchups_dict.values() if v.get("our_score") is not None)
    opp_total = sum(v["opp_score"] for v in matchups_dict.values() if v.get("opp_score") is not None)
    any_opp   = any(v.get("opp_score") is not None for v in matchups_dict.values())

    # ── LEAGUE NAME HEADER ─────────────────────────────────────────────────────
    _fill(draw, 0, y_team, W, y_team + TEAM_H, TEAM_HDR_BG)

    f_tot = _font(30, bold=True)   # large total score numbers

    # Row 1 — league names + VS
    try:
        vs_bb = f_opp_hdr.getbbox("VS")
        vs_w  = vs_bb[2] - vs_bb[0]
    except Exception:
        vs_w  = 26

    row1_y = y_team + 8
    vy     = _text_top(row1_y, f_league)
    draw.text((20,                       vy), _trunc(OUR_LEAGUE, 26), fill=NEON_VIOLET, font=f_league)
    draw.text((W // 2 - vs_w // 2, vy + 4), "VS",                    fill=NEON_DIM,    font=f_opp_hdr)
    draw.text((W // 2 + vs_w // 2 + 14, vy), _trunc(opp_label, 26),  fill=TEXT_OPP,    font=f_league)

    # Row 2 — running totals
    row2_y    = row1_y + 34
    our_str   = str(our_total)
    opp_str   = str(opp_total) if any_opp else "—"
    dash_str  = "  —  "

    try:
        our_w  = f_tot.getbbox(our_str)[2]  - f_tot.getbbox(our_str)[0]
        dash_w = f_tot.getbbox(dash_str)[2] - f_tot.getbbox(dash_str)[0]
        opp_w  = f_tot.getbbox(opp_str)[2]  - f_tot.getbbox(opp_str)[0]
    except Exception:
        our_w = opp_w = 60; dash_w = 40

    block_w   = our_w + dash_w + opp_w
    bx        = (W - block_w) // 2
    ty2       = _text_top(row2_y, f_tot)
    draw.text((bx,               ty2), our_str,  fill=EXP_GREEN,  font=f_tot)
    draw.text((bx + our_w,       ty2), dash_str, fill=NEON_DIM,   font=f_tot)
    draw.text((bx + our_w + dash_w, ty2), opp_str, fill=TEXT_OPP, font=f_tot)

    # Separator below team header
    _fill(draw, 0, y_team + TEAM_H, W, y_team + TEAM_H + SEP, NEON_VIOLET)

    # Center gap fill for data rows (dark bar between the two panels)
    _fill(draw, PANEL_W, y_rows, RIGHT_X, y_footer, CENTER_GAP)

    # ── CARD RENDERER ──────────────────────────────────────────────────────────
    def _draw_card(ox: int, col_idx: int, global_rank: int, player_name: str, info: dict):
        """
        Draw one MADDBOT-style card inside the panel at x-offset ox.
        col_idx  : 0-based index within column (drives alternating colours).
        global_rank: 1-based position across all players.
        """
        ct = y_rows + col_idx * (CARD_H + CARD_GAP)   # card top
        cb = ct + CARD_H                                # card bottom

        row_bg  = ROW_A if col_idx % 2 == 0 else ROW_B
        acc_col = ACCENT_A if col_idx % 2 == 0 else ACCENT_B

        # ── Card body ──────────────────────────────────────────────────────
        # Top border (bright) — full width
        _fill(draw, ox,          ct,           ox + PANEL_W, ct + BORDER_T,   BORDER_TOP)
        # Bottom border (subtle)
        _fill(draw, ox,          cb - BORDER_B, ox + PANEL_W, cb,             BORDER_BOT)
        # Accent stripe (covers left border region)
        _fill(draw, ox,          ct + BORDER_T, ox + ACCENT_W, cb - BORDER_B, acc_col)
        # Card body
        _fill(draw, ox + ACCENT_W, ct + BORDER_T, ox + PANEL_W, cb - BORDER_B, row_bg)
        # Inner stat divider
        _fill(draw, ox + _DIV_X - 1, ct + BORDER_T, ox + _DIV_X + 1, cb - BORDER_B, PANEL_DIV)

        # ── Data extraction ────────────────────────────────────────────────
        ppd      = info.get("ppd", 0.0)
        eff      = info.get("eff", ppd)
        expected = info.get("expected", 0)
        net      = info.get("net", 0)
        our_sc   = info.get("our_score")
        opp_sc   = info.get("opp_score")
        opp_name = str(info.get("opponent", "—"))
        opp_ovr  = info.get("def", 0)

        # Our score colour: win/loss vs opponent if known, else white
        if our_sc is not None and opp_sc is not None:
            sc_col = WIN_GREEN if our_sc > opp_sc else (LOSS_RED if our_sc < opp_sc else TIE_GOLD)
        else:
            sc_col = TEXT_WHITE

        eff_col = EFF_GREEN if eff >= EFF_THRESHOLD else EFF_RED
        exp_col = EXP_GREEN if expected >= 18       else EXP_RED
        net_col = NET_POS   if net >= 0             else NET_NEG

        our_str = str(our_sc) if our_sc is not None else ""
        opp_str = str(opp_sc) if opp_sc is not None else "0"
        exp_str = f"~{expected}" if expected > 0 else "—"

        if opp_sc is not None and our_sc is not None:
            os_col = LOSS_RED if opp_sc > our_sc else (WIN_GREEN if opp_sc < our_sc else TIE_GOLD)
        else:
            os_col = TEXT_MUTED

        # ── LINE 1: rank  |  name ──────────────────────────────────────────
        y1 = ct + _Y_NAME
        draw.text((ox + _RK, _text_top(y1, f_rank)),
                  f"#{global_rank}", fill=NEON_VIOLET, font=f_rank)
        draw.text((ox + _NM, _text_top(y1, f_name)),
                  _trunc(player_name, 14), fill=TEXT_NAME, font=f_name)

        # ── LINE 2: stat labels + opponent name as right-side header ───────
        y2 = ct + _Y_LBL
        off_ovr_val = info.get("off_ovr")
        for lx, label in [(_RK, "OFF"), (_PD, "PPD"), (_EF, "EFF"), (_EP, "EXP"), (_NT, "NET")]:
            draw.text((ox + lx, _text_top(y2, f_lbl)), label, fill=TEXT_LABEL, font=f_lbl)
        # Opponent name sits in the label row right after the divider
        draw.text((ox + _OS, _text_top(y2, f_opp_hdr)),
                  f"vs {_trunc(opp_name, 13)}", fill=TEXT_OPP, font=f_opp_hdr)

        # ── LINE 3: stat values ─────────────────────────────────────────────
        y3 = ct + _Y_VAL
        off_str = f"({off_ovr_val})" if off_ovr_val else "(—)"
        draw.text((ox + _RK, _text_top(y3, f_val)),    off_str,            fill=NEON_VIOLET, font=f_val)
        draw.text((ox + _PD, _text_top(y3, f_val)),    f"({ppd:.2f})",     fill=TEXT_MUTED,  font=f_val)
        draw.text((ox + _EF, _text_top(y3, f_val)),    f"({eff:.2f})",     fill=eff_col,     font=f_val)
        draw.text((ox + _EP, _text_top(y3, f_val)),    f"({exp_str})",     fill=exp_col,     font=f_val)
        net_str = f"+{net}" if net >= 0 else str(net)
        draw.text((ox + _NT, _text_top(y3, f_val)),    f"({net_str})",     fill=net_col,     font=f_val)
        draw.text((ox + _OS, _text_top(y3, f_opp_sc)), f"({opp_str})",     fill=os_col,      font=f_opp_sc)
        if opp_ovr:
            draw.text((ox + _OD, _text_top(y3, f_val)), f"({opp_ovr})",    fill=TEXT_MUTED,  font=f_val)

        # ── BIG SCORE — right dead space, vertically centred ───────────────
        if our_sc is None:
            sc_display = ""
        else:
            sc_display = str(our_sc)
        sc_val  = our_sc if our_sc is not None else -1
        big_col = EXP_GREEN if sc_val >= 18 else EXP_RED
        # vertically centre the large number in the card body
        try:
            bb   = f_big_sc.getbbox(sc_display)
            th   = bb[3] - bb[1]
            ty   = ct + (CARD_H - th) // 2 - bb[1]
        except Exception:
            ty   = ct + CARD_H // 4
        # right-align by measuring width
        try:
            sw = f_big_sc.getbbox(sc_display)[2] - f_big_sc.getbbox(sc_display)[0]
        except Exception:
            sw = len(sc_display) * 32
        draw.text((ox + _SC_BIG_X - sw, ty), sc_display, fill=big_col, font=f_big_sc)

    # ── RENDER BOTH COLUMNS ────────────────────────────────────────────────────
    for ci, (pname, info) in enumerate(left_entries):
        _draw_card(LEFT_X, ci, ci + 1, pname, info)

    for ci, (pname, info) in enumerate(right_entries):
        _draw_card(RIGHT_X, ci, ci + 9, pname, info)

    # ── FOOTER ─────────────────────────────────────────────────────────────────
    _fill(draw, 0, y_footer, W, H, BG_DARK)
    _fill(draw, 0, y_footer, W, y_footer + SEP, NEON_VIOLET)
    draw.text(
        (W // 2, y_footer + FOOTER_H // 2),
        "Deadly Vibes Reborn  •  !postmatchup to repost  •  !board for live scores"
        "  •  !gave <player> <pts> to log opponent score",
        fill=FOOTER_COL, font=f_footer, anchor="mm",
    )

    buf = io.BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)
    return buf
