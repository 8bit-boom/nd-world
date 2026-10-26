#!/bin/bash
# Reproduce the /kind/character body diff between the two servers.
export PATH=/root/.cargo/bin:$PATH
cd /mnt/f/GLM/nd-world
bash .zcode/dev/wsl_pair.sh >/dev/null || exit 1
/root/venv/bin/python - <<'PYEOF'
import httpx, json, os, difflib
os.environ['DB_PATH'] = '/root/devdata/world.db'
os.environ['SECRET_KEY'] = 'test-secret-key-not-for-production'
from app.database import SessionLocal
from app.models import User, World, WorldMembership, Entity
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
else:
    w = db.query(World).filter_by(slug='world-a').first()
db.add_all([Entity(world_id=w.id, kind='character', name='Vosk', subtype='Fixer'),
            Entity(world_id=w.id, kind='character', name='Kira', subtype='PC')])
db.commit()
db.close()
d = httpx.Client(base_url='http://127.0.0.1:9031')
d.post('/login', data={'email': 'gm@test.local', 'password': 'gm-password-123'})
d.cookies.set('active_world', 'world-a')
v = httpx.Client(base_url='http://127.0.0.1:9051')
for k, vv in d.cookies.items():
    v.cookies.set(k, vv)
a = d.get('/kind/character')
b = v.get('/kind/character')
print('py:', a.status_code, 'gw:', b.status_code, b.headers.get('x-nd-served-by'))
if a.status_code == b.status_code == 200 and a.text != b.text:
    for line in list(difflib.unified_diff(a.text.splitlines(), b.text.splitlines(), 'py', 'gw', n=1, lineterm=''))[:16]:
        print(line)
PYEOF
