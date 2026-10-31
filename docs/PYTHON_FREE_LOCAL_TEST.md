# Testing the Python-free image against your real data (for a local Claude Code session)

Do this on the machine that has Docker and your data. Nothing here touches your live volume: everything runs on a COPY.

## 0. Get the code and the newest image
```
git pull                                   # in your nd-world checkout (main)
docker pull ghcr.io/8bit-boom/nd-world-gateway:latest
docker image inspect ghcr.io/8bit-boom/nd-world-gateway:latest --format '{{.Id}} {{.Created}}'   # note the id: it changes when CI publishes
```
CI must have finished for the commit you want (GitHub > Actions > "Build & Publish Docker Image": the `build-and-push-gateway` job).

## 1. Make a copy of your data
In the running app (GM): download `/admin/backup.zip`. Then:
```
mkdir -p /tmp/nd-copy && cd /tmp/nd-copy && unzip -o /path/to/backup.zip     # world.db, uploads/, ... 
```
(or `cp -a` your data folder while the app is stopped, so world.db is not torn).

## 2. Open EVERY entity page and list the ones that fail
```
python3 scripts/python_free_entity_crawl.py --db /tmp/nd-copy/world.db --email YOUR_GM_EMAIL --password 'YOUR_GM_PASSWORD' \
        --world-slug YOUR_WORLD_SLUG --image ghcr.io/8bit-boom/nd-world-gateway:latest
```
Output: how many entity pages answered 200, the failing ones (id, kind, name), and the gateway's own "DECLINE ... gave up at pages.rs:NNN" lines
(NNN is the exact step that refused the page). Paste the whole output to the session that maintains the gateway.

## 3. Same crawl for the other lists and a few pages by hand (browser, http://127.0.0.1:8991 while the crawl container is up, or start the image yourself)
```
docker run --rm -p 8991:8000 -v /tmp/nd-copy:/data -e ND_PYTHON_FREE=1 -e ND_DECLINE_TRACE=1 -e SECRET_KEY=x \
   -e GM_EMAIL=YOU -e GM_PASSWORD=PASS -e TZ=Europe/Budapest ghcr.io/8bit-boom/nd-world-gateway:latest
```
Open: an entity with a picture in its text, one with a rich-text note, the Characters list, a character, Images (send one to the players with a
description), a video (hover it: "Send…"), the second screen (`/display`). Every refusal prints a `DECLINE` line in that terminal: copy them.

## 4. What to report
- the crawl output of step 2,
- the DECLINE lines of step 3,
- the image id from step 0 (so we know which build you tested).
