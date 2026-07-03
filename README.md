# Regulus

A trust-tier moderation bot for Discord community servers.

Assigns new joiners to an `@Unverified` role, computes a trust score from account signals,
and either auto-promotes, holds for mod review, or auto-kicks depending on the score band.
Behavioral triggers on untrusted users escalate to immediate action.

## Setup

1. Create a Python virtual environment and install dependencies:
   ```
   python -m venv .venv
   source .venv/bin/activate   # on Windows: .venv\Scripts\activate
   pip install -r requirements.txt
   ```
2. Copy `.env.example` to `.env` and fill in your bot token and test guild ID.
3. Run the bot:
   ```
   python bot.py
   ```
