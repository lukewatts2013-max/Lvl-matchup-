# 🏈 MADDEN MOBILE BOT COMMISSIONER PLAYBOOK
Daily Guide for Matchups, Score Tracking, and Bench Management

---

## 📅 THE DAILY ROUTINE

Follow these steps every morning when a new League vs League (LvL) match opens:

1. **TAKE SCREENSHOTS OF THE OPPONENT**  
   Open Madden Mobile, go to the active matchup map, and take screenshots showing the opponent usernames and their defensive overall numbers.

2. **GET THE BOT COMMAND LINE**  
   - Option A (You are free): Drop the screenshots right into this chat and say "Format this for the bot."  
   - Option B (An assistant helps): If you are busy at work, any co-commissioner can take the screenshots, drop them in this chat, and ask for the bot line!

3. **POST THE MATCHUPS IN DISCORD**  
   Copy the text line given back to you, paste it into your locked Discord strategy channel, and hit send.

   Example:
   ```
   !matchup MAX=266, Hatter_Neuro=264, Roderigo_Neuro=250, CharlieMike=252
   ```

   The bot will instantly look up your team's score history, find everyone's Points Per Drive (PPD) average, and pair your active players against the toughest defenses.

---

## 👥 BENCH MANAGEMENT (ROSTER MAX: 18 | ACTIVE: 16)

Because only 16 players can play each day, the bot automatically hides anyone who has 0 active weekly drives.

- If a player takes a day off: Just let them sit. Since they aren't logging scores, the bot automatically leaves them off the matchup list.
- If you are benching yesterday's lowest scorer: Before you run the daily `!matchup` command, ensure your active 16 have logged scores. The bot will take the top 16 active PPD averages and leave the benched players off the sheet.

---

## 🏈 EVERYDAY PLAYER RECORDING

Players should type their scores in the bot channel as soon as they finish their daily drives.

### Normal Score Logs
```
!score24
!gave16
```
The bot adds the points to their leaderboards, tracks their drives, and gives a green checkmark reaction.

### Fumble / Bad Luck Protection
```
!score14 !fumble
```
The bot still adds the points to the overall score, but ignores those 3 drives when calculating PPD.

---

## 🛠️ ADMIN OVERRIDES & DATA FIXING

### Fixing a Defense Typo
Just re-run `!matchup` with the corrected numbers — the new list prints below the old one.

### Logging a Score for a Missing Player
```
!forcescore @Kirito 24
!forcescore @Kirito 16 fumble
```

### Starting a New Week
```
!clearstats weekly
```

---

## 🏆 VIEWING THE STANDINGS

| Command | Description |
|---|---|
| `!stats` or `!stats weekly` | Current 7-day leaderboard and PPD averages |
| `!stats monthly` | Total points for the current month |
| `!stats yearly` | Long-term season totals |
