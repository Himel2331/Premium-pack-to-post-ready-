# Telegram Premium Emoji Converter Bot

এই bot Render-এ সহজে deploy করার জন্য তৈরি করা হয়েছে। আপনার আগের Render-ready bot structure-এর মতোই health endpoint + polling mode রাখা হয়েছে।

## কী করতে পারে

- `t.me/addemoji/...` pack select করতে পারে
- current chat-এর জন্য selected pack save করে
- normal emoji → matching premium/custom emoji convert করতে পারে
- raw `custom_emoji_id` → premium emoji render করতে পারে
- text message auto-convert করতে পারে
- photo/video/document/animation/audio/voice caption convert করে resend করতে পারে
- reply করে `/convert` দিলে text বা caption convert করতে পারে
- `/ids` দিয়ে pack-এর সব custom emoji ID list করতে পারে

## Commands

- `/start`
- `/help`
- `/ping`
- `/id`
- `/setpack <addemoji_link_or_set_name>`
- `/currentpack`
- `/clearpack`
- `/packinfo [link_or_set_name]`
- `/ids <link_or_set_name>`
- `/convert <text>`

## Quick usage

1. `/setpack https://t.me/addemoji/vector_icons_by_fStikBot`
2. তারপর plain emoji text পাঠান, যেমন `🙂 Hello 😎`
3. অথবা raw ID পাঠান, যেমন `5219899949281453881`
4. media caption থাকলে media resend হবে converted caption সহ
5. reply দিয়ে `/convert` দিলে replied message/caption convert হবে

## Render deploy

1. এই project GitHub repo-তে push করুন
2. Render-এ repo connect করুন
3. `render.yaml` automatically detect হবে
4. `TELEGRAM_BOT_TOKEN` env দিন
5. Deploy করুন

## Important note

Telegram custom emoji পাঠানোর জন্য bot-এর permission লাগতে পারে। Bot owner-এর Telegram Premium থাকা বা bot-এর custom emoji capability enabled থাকা দরকার। নাহলে Telegram send করার সময় error দিতে পারে।

## Persistence note

ডিফল্টভাবে SQLite database `/tmp`-এ রাখা হয়েছে। Render service restart/redeploy হলে selected pack cache reset হতে পারে। স্থায়ীভাবে রাখতে চাইলে Render persistent disk ব্যবহার করে `DATA_DIR`/`DB_PATH` সেট করুন।

## Files

- `bot.py` - main bot
- `requirements.txt` - dependencies
- `.env.example` - env template
- `render.yaml` - Render config

