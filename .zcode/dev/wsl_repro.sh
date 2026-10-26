#!/bin/bash
# Seed a GM + world + template, run the custom-PC case through the gateway, print the log's DBG lines.
export PATH=/root/.cargo/bin:$PATH
cd /mnt/f/GLM/nd-world
bash .zcode/dev/wsl_pair.sh >/dev/null || exit 1
/root/venv/bin/python - <<'PYEOF'
import httpx, json, os
os.environ['DB_PATH'] = '/root/devdata/world.db'
os.environ['SECRET_KEY'] = 'test-secret-key-not-for-production'
from app.database import SessionLocal
from app.models import User, World, WorldMembership, SheetTemplate
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
if not db.query(SheetTemplate).filter_by(slug='mek-imp').first():
    db.add(SheetTemplate(world_id=w.id, name='Mek', slug='mek-imp', sheet_mode='custom', fields_json=json.dumps([{'id': 'skill', 'label': 'Skill', 'type': 'text'}])))
    db.commit()
db.close()
d = httpx.Client(base_url='http://127.0.0.1:9031')
d.post('/login', data={'email': 'gm@test.local', 'password': 'gm-password-123'})
d.cookies.set('active_world', 'world-a')
v = httpx.Client(base_url='http://127.0.0.1:9051')
for k, vv in d.cookies.items():
    v.cookies.set(k, vv)
payload = {'json_text': json.dumps({'name': 'Imp Custom Z', 'sheet_template_id': 'mek-imp', 'skill': 'Pilot'}), 'kind': 'player_character', 'params': {}}
r = v.post('/api/import/execute', json=payload)
print('gw:', r.status_code, r.headers.get('x-nd-served-by'), r.text[:80])
PYEOF
sleep 0.3
grep -a DBG /root/devdata/gateway.log | head -6
