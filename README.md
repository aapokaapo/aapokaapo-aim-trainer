# Aapokaapo Aim Trainer – Match Importer

A FastAPI service that maintains a local SQLite database of Halo Infinite aim-trainer matches for one or more players.  
Matches are fetched via [SPNKr](https://github.com/acurtis166/SPNKr) and only persisted when they pass the configured selection criteria (e.g. a specific game-variant category).

---

## Project layout

```
.
├── main.py          # FastAPI app, SQLModel ORM models, API endpoints
├── updater.py       # Periodic background task (match fetching & persistence)
├── haloclient.py    # Authenticated HaloInfiniteClient factory (Azure OAuth)
├── requirements.txt # Python dependencies
├── .env.example     # Template for required environment variables
└── README.md        # This file
```

---

## Prerequisites

- Python 3.11 or newer
- An Azure AD application with Halo Infinite API access  
  Follow the [SPNKr getting-started guide](https://acurtis166.github.io/SPNKr/getting-started/) to create the app and obtain an initial refresh token.

---

## Setup

### 1. Clone and create a virtual environment

```bash
git clone https://github.com/aapokaapo/aapokaapo-aim-trainer.git
cd aapokaapo-aim-trainer
python -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate
```

### 2. Install dependencies

```bash
pip install -r requirements.txt
```

### 3. Configure environment variables

Copy the example file and fill in your values:

```bash
cp .env.example .env
```

Edit `.env`:

| Variable | Description |
|---|---|
| `AZURE_CLIENT_ID` | Azure AD application client ID |
| `AZURE_CLIENT_SECRET` | Azure AD application client secret |
| `AZURE_REFRESH_TOKEN` | Initial OAuth refresh token obtained during SPNKr setup |
| `REDIRECT_URI` | OAuth redirect URI (default: `https://localhost`) |
| `DATABASE_URL` | SQLAlchemy database URL (default: `sqlite:///matches.db`) |
| `UPDATE_INTERVAL_SECONDS` | Seconds between background update cycles (default: `300`) |
| `DEFAULT_GAMEMODE` | `game_variant_category` integer used to filter matches (default: `9`) |

> **Tip:** Run a manual `/import-matches/` request first and inspect `match_info.game_variant_category` in the response to determine the correct `DEFAULT_GAMEMODE` value for your aim-trainer playlist.

---

## Database migrations

SQLModel uses SQLAlchemy's `create_all` to manage the schema.  
Tables are created automatically on server startup — **no manual migration step is required** for a fresh install.

If you need to reset the database, delete `matches.db` (or whatever path `DATABASE_URL` points to) and restart the server; the tables will be recreated.

> For production deployments that require incremental migrations, integrate [Alembic](https://alembic.sqlalchemy.org/) and generate a migration with:
> ```bash
> alembic revision --autogenerate -m "initial schema"
> alembic upgrade head
> ```

---

## Starting the server

The background task starts automatically as part of the FastAPI lifespan when the server starts.

```bash
uvicorn main:app --host 0.0.0.0 --port 8000 --reload
```

Or run directly:

```bash
python main.py
```

On startup the server will:
1. Create the `player` and `match` database tables if they do not exist.
2. Launch the periodic background task that fetches new matches for all players in the database every `UPDATE_INTERVAL_SECONDS` seconds.

The interactive API docs are available at <http://localhost:8000/docs>.

---

## API endpoints

| Method | Path | Description |
|---|---|---|
| `POST` | `/import-matches/` | Resolve a gamertag, create/update the player row, and import new matches that match the given `gamemode`. |
| `GET` | `/players/` | List all players and their stored matches. |
| `GET` | `/matches/` | List all stored matches. |

### Example: import matches for a player

```bash
curl -X POST http://localhost:8000/import-matches/ \
  -H "Content-Type: application/json" \
  -d '{"gamertag": "YourGamertag", "gamemode": 9}'
```

---

## Background task behaviour

`updater.py` runs an asyncio loop that wakes up every `UPDATE_INTERVAL_SECONDS` seconds and:

1. Reads all `Player` rows from the database.
2. For each player, fetches match history from the Halo Infinite API in batches of 25, stopping at `Player.latest_match_id` (the incremental cursor).
3. Applies `_passes_criteria()` – currently filters by `game_variant_category == DEFAULT_GAMEMODE`.
4. Fetches full match statistics for each match that passes the filter.
5. Persists new matches to the `match` table and updates `Player.latest_match_id` to the newest seen match UUID.

To add custom criteria (minimum duration, specific map, accuracy threshold, etc.) edit the `_passes_criteria` function in `updater.py`.

---

## Data models

```python
class Player(SQLModel, table=True):
    id: Optional[int]          # auto-increment primary key
    xuid: str                  # unique Halo player identifier
    gamertag: str
    latest_match_id: Optional[str]  # cursor for incremental fetch

class Match(SQLModel, table=True):
    id: Optional[int]          # auto-increment primary key
    match_id: str              # unique Halo match UUID
    player_id: int             # FK → player.id
    duration: str
    played_at: datetime
    raw_match_stats: str       # full match JSON stored as text
```
