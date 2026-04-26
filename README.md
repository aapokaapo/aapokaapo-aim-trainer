# Aapokaapo Aim Trainer

A Halo Infinite aim-trainer leaderboard and match tracker built with FastAPI, SQLModel, and Jinja2.

## Features

- **Leaderboard** — Ranked list of all registered players by number of tracked matches.
- **Player pages** — Per-player match history with parsed stats (kills, deaths, assists, accuracy).
- **Match pages** — Full stats for an individual match, linked back to the player.
- **Search** — Search the leaderboard by gamertag.
- **Automatic updates** — A background task fetches new matches from the Halo Infinite API every 12 minutes.

## Project layout

| File / folder | Description |
|---|---|
| `models.py` | SQLModel ORM models (`Player`, `Match`) and the SQLAlchemy engine |
| `database.py` | CRUD helpers for `Player` and `Match` |
| `main.py` | FastAPI application — HTML views **and** JSON API endpoints |
| `updater.py` | Periodic background task (runs every `UPDATE_INTERVAL_SECONDS` seconds) |
| `haloclient.py` | Azure OAuth + Halo Infinite client factory |
| `templates/` | Jinja2 HTML templates (`base`, `index`, `player`, `match`, `search`) |
| `static/` | CSS and other static assets |

## Setup

### 1. Install dependencies

```bash
pip install -r requirements.txt
```

### 2. Configure environment variables

Copy `.env.example` to `.env` and fill in your values:

```bash
cp .env.example .env
```

| Variable | Required | Description |
|---|---|---|
| `AZURE_CLIENT_ID` | ✅ | Azure AD application client ID |
| `AZURE_CLIENT_SECRET` | ✅ | Azure AD application client secret |
| `AZURE_REFRESH_TOKEN` | ✅ | Initial OAuth refresh token |
| `REDIRECT_URI` | ✅ | OAuth redirect URI (e.g. `https://localhost`) |
| `DATABASE_URL` | ❌ | SQLAlchemy DB URL (default: `sqlite:///matches.db`) |
| `UPDATE_INTERVAL_SECONDS` | ❌ | Seconds between updater cycles (default: `720` = 12 min) |
| `DEFAULT_GAMEMODE` | ❌ | Fallback `game_variant_category` integer (default: `9`) |

See [SPNKr Getting Started](https://acurtis166.github.io/SPNKr/getting-started/) for instructions on
creating an Azure AD app and obtaining the initial refresh token.

### 3. Run the server

```bash
python main.py
# or
uvicorn main:app --reload
```

Open <http://localhost:8000> in your browser.

### 4. Register players

Use the JSON API endpoint to add a player and import their match history:

```bash
curl -X POST http://localhost:8000/import-matches/ \
  -H "Content-Type: application/json" \
  -d '{"gamertag": "YourGamertag", "gamemode": 9}'
```

> **Note:** This endpoint requires `SPARTAN_TOKEN` and `CLEARANCE_TOKEN` environment variables
> (static tokens, see `.env.example`).  The background updater uses the Azure OAuth flow instead.

After the initial import the background updater will keep the player's match history up to date
automatically.

## Pages

| Route | Description |
|---|---|
| `GET /` | Leaderboard + how-to-participate section |
| `GET /player/{xuid}` | Player stats — best match highlight and full match list |
| `GET /match/{match_id}` | Match stats + link back to the player |
| `GET /search?q=...` | Gamertag search (redirects directly when exactly one result) |

## JSON API (legacy)

| Method | Route | Description |
|---|---|---|
| `POST` | `/import-matches/` | Manually import matches for a player |
| `GET` | `/players/` | List all players (JSON) |
| `GET` | `/matches/` | List all matches (JSON) |
