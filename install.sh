#!/bin/bash
# wib uploads: установка в один шаг.
# Запускай двойным кликом по «Установить.command» или командой: bash install.sh
set -u
cd "$(dirname "$0")" || exit 1
export PATH="/opt/homebrew/bin:/usr/local/bin:$PATH"
WIB_HOME="${WIB_HOME:-$HOME/.wibuploads}"
export WIB_HOME

say()  { printf "\n\033[1m%s\033[0m\n" "$1"; }
ok()   { printf "  \033[32m✓\033[0m %s\n" "$1"; }
warn() { printf "  \033[33m!\033[0m %s\n" "$1"; }
run()  { if [ -n "${WIB_DRYRUN:-}" ]; then echo "  [проба] $*"; else "$@"; fi; }
have() { case " ${WIB_FAKE_MISSING:-} " in *" $1 "*) return 1;; esac; command -v "$1" >/dev/null 2>&1; }
pause() { [ -n "${WIB_ANSWER:-}" ] && return; printf "\nНажми Enter, чтобы закрыть окно. "; read -r _ </dev/tty; }
ask() {
  if [ -n "${WIB_ANSWER:-}" ]; then [ "$WIB_ANSWER" = "д" ]; return; fi
  printf "%s (д/н) " "$1"; read -r a </dev/tty
  case "$a" in д|Д|да|Да|y|Y|yes) return 0;; *) return 1;; esac
}
install_brew() { /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"; }
stop() {
  printf "\n\033[31mНе получилось:\033[0m %s\n" "$1"
  printf "Если не выходит, спроси ChatGPT: готовый текст для него лежит в README, в разделе «Не получается?».\n"
  pause; exit 1
}

say "wib uploads: установка"
echo "Проверю компьютер, поставлю то, чего не хватает, и открою страницу. Это займёт несколько минут."

[ "$(uname)" = "Darwin" ] || stop "Пока программа работает только на Mac."

say "1. Python"
if python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)' 2>/dev/null; then
  ok "Python $(python3 -c 'import platform; print(platform.python_version())') на месте"
else
  run xcode-select --install 2>/dev/null
  stop "Нужны инструменты разработчика от Apple. Должно было появиться окно установки. Дождись его конца и запусти этот файл снова."
fi

say "2. Программы для скачивания"
need=()
have yt-dlp || need+=(yt-dlp)
have ffmpeg || need+=(ffmpeg)
if [ ${#need[@]} -eq 0 ]; then
  ok "yt-dlp и ffmpeg уже стоят"
else
  warn "Не хватает: ${need[*]}"
  if ! have brew; then
    echo "  Чтобы их поставить, нужен Homebrew. Это установщик программ для Mac, он безопасный и популярный."
    ask "  Поставить Homebrew? Понадобится пароль от Mac." || stop "Без Homebrew не получится. Поставь его с brew.sh и запусти файл снова."
    run install_brew
    for b in /opt/homebrew/bin/brew /usr/local/bin/brew; do [ -x "$b" ] && eval "$("$b" shellenv)"; done
  fi
  run brew install "${need[@]}" || stop "Homebrew не смог поставить ${need[*]}. Прочитай сообщение выше или спроси ChatGPT."
  ok "Поставил: ${need[*]}"
fi

say "3. Посты Telegram (по желанию)"
if [ -x "$WIB_HOME/venv/bin/python3" ] && "$WIB_HOME/venv/bin/python3" -c 'import playwright' 2>/dev/null; then
  ok "Уже стоит"
elif ask "  Нужно сохранять посты Telegram картинками? Для этого скачается ещё около 200 МБ."; then
  run python3 -m venv "$WIB_HOME/venv" \
    && run "$WIB_HOME/venv/bin/pip" install -q --disable-pip-version-check playwright \
    && run "$WIB_HOME/venv/bin/python3" -m playwright install chromium \
    && ok "Готово" || warn "Не вышло. Пропустим, остальное будет работать. Повтори позже, запустив этот файл ещё раз."
else
  echo "  Пропускаю. Передумаешь, запусти этот файл ещё раз."
fi

say "4. Запуск"
if [ -n "${WIB_DRYRUN:-}" ]; then
  echo "  [проба] sh ./autostart"
else
  sh ./autostart || stop "Сервис не запустился. Лог лежит здесь: ~/Library/Logs/ytads.log"
fi
[ -n "${WIB_NOOPEN:-}" ] || run open "http://wibuploads.localhost:8788"

say "Готово"
echo "  Страница: http://wibuploads.localhost:8788"
echo "  Она открывается только на этом компьютере. Сервис запускается сам при входе в систему."
echo "  Дальше открой вкладку «Старт» на странице: там проверка связи и подключение ключей."
echo "  Чтобы выключить сервис: sh autostart off"
pause
