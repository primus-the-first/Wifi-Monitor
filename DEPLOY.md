# Putting Hotspot Caps online (Render + a free Postgres)

What goes online is the **shared record**: each device's usage, the cap, device names and which
networks are capped. The counting and blocking stay on whichever laptop is hosting the hotspot.
Both laptops report to the server and read the combined totals, so a phone that used 800 MB on
laptop A starts at 800 MB when it joins laptop B.

## Why two services

Render's free web service is fine for the server, but its free **database is deleted after 30 days**,
and files on its disk (including SQLite) are wiped whenever the service restarts or sleeps. So the
shared record lives in a free Postgres from another provider:

- **Supabase** (recommended): free Postgres, 500 MB. Use the **Session pooler** connection string (it works over IPv4).
- **Neon**: free Postgres, 1 GB, but only 100 compute-hours a month. A database that syncs all day never idles, so it would run out around day 17.

Check each provider's current free-plan terms before relying on them.

## Steps

1. **Database.** Create a free project on Supabase. In *Project Settings → Database → Connection string*
   choose **Session pooler** and copy the `postgresql://...` address (put your database password in place of `[YOUR-PASSWORD]`).
2. **GitHub.** Create a new *private* repository and upload the **contents of this `server` folder**
   (`app.py`, `db.py`, `ledger.py`, `admin.html`, `requirements.txt`, `render.yaml`). Don't upload `sync.json` or any `.db` file.
3. **Render.** *New → Blueprint*, pick that repository. When it asks for values:
   - `DATABASE_URL`: the address from step 1
   - `ADMIN_PASSWORD`: a password you choose (the admin page's user name is `admin`)
   - `AGENT_KEY` is generated for you. After the first deploy, copy it from the service's *Environment* tab.
4. **Check it.** Open `https://YOUR-SERVICE.onrender.com/healthz`. It should say `{"ok":true,"db":"postgres"}`.
   (The very first request after a sleep takes about a minute.)
5. **Connect each laptop.** Run once on each laptop (it asks for administrator approval), then restart the app:

   ```
   HotspotCaps.exe --setup-sync https://YOUR-SERVICE.onrender.com THE-AGENT-KEY --name Laptop-A
   ```

   Use a different `--name` on the other laptop. The first laptop uploads its existing history.
   **Install the second laptop fresh. Don't copy the first laptop's database to it**, or its history is counted twice.
6. **Admin page.** Open `https://YOUR-SERVICE.onrender.com`, user `admin`. It shows who is hosting right now,
   every device's usage, and lets you rename devices, reset usage, change the cap, and turn caps on or off per network.

## How it behaves

- The window shows a **☁ SYNCED** badge. **OFFLINE · RETRYING** means the server can't be reached: the laptop keeps
  counting and enforcing from its own numbers, and sends what it counted when the server answers.
- If both laptops host the same network at once, counting stays correct and both log a warning. Turn one hotspot off.
- Render's free service sleeps after 15 minutes with no traffic. A running laptop syncs every 15 seconds, which keeps it awake.
- Closing the app makes one last quick sync, so the other laptop starts from current numbers.

## Keeping it safe

- The agent key and admin password travel over HTTPS only. The app refuses a server address that isn't `https://`.
- To change the key: edit `AGENT_KEY` in Render, then run `--setup-sync` again on each laptop.
- Never put the local dashboard (`python -m wifimon --web`) on the internet: it has no login. The online admin page does.
