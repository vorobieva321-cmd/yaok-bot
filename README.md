# yaok-bot

Ukrainian-language psychological support Telegram bot.

## Features
- 6 topics: anxiety, burnout, relationships, sleep, sadness, anger
- Powered by Claude AI (claude-sonnet-4-6)
- Free tier: 6 messages per user
- Paid tier: unlimited (500 Telegram Stars/month)
- Auto-reminders after 3 days of inactivity
- Admin commands: `/grant`, `/revoke`, `/stats`, `/broadcast`, `/refund`

## Setup

```bash
pip install -r requirements.txt
```

Set env vars:
```
TELEGRAM_BOT_TOKEN=...
ANTHROPIC_API_KEY=...
ADMIN_IDS=comma_separated_telegram_user_ids
```

Run:
```bash
python3 main.py
```
