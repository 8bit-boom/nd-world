#!/bin/bash
# Seed, run the world-import repro through the gateway, print DBG lines.
export PATH=/root/.cargo/bin:$PATH
cd /mnt/f/GLM/nd-world
bash .zcode/dev/wsl_pair.sh >/dev/null || exit 1
/root/venv/bin/python - <<'PYEOF'
import httpx, json, os, base64
os.environ['DB_PATH'] = '/root/devdata/world.db'
os.environ['SECRET_KEY'] = 'test-secret-key-not-for-production'
from app.database import SessionLocal
from app.models import User, World, WorldMembership
from app import auth
auth._PBKDF2_ITERATIONS = 1000
db = SessionLocal()
gm = db.query(User).filter_by(email='gm@test.local').first()
if not gm:
    gm = User(email='gm@test.local', password_hash=auth.hash_password('gm-password-123'), is_gm=True)
    w = World(name='World A', slug='world-a')
    db.add_all([gm, w]); db.commit()
    db.refresh(gm); db.refresh(w)
    db.add(WorldMembership(world_id=w.id, user_id=gm.id)); db.commit()
db.close()
from pathlib import Path
Path('/root/devdata/uploads').mkdir(exist_ok=True)
png = base64.b64encode(b'\x89PNG-restored').decode()
export = {'entities': [{'kind': 'item', 'name': 'Dbg Sword', 'image_data': f'data:image/png;base64,{png}'}]}
d = httpx.Client(base_url='http://127.0.0.1:9031')
d.post('/login', data={'email': 'gm@test.local', 'password': 'gm-password-123'})
d.cookies.set('active_world', 'world-a')
v = httpx.Client(base_url='http://127.0.0.1:9051')
for k, vv in d.cookies.items():
    v.cookies.set(k, vv)
r = v.post('/worlds/2/import', files={'file': ('export.json', json.dumps(export).encode(), 'application/json')}, follow_redirects=False)
print('gw:', r.status_code, r.headers.get('x-nd-served-by'), r.headers.get('location'))
PYEOF
sleep 0.3
grep -a DBG /root/devdata/gateway.log | head -4
