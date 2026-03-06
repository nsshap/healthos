#!/bin/bash
# Проверяет обновления brain-love-and-robots и уведомляет через macOS Notification Center
# Запускается cron'ом ежедневно в 10:00

REPO_DIR="/Users/natka/Desktop/Cursor/brain-love-and-robots"
LOG_FILE="/Users/natka/Desktop/Cursor/Health OS/scripts/repo_check.log"

cd "$REPO_DIR" || exit 1

# Запоминаем текущий HEAD
BEFORE=$(git rev-parse HEAD)

# Забираем обновления
git fetch origin main --quiet 2>&1

# Смотрим что появилось
AFTER=$(git rev-parse origin/main)

if [ "$BEFORE" != "$AFTER" ]; then
    # Есть новые коммиты — собираем список
    NEW_COMMITS=$(git log "$BEFORE".."$AFTER" --oneline --no-walk=unsorted)
    COUNT=$(git rev-list "$BEFORE".."$AFTER" --count)

    # macOS уведомление
    osascript -e "display notification \"$COUNT новых обновлений в репо\" with title \"🧠 brain-love-and-robots\" subtitle \"Открой Claude Code → git pull\" sound name \"default\""

    # Логируем
    echo "[$(date '+%Y-%m-%d %H:%M')] НОВЫЕ КОММИТЫ ($COUNT):" >> "$LOG_FILE"
    echo "$NEW_COMMITS" >> "$LOG_FILE"
    echo "" >> "$LOG_FILE"

    # Применяем обновления
    git pull origin main --quiet 2>&1

    echo "[$(date '+%Y-%m-%d %H:%M')] Pull выполнен." >> "$LOG_FILE"
else
    echo "[$(date '+%Y-%m-%d %H:%M')] Нет изменений." >> "$LOG_FILE"
fi
