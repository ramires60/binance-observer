# Binance Observer 3.0 — GitHub Actions

Educational, read-only monitoring of public Binance USDT-M perpetual market data. **No trades are placed.**

## Setup
1. Upload `binance_observer.py`, `requirements.txt`, and `.github/workflows/observer.yml` to a **private** GitHub repository, preserving the workflow path.
2. Open repository **Settings → Secrets and variables → Actions → New repository secret**.
3. Add `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` separately. Do not add them to source code or README.
4. Open **Actions → Binance Observer → Run workflow** for a test.

Runs every 6 hours (UTC). GitHub scheduled runs can be delayed or skipped; no uptime guarantee. Each run scans the market and sends one summary. The SQLite historical database of the continuous desktop edition **is not persisted** in this version. No cooldown or cross-run historical outcomes are tracked. Some regions/networks may block Binance public endpoints. A failed run is visible under Actions.

GitHub Free has usage and storage limits. Check your account's billing settings before enabling more frequent runs.
