# Jivo Bot

Flask-сервис принимает webhook-события Jivo, ищет ответы в базе знаний `templates.json` и отправляет сообщения обратно через Jivo Bot API. Главный модуль приложения — `bot_search_improved.py`.

## Требования

- Python 3.10 или новее.
- Windows PowerShell либо другой терминал.
- Для реальной интеграции с Jivo — публичный HTTPS URL, доступный из интернета.

Зависимости перечислены в `requirements.txt`. Для разработки используйте `requirements-dev.txt`; для Ubuntu production — `requirements-ubuntu.txt`.

## Подготовка окружения

Из корня проекта создайте виртуальное окружение, если `.venv` ещё нет, затем активируйте его:

```powershell
cd C:\bot
py -m venv .venv
.\.venv\Scripts\Activate.ps1
```

Если PowerShell запрещает активацию скрипта, разрешите её только для текущего окна:

```powershell
Set-ExecutionPolicy -Scope Process -ExecutionPolicy RemoteSigned
.\.venv\Scripts\Activate.ps1
```

Установите основные пакеты:

```powershell
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```

`redis` устанавливайте только при использовании Redis через `REDIS_URL`:

```powershell
python -m pip install redis
```

Для запуска тестов установите dev-зависимости:

```powershell
python -m pip install -r requirements-dev.txt
```

## Запуск локально

В PowerShell задайте обязательные настройки и запустите приложение:

```powershell
cd C:\bot
$env:BOT_TOKEN="замените-на-секретный-токен"
$env:JIVO_PROVIDER_ID="123456"
$env:PORT="8000"
$env:TIMEZONE="Europe/Moscow"

python bot_search_improved.py
```

Переменные `$env:...` действуют в текущем окне PowerShell. Оставьте процесс запущенным; для команд проверки откройте второе окно терминала.

Проверка сервера:

```powershell
Invoke-RestMethod http://127.0.0.1:8000/health
Invoke-RestMethod http://127.0.0.1:8000/ready
```

`/health` показывает состояние приложения, рабочее ли сейчас время, часовой пояс, хранилище состояния и количество загруженных шаблонов. `BOT_TOKEN` и `JIVO_PROVIDER_ID` обязательны уже при импорте приложения.

Запуск тестов из корня проекта:

```powershell
python -m pytest
```

## Подключение Jivo

Jivo не может отправить webhook на `localhost`. Для локальной проверки откройте ещё одно окно PowerShell и запустите установленный ngrok:

```powershell
ngrok http 8000
```

Ngrok покажет публичный HTTPS-адрес. В настройках webhook провайдера Jivo укажите адрес с токеном в конце:

```text
https://<публичный-домен>/<BOT_TOKEN>
```

`JIVO_PROVIDER_ID` — ID bot-provider в Jivo; он используется для отправки ответов обратно через Jivo API. Токен в URL должен точно совпадать с `BOT_TOKEN`. Не публикуйте и не отправляйте токен в открытые чаты.

Webhook принимает `POST /<token>` с JSON-событием Jivo. Локальный адрес `http://127.0.0.1:8000/<BOT_TOKEN>` можно использовать для тестового запроса, но он не доступен самому Jivo.

## Ubuntu VPS

Инструкция рассчитана на Ubuntu 22.04/24.04, один VPS и один Gunicorn worker. Приложение слушает только `127.0.0.1:8000`; наружу публикуются лишь Nginx-порты 80/443. SQLite годится для одного сервера и одного worker-процесса. Не размещайте её на NFS и не запускайте несколько независимых VPS с одной SQLite-базой.

1. Установите системные пакеты и создайте отдельную учётную запись приложения:

```bash
sudo apt update
sudo apt install -y git python3 python3-venv nginx certbot python3-certbot-nginx ufw
sudo adduser --system --group --home /opt/redsms-jivo --no-create-home redsms-jivo
sudo install -d -o redsms-jivo -g redsms-jivo -m 0750 /opt/redsms-jivo
```

2. Склонируйте проект и установите зависимости в отдельное окружение:

```bash
sudo install -d -o root -g root -m 0700 /root/.ssh
sudo ssh-keygen -t ed25519 -N '' -C 'redsms-jivo-vps-deploy' -f /root/.ssh/redsms_jivo
sudo cat /root/.ssh/redsms_jivo.pub
```

Добавьте выведенный **публичный** ключ в GitHub repository settings → Deploy keys без права записи. Никогда не публикуйте файл `/root/.ssh/redsms_jivo`. Затем клонируйте проект и установите зависимости:

