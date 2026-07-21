# Deploying nd-world for your table

This walks through running nd-world on your own machine/server (Part A), then
making it reachable from the internet so you can invite players (Part B).

If you just want the short version: `bash scripts/setup.sh` handles Part A —
installing Docker, creating `.env`, setting up your GM login, and starting the
stack. This doc explains what that does and covers Part B in detail.

---

## Part A — Run it locally

1. Install [Docker](https://docs.docker.com/get-docker/) (with the Compose
   plugin — recent installs include it automatically).
2. Clone this repo and run the setup script:
   ```bash
   git clone https://github.com/8bit-boom/nd-world.git
   cd nd-world
   bash scripts/setup.sh
   ```
3. The script creates `.env` (a random `SECRET_KEY`, and it'll ask for the
   email/password you want to use as the GM), then builds and starts
   nd-world, SwarmUI (image generation), and Ollama (AI chat).
4. Once it prints a URL, open it and log in with the GM email/password you
   set. At this point the app works for anyone on your home WiFi/network, but
   not yet from the internet — that's Part B.

**Manual setup** (if you'd rather not use the script): copy `.env.example` to
`.env`, fill in `SECRET_KEY` (`openssl rand -hex 32`) and `GM_EMAIL`/
`GM_PASSWORD`, then run `docker compose up -d --build`.

---

## Part B — Make it reachable from the internet

You have two options. **Option 1 (Cloudflare Tunnel) is strongly recommended
if you've never done this before** — it needs no router changes and no
technical networking knowledge. Option 2 is more advanced.

### Option 1: Cloudflare Tunnel (recommended)

This creates a secure, private connection from your server out to Cloudflare,
who then gives you a public web address. Nobody needs to open any doors into
your home network for this to work.

**Step 1 — Try it instantly, no account needed (good for a first test):**

In your server's terminal (replace the port if you changed `APP_PORT`):
```bash
docker run --rm cloudflare/cloudflared:latest tunnel --url http://localhost:8080
```
After a few seconds you'll see a line containing a web address ending in
`.trycloudflare.com` — that's a public link to your app, live right now.
Share it with a player and try logging in (or joining, if you've sent them an
invite link). This link stops working as soon as you close/stop this command,
and a new random link is generated each time — it's only meant for a quick
test, not permanent use.

Press `Ctrl+C` in the terminal to stop it when you're done testing.

**Step 2 — Set up a permanent link (once you're happy with the test):**

This needs a free Cloudflare account and a domain name (a domain costs a
small yearly fee from any registrar — e.g. Namecheap, Cloudflare Registrar —
if you don't already have one; a subdomain like `campaign.yourdomain.com`
works too).

1. Sign up at [cloudflare.com](https://cloudflare.com) (free tier is fine)
   and add your domain to your account, following Cloudflare's own setup
   wizard (it will ask you to change your domain's "nameservers" at your
   registrar — follow their instructions).
2. In the Cloudflare dashboard, go to **Zero Trust → Networks → Tunnels** and
   click **Create a tunnel**. Name it something like `nd-world`.
3. Cloudflare will give you a command starting with
   `cloudflared service install ...` containing a long token. Copy the whole
   command and run it on your server.
4. Back in the Cloudflare dashboard, add a **Public Hostname** for the
   tunnel: pick a subdomain (e.g. `world`), your domain, and set the service
   to `http://localhost:8080` (or `http://<your-server-ip>:8080`).
5. Save. Within a minute or two, `https://world.yourdomain.com` (or whatever
   you chose) will show the nd-world login page — permanently, with a free
   HTTPS padlock included.

**Once this is live**, edit `.env` and set `COOKIE_SECURE=true`, then run
`docker compose up -d` again to apply it. (This matters — without it, logins
won't stay signed in when accessed over `https://`.)

### Option 2: Port forwarding + reverse proxy (more advanced)

Only do this if Option 1 doesn't work for your situation. This method opens a
door directly into your home network, which needs to be configured carefully.

1. Install a reverse proxy (e.g. **Nginx Proxy Manager** or **Caddy**) to get
   a free HTTPS certificate for a domain name and forward traffic to
   `http://<your-server-ip>:8080`.
2. Point that domain's DNS at your home internet connection's public IP
   address (your internet provider's website or a service like No-IP/DuckDNS
   can tell you this address, since most home internet IPs change over time).
3. Forward the port your reverse proxy listens on (usually 443) from your
   router to the machine running it.
4. Once traffic is flowing over HTTPS, set `COOKIE_SECURE=true` in `.env` and
   restart (`docker compose up -d`), same as Option 1.

---

## Inviting players

Once nd-world is reachable (locally or over the internet):

1. Log in as the GM.
2. Open the world switcher → **⚙ Manage worlds** → **Edit** on the world you
   want to invite someone to.
3. Under **Invite Links**, click **+ Create Invite Link** (optionally set an
   expiry or a max number of uses — leave blank for an unlimited, permanent
   link).
4. Copy the `/join/...` link it creates and send it to your player. Opening
   it lets them create their own account (or log in, if they already have
   one) and joins them to that world as a player.

Players only see worlds they've been invited to, and only the lore you haven't
marked "Hide from players" — see the visibility checkbox on any entity's edit
page. They manage one character each (via the character creation wizard, or
directly on their sheet), and — if you leave **Players can see each other's
characters** checked on the world's Edit page — can see the rest of the party
read-only.

Manage existing players (remove access) and revoke invite links from the same
world Edit page.
