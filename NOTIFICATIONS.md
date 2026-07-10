# Notifications setup

AzMon can push alerts to **desktop, phone (WhatsApp / Telegram / ntfy)**, on top
of the always-on in-page banner + sound. Enable any mix in `config.yaml` under
`senders:` (or edit it live from the ⚙ **Settings → Edit config** in the app —
changes apply without a restart). Test any channel with **Settings → Send test
notification**.

Only alerts at/above `min_severity` that are **newly fired** notify (dashboard
still shows everything). Muted rules and quiet hours are respected.

**Picking one:** on your own laptop, the Windows toast needs zero setup.
For your phone, ntfy is the fastest (~1 min, no account); Telegram and
WhatsApp take a couple more minutes but you likely already have the app
installed. You can enable more than one at once — every enabled channel
fires for every notifying alert.

Every channel below only ever receives a short summary (severity, rule name,
resource name, count, timestamp) — never the full alert description or KQL
results. See [SECURITY.md](SECURITY.md#third-party-senders-opt-in) for the
exact data-flow table.

---

## 🖥️ Desktop (Windows toast) — already on
```yaml
senders:
  windows_toast:
    enabled: true
    app_id: "AzMon"
```
Nothing to set up. Windows only (silently skipped elsewhere).

## 📱 WhatsApp (free, via CallMeBot) — ~1 minute
1. Add **+34 644 51 95 23** to your contacts.
2. Send it this exact WhatsApp message: **`I allow callmebot to send me messages`**
3. It replies with your **apikey**.
4. Config:
   ```yaml
   senders:
     whatsapp:
       enabled: true
       phone: "4479XXXXXXXX"     # your number, country code, NO +
       apikey: "123456"          # from the reply
   ```
No Meta app, no cost for personal use. CallMeBot is a third-party free relay —
only the short summary line is sent to it (see the privacy note above), but
it does see your phone number and API key as part of every request.

## ✈️ Telegram — ~2 minutes
1. In Telegram, message **@BotFather** → `/newbot` → copy the **bot token**.
2. Message your new bot once (say "hi") so it can reply to you.
3. Get your **chat id**: open
   `https://api.telegram.org/bot<YOUR_TOKEN>/getUpdates` in a browser and copy
   `chat.id` from the JSON.
4. Config:
   ```yaml
   senders:
     telegram:
       enabled: true
       bot_token: "123456:ABC..."
       chat_id: "987654321"
   ```

## 🔔 ntfy (phone push anywhere) — ~1 minute
1. Install the **ntfy** app (iOS/Android), subscribe to a **secret topic name**.
2. Config:
   ```yaml
   senders:
     ntfy:
       enabled: true
       server: "https://ntfy.sh"
       topic: "azmon-your-secret-topic-9f2a"   # unguessable = private
       token: ""                                # only if self-hosted w/ auth
   ```

---

**Tip:** after editing in the UI, hit **Save** — you'll see
`config reloaded` in **Settings → View logs**, and notify channels switch over
live. Secrets live only in your local `config.yaml` (git-ignored).