```bash
sudo env GIT_SSH_COMMAND="ssh -i /root/.ssh/redsms_jivo -o IdentitiesOnly=yes" git clone git@github.com:gstazhkov/redsms_jivo.git /opt/redsms-jivo
sudo python3 -m venv /opt/redsms-jivo/.venv
sudo /opt/redsms-jivo/.venv/bin/pip install --upgrade pip
sudo /opt/redsms-jivo/.venv/bin/pip install -r /opt/redsms-jivo/requirements-ubuntu.txt
sudo chown -R root:redsms-jivo /opt/redsms-jivo
```

Код и virtualenv остаются read-only для service account; запись приложению нужна только в `/var/lib/redsms-jivo`.

3. Создайте закрытый файл настроек. Используйте уникальные секреты, созданные на самом VPS; не храните этот файл в Git и не присылайте секреты в чат:

```bash
sudo install -d -o root -g redsms-jivo -m 0750 /etc/redsms-jivo
sudo install -o root -g redsms-jivo -m 0640 /opt/redsms-jivo/.env.example /etc/redsms-jivo/bot.env
sudoedit /etc/redsms-jivo/bot.env
```

Замените примеры на реальные значения из Jivo и Omnidesk. Для `BOT_TOKEN` и `OMNI_WEBHOOK_TOKEN` можно получить отдельные случайные значения командой `openssl rand -hex 32`. Укажите публичный hostname в `TRUSTED_HOSTS`, домен API в `OMNI_API_URL`, а SQLite оставьте в `/var/lib/redsms-jivo/support.sqlite3`.

4. Установите systemd unit и запустите приложение:

```bash
sudo install -m 0644 /opt/redsms-jivo/deploy/redsms-jivo.service /etc/systemd/system/redsms-jivo.service
sudo systemctl daemon-reload
sudo systemctl enable --now redsms-jivo
sudo systemctl status redsms-jivo
sudo journalctl -u redsms-jivo -n 100 --no-pager
```

В unit настроены один worker, loopback bind, отдельная запись в `/var/lib/redsms-jivo`, `UMask=0077`, read-only system paths и запрет привилегий. Не увеличивайте число `--workers`, пока состояние cooldown не вынесено в общий Redis и запуск фонового recovery worker не выделен в отдельный процесс.

5. Настройте Nginx. Сначала замените `bot.example.com` в файле на ваш DNS-домен, направленный на VPS:

```bash
sudo install -m 0644 /opt/redsms-jivo/deploy/nginx-log-format.conf /etc/nginx/conf.d/redsms-jivo-log-format.conf
sudo install -m 0644 /opt/redsms-jivo/deploy/nginx.conf /etc/nginx/sites-available/redsms-jivo
sudoedit /etc/nginx/sites-available/redsms-jivo
sudo ln -s /etc/nginx/sites-available/redsms-jivo /etc/nginx/sites-enabled/redsms-jivo
sudo nginx -t
sudo systemctl reload nginx
```

В Nginx используется access-log формат без URL-пути: токены Jivo и OmniDesk входят в путь webhook и не должны попадать в журналы. Затем получите TLS-сертификат:

```bash
sudo certbot --nginx -d bot.example.com
```

В firewall разрешите SSH до включения UFW, чтобы не потерять удалённый доступ:

```bash
sudo ufw allow OpenSSH
sudo ufw allow 80/tcp
sudo ufw allow 443/tcp
sudo ufw enable
sudo ufw status
```

После этого webhook Jivo должен указывать на `https://bot.example.com/<BOT_TOKEN>`, а Omnidesk — на `https://bot.example.com/omni/<OMNI_WEBHOOK_TOKEN>`. Проверьте `https://bot.example.com/health` и `https://bot.example.com/ready`.

Для обновления приложения используйте тот же deploy key:

```bash
sudo env GIT_SSH_COMMAND="ssh -i /root/.ssh/redsms_jivo -o IdentitiesOnly=yes" git -C /opt/redsms-jivo pull --ff-only
sudo /opt/redsms-jivo/.venv/bin/pip install -r /opt/redsms-jivo/requirements-ubuntu.txt
sudo chown -R root:redsms-jivo /opt/redsms-jivo
sudo systemctl restart redsms-jivo
```

Делайте регулярную зашифрованную резервную копию `/var/lib/redsms-jivo/support.sqlite3`; база содержит тексты обращений и идентификаторы клиентов. Доступ к SSH ограничьте ключами, включите security updates Ubuntu и держите секреты только в `/etc/redsms-jivo/bot.env`.

