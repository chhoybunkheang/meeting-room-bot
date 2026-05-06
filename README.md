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
   SPREADSHEET_URL=your_google_spreadsheet_url
   GROUP_CHAT_ID=your_telegram_group_chat_id
   ADMIN_ID=your_telegram_admin_user_id
   GOOGLE_CREDENTIALS={"type":"service_account","project_id":"..."}
   
   USE_WEBHOOK=true
   WEBAPP_HOST=0.0.0.0
   WEBAPP_PORT=$PORT
   WEBHOOK_URL=https://${{RAILWAY_PUBLIC_DOMAIN}}
   ```

3. **Enable Public Domain**
   - In Railway dashboard, go to Settings → Networking
   - Click "Generate Domain" to get a public URL
   - Copy the domain URL

4. **Update WEBHOOK_URL**
   - Replace `WEBHOOK_URL` variable with your actual Railway domain:
   - Example: `https://your-app-name.up.railway.app`

### Environment Variables Explained

- `BOT_TOKEN`: Get from [@BotFather](https://t.me/botfather) on Telegram
- `SPREADSHEET_URL`: Your Google Sheets URL for bookings
- `GROUP_CHAT_ID`: The chat ID of your group (use `/chatid` command)
- `ADMIN_ID`: Your Telegram user ID (use `/myid` command)
- `GOOGLE_CREDENTIALS`: JSON credentials from Google Cloud Console
- `USE_WEBHOOK`: Set to `true` for Railway (required for web service)
- `WEBAPP_HOST`: Keep as `0.0.0.0`
- `WEBAPP_PORT`: Use `$PORT` (Railway auto-assigns this)
- `WEBHOOK_URL`: Your Railway public domain URL

### 📋 Files for Railway

- `Procfile`: Tells Railway how to start the bot
- `railway.json`: Railway configuration
- `railway.toml`: Railway build/deploy configuration
- `Dockerfile`: Installs Python dependencies and LibreOffice
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
