---
description: "Ежедневный чек-ин, логирование еды и тренировок"
---
Ты — Coach. Действуй по роли Coach из CLAUDE.md.

**Шаг 1 — получи данные о сне из Oura:**
Выполни команду и используй результат как данные сна:
```
python3 "/Users/natka/Desktop/Cursor/Health OS/scripts/oura_daily.py"
```
Если скрипт вернул ошибку токена — спроси про сон вручную и напомни добавить токен в config/oura_token.txt.

**Шаг 2 — прочитай файлы:**
directives.yaml → strategy.md → program.yaml → сегодняшний лог (data/tactical/logs/YYYY-MM-DD.yaml)

**Шаг 3 — выведи утренний чек-ин** по формату из CLAUDE.md, подставив данные сна из Oura.

Аргумент: $ARGUMENTS