## Как обрабатываются события

- `CLIENT_MESSAGE`: в рабочее время бот приглашает оператора. Вне рабочего времени ищет подходящий шаблон; если совпадение не найдено или оно неоднозначно, отправляет общий автоответ. При настроенных Telegram-переменных также отправляет уведомление.
- `AGENT_UNAVAILABLE`: отправляет общий автоответ с ограничением частоты.
- `CHAT_CLOSED`: очищает временные ограничения для чата.
- Повторные события с тем же ID игнорируются в течение `EVENT_TTL`.

Webhook сначала записывает событие в SQLite и только затем подтверждает его Jivo. Исходящие сообщения также сохраняются в SQLite до отправки; при ошибке доставки они автоматически повторяются с возрастающей задержкой. Поэтому входящие и исходящие операции переживают перезапуск процесса. По умолчанию база — `support.sqlite3` в текущей рабочей папке; задайте `SUPPORT_DB_PATH`, чтобы выбрать другой путь. В базе хранятся payload обращений, включая текст сообщений и ID чатов/клиентов: ограничьте доступ к файлу и включите его в резервное копирование.

Каждый вызов передачи оператору сохраняется в таблицу `operator_handoffs`: статус `pending` означает, что Jivo ещё не подтвердил отправку, `sent` — что запрос принят. Число попыток и последняя ошибка также записываются. Исходящие операции находятся в таблице `outbound_jobs`, принятые webhook-события — в `inbound_events`.

Защита от повторных входящих событий и исходящая очередь хранятся в SQLite. Redis используется для cooldown автоответов и уведомлений; без Redis cooldown хранится в памяти процесса. Для нескольких worker-процессов подключите Redis; SQLite-файл должен находиться на локальном диске, доступном приложению.

Для REDSMS OmniDesk работает отдельный маршрут `POST /omni/<OMNI_WEBHOOK_TOKEN>`. Он принимает payload правила Omnidesk, ищет ответ в той же базе шаблонов, ставит ответ в SQLite outbox и назначает обращение на заданную группу или сотрудника. API-вызовы идут по официальному Omnidesk REST API с Basic Auth. Сообщения с `message.staff_id` игнорируются, чтобы бот не отвечал на собственные ответы или реплики оператора.

Чтобы подключить Omnidesk:

1. В настройках Omnidesk API создайте API-ключ и используйте email сотрудника с правом отвечать и назначать обращения.
2. Задайте переменные `OMNI_API_URL`, `OMNI_STAFF_EMAIL`, `OMNI_API_KEY`, `OMNI_WEBHOOK_TOKEN` и одно из `OMNI_GROUP_ID` / `OMNI_ASSIGNEE_STAFF_ID`.
3. В правилах Omnidesk добавьте действие «Выполнить вебхук» для нового входящего сообщения, метод `POST`, URL `https://<публичный-домен>/omni/<OMNI_WEBHOOK_TOKEN>`, тип содержимого JSON.
4. Отправляйте JSON, содержащий ID обращения, ID события/сообщения и текст, например:

```json
{
  "event_id": "unique-event-id",
  "case": {"case_id": "12345"},
  "message": {"message_id": "67890", "content": "Текст клиента", "staff_id": 0},
  "user": {"user_id": "2468", "full_name": "Имя клиента"}
}
```

В конструкторе запроса Omnidesk свяжите эти поля с доступными переменными события в вашем аккаунте. Названия переменных шаблона зависят от правила; пример выше — контракт принимающего endpoint, а не готовый текст синтаксиса Omnidesk.

Бот отправляет ответ вызовом `POST /api/cases/{case_id}/messages.json` и передаёт обращение вызовом `PUT /api/cases/{case_id}.json`. При передаче статус выставляется в `open`; если задан `OMNI_GROUP_ID`, обращение назначается на эту группу, а при заданном `OMNI_ASSIGNEE_STAFF_ID` — на сотрудника. Эти операции сохраняются в SQLite outbox и повторяются при временных ошибках.

`omni_adapter.py` остаётся общим адаптером кода для другого payload-формата; REDSMS Omnidesk использует специализированный `omnidesk_client.py`.

## Шаблоны ответов

Файл по умолчанию — `templates.json`. Он перечитывается при изменении файла. Для ответа используются записи со статусом `ready` и непустым `details.response`:

