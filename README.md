# meeting-room-bot
Telegram bot for meeting room booking

## Features

- Meeting room booking and cancellation
- Group schedule announcements
- Admin document upload (/uploaddoc)
- Document and image to PDF conversion (/topdf)

### PDF Conversion (/topdf)

- Send `/topdf` to the bot, then upload a document or image.
- Supported out of the box: image files (`.jpg`, `.jpeg`, `.png`, `.bmp`, `.gif`, `.webp`, `.tif`, `.tiff`) and text-like files (`.txt`, `.md`, `.csv`, `.log`).
- Office files (`.docx`, `.xlsx`, `.pptx`, etc.) are supported when LibreOffice is installed on the server.

### LibreOffice Deployment

- This repository now includes a `Dockerfile` that installs LibreOffice for server-side Office to PDF conversion.
- Railway and Render should be deployed using the Docker configuration in this repo so `soffice` is available at runtime.

## 🚀 Deployment on Railway

### Quick Setup

1. **Create a new project on Railway**
   - Go to [Railway](https://railway.app/)
   - Click "New Project" → "Deploy from GitHub repo"
   - Select your repository
   - Railway will build using the included `Dockerfile`

2. **Configure Environment Variables**
   
   Add these variables in Railway dashboard (Settings → Variables):
   
   ```
   BOT_TOKEN=your_telegram_bot_token
   DATABASE_URL=postgresql+psycopg2://user:password@host/database
   MINI_APP_URL=https://your-web-service.up.railway.app
   GROUP_CHAT_ID=your_telegram_group_chat_id
   ADMIN_ID=your_telegram_admin_user_id
   MEETING_TIMEZONE=Asia/Phnom_Penh
   USE_WEBHOOK=false
   TELEGRAM_INIT_DATA_MAX_AGE=3600
   ```

3. **Enable Public Domain**
   - In Railway dashboard, go to Settings → Networking
   - Click "Generate Domain" to get a public URL
   - Copy the domain URL

4. **Create the bot worker**
   - Add a second service from the same repository using `Dockerfile.bot`.
   - Set `MINI_APP_URL` to the web service's public domain.

### Environment Variables Explained

- `BOT_TOKEN`: Get from [@BotFather](https://t.me/botfather) on Telegram
- `DATABASE_URL`: SQLAlchemy-compatible PostgreSQL connection URL
- `MINI_APP_URL`: Public URL of the FastAPI Mini App
- `GROUP_CHAT_ID`: The chat ID of your group (use `/chatid` command)
- `ADMIN_ID`: Your Telegram user ID (use `/myid` command)
- `MEETING_TIMEZONE`: IANA timezone used for booking dates and meeting status (defaults to `Asia/Phnom_Penh`)
- `USE_WEBHOOK`: Keep `false` for the separately deployed polling worker
- `TELEGRAM_INIT_DATA_MAX_AGE`: Maximum Mini App credential age in seconds

### 📋 Files for Railway

- `Procfile`: Declares separate web and worker processes
- `railway.json`: Railway configuration
- `railway.toml`: Railway build/deploy configuration
- `Dockerfile`: Runs the Mini App web service
- `Dockerfile.bot`: Runs the Telegram bot worker with LibreOffice
- `requirements.txt`: Python dependencies

### 🔧 Troubleshooting

**Word to PDF not working?**
- Check deployment logs for Docker build errors
- Verify the service is using the `Dockerfile` build, not plain Python/Nixpacks
- Confirm `soffice` is available inside the running container

**Bot not responding?**
- Check logs in Railway dashboard
- Verify all environment variables are set
- Make sure WEBHOOK_URL matches your Railway domain
- Ensure public domain is generated and active

**Webhook errors?**
- Regenerate domain in Railway if URL changed
- Update WEBHOOK_URL environment variable
- Redeploy the service

### 💡 Local Development

For local testing, create a `.env` file:

```env
BOT_TOKEN=your_token
SPREADSHEET_URL=your_sheet_url
GROUP_CHAT_ID=your_chat_id
ADMIN_ID=your_admin_id
GOOGLE_CREDENTIALS={"type":"service_account",...}
USE_WEBHOOK=false
```

Then run: `python meeting_bot.py`

## Database setup

The application uses PostgreSQL. Apply the schema migration before starting a new
deployment or deploying the overlap-protection update:

```bash
psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -f migrations/001_initial_schema.sql
```

The migration creates the required tables and indexes and adds a PostgreSQL
exclusion constraint that prevents simultaneous requests from double-booking the
room. If existing active bookings overlap, PostgreSQL will reject the migration;
resolve those records and run it again.

## Runtime processes

Production requires two independently supervised processes:

- Web: `uvicorn mini_app:app --host 0.0.0.0 --port $PORT`
- Bot worker: `python meeting_bot.py`

The default `Dockerfile` runs the web process. `Dockerfile.bot` runs the Telegram
bot and its hourly cleanup scheduler. On Railway, create two services from the
same repository and select the appropriate Dockerfile for each service. On
Render, `render.yaml` declares both processes.

The bot worker defaults to polling (`USE_WEBHOOK=false`). Do not run multiple
polling workers with the same bot token.

## Security

Telegram Mini App credentials expire after one hour by default. Override this
with `TELEGRAM_INIT_DATA_MAX_AGE` (seconds). Setting it to `0` disables expiry and
is not recommended.

## Tests

Create a fresh virtual environment, install development dependencies, and run:

```bash
python -m pip install -r requirements-dev.txt
python -m pytest -q
```

Set `TEST_DATABASE_URL` to a disposable PostgreSQL database to enable the real
concurrency integration test. Without it, that test is skipped.

The `/health` endpoint reports database readiness and observed query latency.
Every HTTP response also includes `Server-Timing` and `X-Process-Time-Ms`
headers, which can be collected by a proxy or monitoring service.
