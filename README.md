# BO3 Workshop Map Tracker

Local website that tracks BO3 workshop maps (tag `Map`, newest first) in a SQLite file (`maps.db`).

## Setup
1. Get a Steam Web API key: https://steamcommunity.com/dev/apikey (domain can be `localhost`).
2. Copy `config.example.json` to `config.json` and paste the key in. `config.json` is git-ignored; never commit it.
3. `pip install flask requests` (once).
4. Double-click `start.bat`, or run `python app.py` and open http://localhost:5000.

## Use
- **Fetch new maps**: pulls the newest uploads and stops at the first map you already have (or at "max").
- **Get all maps**: imports every map on the workshop. Runs on the server until done, so you can close the tab. Click again to stop; it resumes where it left off.
- **Refresh Steam data**: re-downloads pictures, creators and Steam ratings for every map.
- Per map: set **Easter egg** (?/Yes/No), give **My rating** (click a star; click again to clear), and open **Notes** for a rich-text editor (Ctrl+S saves).
- The header shows overall progress (X / Y completed and %); filtered views show their own progress too.
- Tick **Completed** to stamp today's date; edit the date if needed.
- **Export CSV**: `MapName,SteamWorkshopLink,DateUploaded,Completed,DateCompleted,Creator,HasEasterEgg,SteamStars,SteamVotes,MyRating,Notes`.
Steam is called at most once per second, 100 maps per call, and stops immediately on a rate-limit reply.

## Unraid (Docker)
Your list is stored in `/mnt/user/appdata/bo3-tracker/maps.db` and survives rebuilds.

1. Copy this folder to the server, e.g. `\TOWER\appdata\bo3-tracker-src` (Windows Explorer, SMB share).
2. Unraid web UI → **Terminal** (top right `>_` icon), then build:
   ```
   cd /mnt/user/appdata/bo3-tracker-src
   docker build -t bo3-tracker .
   ```
3. Run it (replace the key):
   ```
   docker run -d --name bo3-tracker --restart unless-stopped \
     -p 5000:5000 \
     -e STEAM_API_KEY=PASTE_YOUR_KEY_HERE \
     -v /mnt/user/appdata/bo3-tracker:/data \
     bo3-tracker
   ```
4. Open `http://<unraid-ip>:5000` from any device on your network.

The container shows up in the **Docker** tab, where you can start/stop it and view logs.

**Updating after code changes:** copy the new files over, then:
```
cd /mnt/user/appdata/bo3-tracker-src
docker build -t bo3-tracker .
docker rm -f bo3-tracker
```
and rerun the step 3 command. Your map list is untouched.

**Alternative:** install the *Docker Compose Manager* plugin from Community Apps, point a new stack at `docker-compose.yml`, put your key in it, and use *Compose Up*.

## Unraid via "Add Container" (registry)
The image contains no secrets (the API key is set in Unraid), so it's safe to keep it public. Private works too, with one extra step.

### 1. Push the image to GitHub Container Registry (ghcr.io)
1. GitHub → Settings → Developer settings → Personal access tokens → **Tokens (classic)** → generate one with `write:packages` (and `read:packages`).
2. On your PC: `docker login ghcr.io` — username = your GitHub username, password = that token.
3. `.\publish.ps1 ghcr.io/YOUR_GITHUB_USERNAME/bo3-tracker`
4. New packages are private by default. To make it public: GitHub → your profile → **Packages** → bo3-tracker → Package settings → Change visibility.

### 2. Add the template to Unraid
1. Edit `unraid/my-bo3-tracker.xml` and replace `YOUR_GITHUB_USERNAME`.
2. Copy it to the flash drive: `\TOWER\flash\config\plugins\dockerMan\templates-user\my-bo3-tracker.xml`
3. Docker tab → **Add Container** → Template dropdown → **bo3-tracker**. Paste your Steam API key and click **Apply**.

### If the package is private
Unraid needs to log in to pull it. In the Unraid terminal: `docker login ghcr.io` (token only needs `read:packages`).
Unraid keeps `/root` in RAM, so this login is lost on reboot. To persist it, install the **User Scripts** plugin and add a script set to *At Startup of Array*:
```
echo YOUR_READ_TOKEN | docker login ghcr.io -u YOUR_GITHUB_USERNAME --password-stdin
```

### Updating
Run `.\publish.ps1 ...` again, then in Unraid's Docker tab click the container → **Force Update**. Your list in appdata is kept.
