# Puppet Theater Show Tracker

This Python script monitors the Puppet Theater Minsk website for new shows and sends notifications via Telegram when new shows are added.

## Features

- Scrapes show information from puppet-minsk.by/afisha
- Tracks new shows and compares with previously saved shows
- Sends notifications via Telegram when new shows are detected
- Persists show data in a local JSON file

## Requirements

- Python 3.7+
- Playwright
- requests

## Setup

1. Install dependencies:
```bash
pip install playwright requests
playwright install chromium
```

2. Configure Telegram:
- Create a Telegram bot using BotFather
- Update `BOT_TOKEN` in the script with your bot token
- Update `CHAT_ID` with your Telegram chat ID

3. Run the script:
```bash
python afisha.py
```

## Note

Make sure to keep your Telegram bot token secure and never commit it to version control.