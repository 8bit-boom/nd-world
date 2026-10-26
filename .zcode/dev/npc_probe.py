"""Replicate the npc-candidates test flow exactly (run inside the container, /work cwd)."""
import os, json
os.environ['ND_TEST_DATA_DIR'] = '/work/tests/_data/dev'
os.environ['DB_PATH'] = '/work/tests/_data/dev/world.db'
os.environ['SECRET_KEY'] = 'test-secret-key-not-for-production'
os.environ['ND_GOLDEN_DIRECT_URL'] = 'http://127.0.0.1:9031'
os.environ['ND_TEST_BASE_URL'] = 'http://127.0.0.1:9051'
import httpx
from app.database import SessionLocal, init_db
from app.models import Base
from app.database import engine
Base.metadata.drop_all(bind=engine)
from app.models import Entity, GameSession, PlayerCharacter

DIRECT, BASE = 'http://127.0.0.1:9031', 'http://127.0.0.1:9051'
httpx.post(DIRECT + '/__test__/reset', timeout=60).raise_for_status()
init_db()
db = SessionLocal()
from tests.conftest import GM_PASSWORD, PLAYER_PASSWORD, _GM_PASSWORD_HASH, _PLAYER_PASSWORD_HASH
from app.models import User, World, WorldMembership
gm = User(email='gm@test.local', password_hash=_GM_PASSWORD_HASH, display_name='GM', is_gm=True)
wa, wb = World(name='World A', slug='world-a'), World(name='World B', slug='world-b')
db.add_all([gm, wa, wb]); db.commit()
db.refresh(gm); db.refresh(wa); db.refresh(wb)
pa = User(email='player-a@test.local', password_hash=_PLAYER_PASSWORD_HASH, display_name='Player A', is_gm=False)
db.add(pa); db.commit(); db.refresh(pa)
db.add_all([WorldMembership(world_id=wa.id, user_id=pa.id), WorldMembership(world_id=wb.id, user_id=pa.id)]); db.commit()
db.add_all([
    Entity(world_id=wa.id, kind='character', name='Vosk', folder='Dockside'),
    Entity(world_id=wa.id, kind='character', name='Kira', subtype='PC'),
    PlayerCharacter(world_id=wa.id, owner_user_id=pa.id, name='Zed'),
])
gs_a = GameSession(world_id=wa.id, title='Docks', session_num=1)
gs_b = GameSession(world_id=wb.id, title='There', session_num=1)
db.add_all([gs_a, gs_b]); db.commit()
ids = (gs_a.id, gs_b.id, 999999); wslug = wa.slug
db.close()

for email, pw in (('gm@test.local', GM_PASSWORD), ('player-a@test.local', PLAYER_PASSWORD)):
    d = httpx.Client(base_url=DIRECT, timeout=60)
    r = d.post('/login', data={'email': email, 'password': pw, 'next': '/'}, follow_redirects=False)
    assert r.status_code == 303, (email, r.status_code)
    d.cookies.set('active_world', wslug)
    d.get('/')
    for i in ids:
        url = f'/sessions/{i}/npc-candidates'
        a = d.get(url)
        v = httpx.Client(base_url=BASE, timeout=60)
        for k, vv in d.cookies.items():
            v.cookies.set(k, vv)
        b = v.get(url)
        try:
            ja = (a.status_code, a.json())
        except Exception as e:
            ja = ('UNPARSEABLE', a.status_code, a.text[:150])
        try:
            jb = (b.status_code, b.json())
        except Exception as e:
            jb = ('UNPARSEABLE', b.status_code, b.text[:150])
        print(email, i, ja, jb, 'GW' if b.headers.get('x-nd-served-by') == 'gateway' else 'py')
