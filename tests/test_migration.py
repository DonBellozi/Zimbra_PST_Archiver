import hashlib
import io
import json
import re
import socketserver
import tarfile
import threading
from contextlib import contextmanager
from datetime import datetime, timezone, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from imapclient import IMAPClient
from imapclient.imap_utf7 import encode
from sqlalchemy import create_engine, select, func
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.db import Base, db_session
from app.models import Job, Setting, now
from app.migration_models import Migration, MigrationMessage, MigrationArtifact
from app.migration_api import parse_accounts
from app import migration_runner as runner
from app import imap_source
from app.security import encrypt, decrypt


@pytest.fixture
def db(tmp_path):
    engine = create_engine('sqlite://', connect_args={'check_same_thread': False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    with Session(engine, expire_on_commit=False) as session:
        for key, value in {'work_dir': str(tmp_path/'work'), 'nas_dir': str(tmp_path/'nas'),
                           'min_free_gb': '0', 'nas_enabled': 'true'}.items():
            session.add(Setting(key=key, value=value, encrypted=False))
        (tmp_path/'work').mkdir(); (tmp_path/'nas').mkdir()
        session.commit()
        yield session
    engine.dispose()


def new_job(db, tmp_path, **kwargs):
    job = Migration(batch='test', account='user@example.com', target='user@example.com',
        secret=encrypt('password "with, commas; and space'), host='localhost', port=993, tls='ssl',
        storage=str(tmp_path/'nas'), use_nas=False, want_tgz=True, want_pst=False, **kwargs)
    db.add(job); db.commit()
    return job


@pytest.fixture
def imap_server():
    # Real loopback wire protocol, including modified UTF-7, EXAMINE and ranged
    # FETCH. Plain transport is confined to this test fixture's injected factory.
    class Server(socketserver.ThreadingTCPServer):
        allow_reuse_address = True
        daemon_threads = True
    state = SimpleNamespace(validity=7, fail=False, calls=[], reads=[], messages={
        'INBOX': {11: b'Subject: one\r\nMessage-ID: <one@invalid>\r\n\r\n' + b'a'*1100000},
        'INBOX/Камеры': {21: b'Subject: two\r\nMessage-ID: <two@invalid>\r\n\r\ntwo'}})
    class Handler(socketserver.StreamRequestHandler):
        def send(self, value): self.wfile.write(value + b'\r\n'); self.wfile.flush()
        def handle(self):
            self.send(b'* OK local test IMAP')
            folder = 'INBOX'
            while line := self.rfile.readline():
                tag, command = line.rstrip(b'\r\n').split(b' ', 1)
                state.calls.append(command.split(b' ', 1)[0])
                if command == b'CAPABILITY': self.send(b'* CAPABILITY IMAP4rev1')
                elif command.startswith(b'LOGIN '): pass
                elif command.startswith(b'LIST '):
                    for name in state.messages:
                        self.send(b'* LIST () "/" "' + encode(name) + b'"')
                elif command.startswith(b'EXAMINE '):
                    wire_name = command.split(b' ', 1)[1].strip(b'"')
                    folder = next(n for n in state.messages if encode(n) == wire_name)
                    self.send(b'* FLAGS (\\Seen)')
                    self.send(f'* {len(state.messages[folder])} EXISTS'.encode())
                    self.send(f'* OK [UIDVALIDITY {state.validity}] valid'.encode())
                elif command.startswith(b'UID SEARCH '):
                    self.send(b'* SEARCH ' + b' '.join(str(u).encode() for u in state.messages[folder]))
                elif command.startswith(b'UID FETCH '):
                    requested = command.split(b' ')[2]
                    uids = [int(x) for x in requested.split(b',')]
                    partial = re.search(br'BODY.PEEK\[\]<([0-9]+)\.([0-9]+)>', command)
                    for seq, uid in enumerate(uids, 1):
                        data = state.messages[folder][uid]
                        if partial:
                            start, count = map(int, partial.groups())
                            if state.fail and uid == 21:
                                state.fail = False; return
                            state.reads.append((folder, uid, start))
                            chunk = data[start:start+count]
                            self.send(f'* {seq} FETCH (UID {uid} BODY[]<{start}> {{{len(chunk)}}}'.encode())
                            self.wfile.write(chunk); self.send(b')')
                        else:
                            flags = '\\Seen' if uid == 11 else ''
                            self.send(f'* {seq} FETCH (UID {uid} RFC822.SIZE {len(data)} INTERNALDATE "02-Jan-2021 12:00:00 +0300" FLAGS ({flags}))'.encode())
                elif command == b'LOGOUT':
                    self.send(b'* BYE goodbye'); self.send(tag+b' OK logout'); return
                else:
                    self.send(tag+b' BAD unsupported'); continue
                self.send(tag + b' OK completed')
    with Server(('127.0.0.1', 0), Handler) as server:
        thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
        @contextmanager
        def factory(job, password):
            with IMAPClient('127.0.0.1', port=server.server_address[1], ssl=False, timeout=3) as client:
                client.login(job.account, password); client.normalise_times=False
                yield client
        yield state, factory
        server.shutdown(); thread.join()


def test_csv_quotes_bom_duplicates_no_password_in_errors():
    data = '\ufeffemail;password;target_email\r\nu@old.ru;"a;b,""c"" ";u@new.ru\r\n'.encode()
    assert parse_accounts(data) == [('u@old.ru', 'a;b,"c" ', 'u@new.ru')]
    with pytest.raises(ValueError, match='повторный'):
        parse_accounts(b'email,password\nu@a.ru,secret\nu@a.ru,other\n')
    with pytest.raises(ValueError) as err:
        parse_accounts(b'email,password\nu@a.ru,top,secret\n')
    assert 'secret' not in str(err.value)
    rows = parse_accounts(('email,password\n' + ''.join(f'user{i}@x.ru,pass\n' for i in range(239))).encode())
    assert len(rows) == 239


def test_wire_download_resume_tgz_and_rescan(db, tmp_path, imap_server):
    state, factory = imap_server
    job = new_job(db, tmp_path)
    state.fail = True
    with pytest.raises(IMAPClient.AbortError): runner.process(db, job, factory)
    first_reads = len([r for r in state.reads if r[1] == 11])
    assert first_reads == 2  # 1 MiB chunks, not the entire message in memory
    runner.process(db, job, factory)
    assert job.status == 'COMPLETED' and job.downloaded == job.total == 2
    assert len([r for r in state.reads if r[1] == 11]) == first_reads
    assert Path(job.tgz_path).name.endswith('-user@example.com.tgz')
    with tarfile.open(job.tgz_path) as archive:
        members = archive.getmembers()
        assert {m.name.rsplit('/', 1)[0] for m in members} == {'Inbox', 'Inbox/Камеры'}
        blobs = [archive.extractfile(m).read() for m in members]
        assert set(blobs) == {v for box in state.messages.values() for v in box.values()}
        assert sorted(bool(m.mode & 0o200) for m in members) == [False, True]
        assert all(m.mtime == 1609578000 for m in members)
    assert not {b'STORE', b'EXPUNGE', b'SELECT'} & set(state.calls)
    before = len(state.reads)
    old_tgz = Path(job.tgz_path)
    state.messages['INBOX'][12] = b'Subject: new\r\n\r\nnew'
    job.scanned=False; job.export_generation+=1; job.tgz_sha256=None; db.commit()
    runner.process(db, job, factory)
    assert job.total == 3 and len(state.reads) == before+1
    assert old_tgz.exists() and Path(job.tgz_path) != old_tgz


def test_uidvalidity_change_and_corrupt_local_file(db, tmp_path, imap_server):
    state, factory = imap_server
    job = new_job(db, tmp_path)
    runner.process(db, job, factory)
    row = db.scalar(select(MigrationMessage).where(MigrationMessage.uid == 21))
    path = runner.storage_root(job)/'source'/row.relative_path
    raw = path.read_bytes(); path.write_bytes(b'X'*len(raw))
    with pytest.raises(imap_source.SourceChanged): runner.process(db, job, factory)
    assert row.sha256 is None
    state.validity = 8
    with pytest.raises(imap_source.SourceChanged): runner.process(db, job, factory)
    job.scanned=False; db.commit()
    runner.process(db, job, factory)
    assert job.total == 2
    assert db.scalar(select(func.count()).select_from(MigrationMessage)) == 4


def test_partial_pst_failure_then_retry_without_imap(db, tmp_path, imap_server, monkeypatch):
    state, factory = imap_server
    job = new_job(db, tmp_path); job.want_pst=True
    def failed(*args): raise RuntimeError('failure')
    monkeypatch.setattr(runner, 'convert_pst', failed)
    with pytest.raises(RuntimeError): runner.process(db, job, factory)
    assert job.tgz_status == 'READY' and Path(job.tgz_path).is_file() and job.pst_status == 'FAILED'
    def fake_converter(command, tgz, output, *args): output.write_bytes(b'!BDN'+b'0'*1024)
    monkeypatch.setattr(runner, 'convert_pst', fake_converter)
    def offline(*args): raise AssertionError('must not connect')
    runner.process(db, job, offline)
    assert job.pst_status == 'READY'
    artifact = db.get(MigrationArtifact, job.pst_path)
    artifact.expires_at = now()-timedelta(seconds=1); db.commit()
    runner.cleanup(db)
    assert job.pst_status=='EXPIRED' and Path(job.tgz_path).exists()


def test_space_wait_and_cancel_recovery(db, tmp_path, imap_server, monkeypatch):
    state, factory = imap_server
    job = new_job(db, tmp_path)
    monkeypatch.setattr(imap_source, 'connect', factory)
    # Default argument of process is bound at definition; inject wrapper for tick.
    original = runner.process
    monkeypatch.setattr(runner, 'process', lambda db, job: original(db, job, factory))
    monkeypatch.setattr(runner.shutil, 'disk_usage', lambda path: SimpleNamespace(free=0))
    runner.tick(db)
    assert job.status=='WAITING_FOR_SPACE' and not state.reads
    job.status='DOWNLOADING'; db.commit(); runner.recover(db)
    assert job.status=='QUEUED'
    job.cancel=True; job.next_try=None; db.commit()
    runner.tick(db)
    assert job.status=='CANCELLED'


def test_ready_pst_survives_nas_space_wait(db, tmp_path, imap_server, monkeypatch):
    _, factory = imap_server
    job = new_job(db, tmp_path); job.want_pst=True
    conversions = []
    def converter(command, tgz, output, *args):
        conversions.append(1); output.write_bytes(b'!BDN'+b'0'*1024)
    monkeypatch.setattr(runner, 'convert_pst', converter)
    real_space = runner.free_space
    def fail_final_copy(path, amount, reserve):
        if amount == 1028: raise runner.NeedSpace()
        real_space(path, amount, reserve)
    monkeypatch.setattr(runner, 'free_space', fail_final_copy)
    with pytest.raises(runner.NeedSpace): runner.process(db, job, factory)
    assert job.tgz_status=='READY'
    monkeypatch.setattr(runner, 'free_space', real_space)
    runner.process(db, job, factory)
    assert job.status=='COMPLETED' and len(conversions)==1


def test_authentication_failure_isolated_from_next_job(db, tmp_path, imap_server, monkeypatch):
    from contextlib import contextmanager
    _, factory = imap_server
    job = new_job(db, tmp_path)
    @contextmanager
    def deny(job, password):
        raise imap_source.AuthenticationError('Вход отклонён')
        yield
    original=runner.process
    monkeypatch.setattr(runner, 'process', lambda db, job: original(db, job, deny))
    runner.tick(db)
    assert job.status=='FAILED' and 'password' not in (job.error or '')
    second = Migration(batch='next', account=job.account, target=job.target, secret=job.secret,
        host=job.host, port=job.port, tls=job.tls, storage=job.storage, use_nas=False,
        want_tgz=True, want_pst=False)
    db.add(second); db.commit()
    monkeypatch.setattr(runner, 'process', lambda db, job: original(db, job, factory))
    runner.tick(db)
    assert second.status=='COMPLETED' and job.status=='FAILED'


def test_api_csv_239_idempotency_secret_and_additive_schema(db, tmp_path):
    from app.main import app, guard
    from app.config import env
    # No lifespan here: table creation is explicitly done in the fixture.
    app.dependency_overrides[guard] = lambda: True
    app.dependency_overrides[db_session] = lambda: db
    try:
        client = TestClient(app)
        page = client.get('/migrations')
        token = re.search(r'data-csrf="([^"]+)"', page.text).group(1)
        csvdata = ('email,password\n'+''.join(f'user{i}@x.ru,"s,ecret"\n' for i in range(239))).encode()
        url='/api/migrations/import?host=imap.example.com&port=993&tls=ssl&storage=nas&tgz=1&pst=0&batch=12345678-1234-4234-8234-123456789abc'
        assert client.post(url, content=csvdata).status_code == 403
        headers={'X-CSRF-Token':token}
        preview=client.post(url+'&preview=1',content=csvdata,headers=headers)
        assert preview.json()['count']==239
        assert db.scalar(select(func.count()).select_from(Migration))==0
        response=client.post(url,content=csvdata,headers=headers)
        assert response.status_code==200, response.text
        assert client.post(url,content=csvdata,headers=headers).json()==response.json()
        stored=db.scalar(select(Migration))
        assert stored.secret != 's,ecret' and decrypt(stored.secret)=='s,ecret'
        listing=client.get('/api/migrations')
        assert len(listing.json())==239 and 's,ecret' not in listing.text
        assert 'secret"' not in listing.text.replace('has_secret"','')
        Base.metadata.create_all(db.bind)
        assert 'jobs' in Base.metadata.tables
    finally: app.dependency_overrides.clear()


def test_additive_sql_matches_models_and_keeps_ssh_history(tmp_path):
    engine = create_engine('sqlite:///' + str(tmp_path/'upgrade.sqlite'))
    from app.models import User, JobEvent
    Base.metadata.create_all(engine, tables=[User.__table__, Setting.__table__, Job.__table__, JobEvent.__table__])
    with Session(engine) as db:
        db.add(Job(account='existing@example.com')); db.commit()
    sql = Path('migrations/002_imap.sql').read_text()
    raw = engine.raw_connection()
    raw.executescript(sql); raw.executescript(sql); raw.close()
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        assert db.scalar(select(Job.account)) == 'existing@example.com'
        assert list(db.scalars(select(Migration))) == []
    engine.dispose()
