#!/bin/bash
# Move the trading agent from this Mac to GitHub Actions. Run once, from Terminal:
#
#   cd ~/agentic-ai/trading-agent && ./scripts/move-to-github.sh
#
# It will:
#   1. stop the Mac schedules (so two copies never trade the same account)
#   2. create a PRIVATE GitHub repo and push the code (.env is never uploaded)
#   3. copy the bot's current book (holdings, open orders, history) to a `state` branch
#   4. copy your keys from .env into the repo's encrypted Actions secrets
#   5. start a "doctor" run on GitHub to check every connection
set -euo pipefail
cd "$(dirname "$0")/.."
REPO_NAME="${1:-trading-agent}"

say() { printf '\n\033[1m%s\033[0m\n' "$*"; }

# --- 0. checks ---------------------------------------------------------------
if ! command -v gh >/dev/null 2>&1; then
  echo "GitHub CLI not found. Install it, log in, then run this again:"
  echo "  brew install gh && gh auth login"
  exit 1
fi
if ! gh auth status >/dev/null 2>&1; then
  echo "Log in to GitHub first:  gh auth login   (choose GitHub.com, HTTPS, login with a web browser)"
  exit 1
fi
gh auth setup-git >/dev/null 2>&1 || true
[ -f .env ] || { echo ".env not found in $(pwd)"; exit 1; }
LOGIN=$(gh api user --jq .login)

# --- 1. stop the Mac schedules -------------------------------------------------
say "1/5 Stopping the schedules on this Mac"
./ta remove-schedule || true
if [ -x .venv/bin/python ]; then PY=.venv/bin/python; else PY=python3; fi
if ! "$PY" - <<'PY'
import fcntl, os, sys
path = "data/.run.lock"
if os.path.exists(path):
    with open(path, "a") as f:
        try:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            sys.exit(1)
PY
then
  echo "A run is still going on this Mac. Wait for it to finish (a few minutes), then run this again."
  exit 1
fi

# --- 2. code -> private repo (main) --------------------------------------------
say "2/5 Pushing the code to github.com/$LOGIN/$REPO_NAME (private)"
# The GitHub workflow files ship in ci/workflows/ and are installed here, into
# .github/workflows/, where GitHub looks for them.
if [ -d ci/workflows ]; then
  mkdir -p .github/workflows
  cp ci/workflows/*.yml .github/workflows/
  rm -rf ci
fi
[ -f .github/workflows/_run.yml ] || { echo "Workflow files are missing (.github/workflows)."; exit 1; }
[ -d .git ] || git init -q -b main
if [ -z "$(git config user.email || true)" ]; then
  git config user.name "$LOGIN"
  git config user.email "$(gh api user --jq .id)+$LOGIN@users.noreply.github.com"
fi
git add -A
# Safety: refuse if a secret or local-only file is about to be committed
if git diff --cached --name-only | grep -E '(^|/)\.env$|^data/|^logs/|^reports/|^\.venv/' ; then
  echo "Refusing to continue: the files above must not be uploaded. Check .gitignore."
  exit 1
fi
git diff --cached --quiet || git commit -q -m "Trading agent"
if ! git remote get-url origin >/dev/null 2>&1; then
  if gh repo view "$LOGIN/$REPO_NAME" >/dev/null 2>&1; then
    git remote add origin "https://github.com/$LOGIN/$REPO_NAME.git"
  else
    gh repo create "$REPO_NAME" --private --source . --remote origin >/dev/null
  fi
fi
if [ "$(gh repo view "$LOGIN/$REPO_NAME" --json visibility --jq .visibility)" != "PRIVATE" ]; then
  echo "The repo $LOGIN/$REPO_NAME is not private. Make it private first (it will hold your trade history)."
  exit 1
fi
git branch -M main
git push -q -u origin main

# --- 3. current book -> state branch --------------------------------------------
say "3/5 Copying the bot's current book to the 'state' branch"
if git ls-remote --exit-code --heads origin state >/dev/null 2>&1; then
  echo "A state branch already exists on GitHub; leaving the cloud copy as it is."
else
  TMP=$(mktemp -d)
  mkdir -p "$TMP/data" "$TMP/reports"
  cp data/state-*.json "$TMP/data/" 2>/dev/null || true
  [ -f data/STOP ] && cp data/STOP "$TMP/data/"
  if [ -d data/tradingagents/memory ]; then
    mkdir -p "$TMP/data/tradingagents" && cp -R data/tradingagents/memory "$TMP/data/tradingagents/"
  fi
  [ -d reports ] && cp -R reports/. "$TMP/reports/"
  printf '%s\n' 'data/tradingagents/cache/' 'data/tradingagents/logs/' 'data/.run.lock' > "$TMP/.gitignore"
  ORIGIN=$(git remote get-url origin)
  (
    cd "$TMP"
    git init -q -b state
    git config user.name "$(git -C "$OLDPWD" config user.name)"
    git config user.email "$(git -C "$OLDPWD" config user.email)"
    git add -A
    git commit -q -m "Initial state from Mac"
    git push -q "$ORIGIN" state
  )
  rm -rf "$TMP"
  echo "Done: copied the bot's book and reports."
fi

# --- 4. keys -> encrypted Actions secrets ---------------------------------------
say "4/5 Copying keys from .env to GitHub Actions secrets"
for k in OPENAI_API_KEY ALPACA_PAPER_KEY_ID ALPACA_PAPER_SECRET_KEY ALPACA_LIVE_KEY_ID \
         ALPACA_LIVE_SECRET_KEY TELEGRAM_BOT_TOKEN TELEGRAM_CHAT_ID FRED_API_KEY; do
  v=$(grep -E "^${k}=" .env | tail -1 | cut -d= -f2- | sed -e 's/^["'\'']//' -e 's/["'\'']$//' || true)
  if [ -n "$v" ]; then
    printf '%s' "$v" | gh secret set "$k" --repo "$LOGIN/$REPO_NAME" >/dev/null && echo "  set $k"
  else
    echo "  skipped $k (empty in .env)"
  fi
done

# --- 5. mark this copy as moved, then check the cloud copy -----------------------
touch .runs-on-github
say "5/5 Starting a connection check on GitHub"
started=""
for _ in 1 2 3 4 5 6; do   # GitHub takes a few seconds to register new workflows
  sleep 5
  if gh workflow run manual.yml --repo "$LOGIN/$REPO_NAME" -f task=doctor >/dev/null 2>&1; then
    started=1; break
  fi
done
if [ -n "$started" ]; then
  echo "Started. Watch it at: https://github.com/$LOGIN/$REPO_NAME/actions"
else
  echo "Couldn't start it automatically. On GitHub: Actions > manual run > Run workflow > doctor."
fi

say "Moved. The bot now runs on GitHub on these schedules (Melbourne time):"
echo "  Crypto:  every 8 hours (10:15am, 6:15pm, 2:15am; +1h in daylight saving)"
echo "  Stocks:  Tue-Sat 8:30am (9:30am in daylight saving)"
echo "  Weekly:  Friday 6pm (5pm outside daylight saving)"
echo "This Mac copy will no longer trade. Your Mac can be off."
