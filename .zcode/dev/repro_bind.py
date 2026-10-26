"""Reproduce the tpl_fields_json binds case (run inside the container, cwd /work)."""
import os, json
os.environ['DB_PATH'] = '/work/tests/_data/dev/world.db'
os.environ['SECRET_KEY'] = 'test-secret-key-not-for-production'
from app import auth
auth._PBKDF2_ITERATIONS = 1000
from app.database import SessionLocal, init_db, engine
from app.models import Base, User, World, WorldMembership, PlayerCharacter

Base.metadata.drop_all(bind=engine)
init_db()
db = SessionLocal()
gm = User(email='gm@test.local', password_hash=auth.hash_password('gm-password-123'), display_name='GM', is_gm=True)
w = World(name='World A', slug='world-a')
db.add_all([gm, w]); db.commit()
db.refresh(gm); db.refresh(w)
pc = PlayerCharacter(world_id=w.id, owner_user_id=gm.id, name='Kira')
db.add(pc); db.commit()
db.refresh(pc)
pid = pc.id
db.close()

import httpx
d = httpx.Client(base_url='http://127.0.0.1:9031')
r = d.post('/login', data={'email': 'gm@test.local', 'password': 'gm-password-123'})
print('login:', r.status_code)
page = d.get(f'/characters/{pid}/edit')
import re
m = re.findall(r'data-fields="([^"]{0,120})', page.text)
print('python data-fields:', m[:2])
v = httpx.Client(base_url='http://127.0.0.1:9051')
for k, vv in d.cookies.items():
    v.cookies.set(k, vv)
r = v.get(f'/characters/{pid}/edit')
print('gateway status:', r.status_code, 'served-by:', r.headers.get('x-nd-served-by'))
m = re.findall(r'data-fields="([^"]{0,120})', r.text)
print('gateway data-fields:', m[:2])