```json
{
  "data": [
    {
      "id": 1,
      "title": "Проблема с оплатой",
      "status": "ready",
      "details": {
        "trigger": "Не пришли деньги после оплаты",
        "response": "Проверим поступление платежа и вернёмся с ответом."
      }
    }
  ]
}
```

Поиск сопоставляет текст обращения с заголовком и триггером шаблона, а также учитывает другие доступные поля записи. Порог и допустимая разница между похожими результатами настраиваются переменными `MATCH_THRESHOLD`, `MATCH_MARGIN` и `FUZZY_THRESHOLD`.

## Переменные окружения

| Переменная | Обязательна | По умолчанию | Назначение |
|---|---:|---|---|
| `BOT_TOKEN` | Да | — | Секрет для защиты webhook, также часть URL отправки в Jivo. |
| `JIVO_PROVIDER_ID` | Да | — | ID bot-provider в Jivo. |
| `PORT` | Нет | `8000` | Порт Flask-сервера. |
| `MAX_REQUEST_BYTES` | Нет | `1048576` | Максимальный размер входящего webhook-запроса, 1 MiB. |
| `TRUSTED_HOSTS` | Нет | Не ограничены | Допустимые HTTP Host через запятую; на VPS укажите только свой домен. |
| `TIMEZONE` | Нет | `Europe/Moscow` | Часовой пояс рабочего расписания. |
| `WORK_START`, `WORK_END` | Нет | `9`, `19` | Начало и конец рабочего времени; час окончания не включается. |
| `WORK_DAYS` | Нет | `0,1,2,3,4` | Рабочие дни: 0 — понедельник, 6 — воскресенье. |
| `AUTO_REPLY` | Нет | Текст в коде | Общий ответ вне рабочего времени. |
| `TEMPLATES_FILE` | Нет | `templates.json` | Путь к файлу базы ответов. |
| `TEMPLATE_FOOTER` | Нет | Пусто | Необязательный текст в конце шаблонного ответа. |
| `TG_BOT_TOKEN`, `TG_CHAT_ID` | Нет | Не заданы | Обе переменные нужны для уведомлений в Telegram. |
| `REDIS_URL` | Нет | Не задан | URL Redis; без него используется память процесса. |
| `SUPPORT_DB_PATH` | Нет | `support.sqlite3` | Путь к SQLite-базе очередей и журнала передач оператору. |
| `OMNI_API_URL` | Для OmniDesk | — | Корень API аккаунта, например `https://<аккаунт>.omnidesk.ru`. |
| `OMNI_STAFF_EMAIL`, `OMNI_API_KEY` | Для OmniDesk | — | Basic Auth Omnidesk: email сотрудника и API-ключ. |
| `OMNI_WEBHOOK_TOKEN` | Для OmniDesk | — | Секретный сегмент для входящего пути `/omni/<токен>`. |
| `OMNI_GROUP_ID` | Для передачи | — | ID группы Omnidesk для назначения обращения. |
| `OMNI_ASSIGNEE_STAFF_ID` | Нет | — | ID сотрудника; можно задать вместе с группой. |
| `EVENT_TTL` | Нет | `600` секунд | Время защиты от обработки повторного события. |
| `REPLY_COOLDOWN` | Нет | `43200` секунд | Интервал между автоответами в одном чате. |
| `TG_COOLDOWN` | Нет | `600` секунд | Интервал между Telegram-уведомлениями по одному чату. |
| `BACKGROUND_WORKERS` | Нет | `8` | Число фоновых потоков приложения. |
| `MATCH_THRESHOLD` | Нет | `0.52` | Минимальный порог поиска шаблона. |
| `MATCH_MARGIN` | Нет | `0.08` | Минимальная разница между кандидатами. |
| `FUZZY_THRESHOLD` | Нет | `0.86` | Порог нечёткого совпадения. |
| `LOG_LEVEL` | Нет | `INFO` | Уровень логирования, например `DEBUG`, `INFO`, `WARNING`. |

Дополнительные таймауты HTTP-запросов задаются через `REQUEST_TIMEOUT_CONNECT` и `REQUEST_TIMEOUT_READ` (по умолчанию 3 и 7 секунд).

## HTTP-эндпоинты

| Метод и путь | Назначение |
|---|---|
| `GET /health` | Состояние приложения и рабочее время. |
| `GET /ready` | Проверка готовности загрузить шаблоны. |
| `GET /metrics` | Счётчики совпавших, не найденных и неоднозначных шаблонов. |
| `POST /<token>` | Webhook для событий Jivo. |
| `POST /omni/<token>` | Webhook REDSMS OmniDesk. |
