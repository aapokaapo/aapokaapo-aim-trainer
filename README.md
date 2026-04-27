# AapoKaapo Aim Trainer – Leaderboard

A self-hosted FastAPI web application that tracks player performance in the **AapoKaapo Aim Trainer V4** custom Halo Infinite game mode — automatically syncing match results from the Halo Infinite API and displaying a live leaderboard ranked by best time to 100 kills.

## Features

- 🏆 Live leaderboard showing each player's personal best time
- 🔐 One-click Xbox / Microsoft OAuth registration
- 🔄 Automatic background match syncing every 5 minutes (configurable)
- ✅ Match validation: only games on Live Fire - Ranked with ≥100 kills count
- 🗄️ SQLite by default, any SQLAlchemy-compatible DB via `DATABASE_URL`
- 🚀 Built with FastAPI + SQLModel + spnkr + aiohttp

## Prerequisites

- Python 3.11+
- An **Azure AD app registration** (consumer tenant) with the `XboxLive.signin` delegated permission
  - Follow the [spnkr getting-started guide](https://acurtis166.github.io/SPNKr/getting-started/) to set this up
- An **Azure AD app refresh token** (obtained via the spnkr guide)
- The redirect URI registered in Azure: `{PUBLIC_BASE_URL}/auth/microsoft/callback`

## Installation & Setup

```bash
# 1. Clone the repo
git clone https://github.com/aapokaapo/aapokaapo-aim-trainer.git
cd aapokaapo-aim-trainer

# 2. Create and activate a virtual environment
python -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate

# 3. Install dependencies
pip install -r requirements.txt

# 4. Configure environment variables
cp .env.example .env
# Edit .env with your Azure credentials and PUBLIC_BASE_URL
```

Then run:

```bash
python main.py
```

Or with uvicorn directly:

```bash
uvicorn main:app --host 0.0.0.0 --port 8000
```

## Environment Variables

| Variable | Default | Description |
|---|---|---|
| `AZURE_CLIENT_ID` | *(required)* | Azure AD application (client) ID |
| `AZURE_CLIENT_SECRET` | *(required)* | Azure AD client secret |
| `AZURE_REFRESH_TOKEN` | *(required)* | Initial OAuth refresh token (from spnkr setup) |
| `PUBLIC_BASE_URL` | `http://localhost:8000` | Public URL of the app, used to build the OAuth redirect URI |
| `DATABASE_URL` | `sqlite:///matches.db` | SQLAlchemy connection string |
| `UPDATE_INTERVAL_SECONDS` | `300` | Seconds between automatic background match-sync cycles |
| `INTER_PLAYER_DELAY_SECONDS` | `2` | Pause (seconds) between syncing consecutive players to respect API rate limits |

## How to Participate

1. Visit the leaderboard page and click **Register to Leaderboard**
2. Sign in with your Microsoft / Xbox account
3. Bookmark the game mode on Halo Waypoint: **AapoKaapo Aim Trainer V4**
4. In Halo Infinite → Custom Games → Create Custom Game, select the bookmarked mode and the **Live Fire - Ranked** map
5. Race to 100 kills — your time is automatically tracked!

## API Endpoints

| Method | Path | Description |
|---|---|---|
| `GET` | `/` | Leaderboard page |
| `GET` | `/api/leaderboard` | JSON leaderboard (top 100) |
| `GET` | `/api/status` | Last background sync timestamp |
| `GET` | `/auth/microsoft/login` | Start Xbox OAuth flow |
| `GET` | `/auth/microsoft/callback` | OAuth callback (handled automatically) |
| `POST` | `/players/` | Manually add a player by gamertag (admin) |
| `GET` | `/players/` | List all tracked players |
| `GET` | `/players/{gamertag}/matches` | Match history for a specific player |
| `GET` | `/matches/` | List all stored matches |
| `POST` | `/api/debug/force-update` | Force an immediate sync cycle |
| `POST` | `/api/debug/revalidate-matches` | Re-run validation on all stored matches |

## Project Structure

```
aapokaapo-aim-trainer/
├── main.py           # FastAPI app, routes, OAuth handlers
├── updater.py        # Background match sync task
├── haloclient.py     # Halo Infinite API client factory (spnkr wrapper)
├── models.py         # SQLModel ORM models (Player, Match)
├── database.py       # Database engine setup
├── leaderboard.html  # Frontend leaderboard page
├── index.html        # Add-player page (legacy)
├── requirements.txt
└── .env.example
```

## License

All rights reserved.
