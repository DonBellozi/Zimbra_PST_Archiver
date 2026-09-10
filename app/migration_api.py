import csv
import io
import json
import secrets
import shutil
import uuid
from datetime import timezone
from pathlib import Path
from cryptography.fernet import Fernet
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse
from sqlalchemy import select, delete
from sqlalchemy.orm import Session
from .db import db_session
from .config import env
from .models import Setting, now
from .settings import get_all
from .security import encrypt
from .migration_models import Migration, MigrationMessage, MigrationArtifact
from .migration_runner import ACTIVE, storage_root, StorageMissing

MAX_CSV = 2 * 1024 * 1024


def parse_accounts(data):
    if len(data) > MAX_CSV: raise ValueError('CSV должен быть не больше 2 МБ')
    try: text = data.decode('utf-8-sig')
    except UnicodeDecodeError: raise ValueError('Сохраните CSV в UTF-8 (CSV UTF-8 в Excel)') from None
    first = text.splitlines()[0] if text else ''
    delimiter = ';' if first.count(';') > first.count(',') else ','
    reader = csv.DictReader(io.StringIO(text, newline=''), delimiter=delimiter, strict=True)
    if not reader.fieldnames or len(reader.fieldnames) != len(set(reader.fieldnames)):
        raise ValueError('Заголовки CSV отсутствуют или повторяются')
    if not {'email', 'password'} <= set(reader.fieldnames) or set(reader.fieldnames) - {'email', 'password', 'target_email'}:
        raise ValueError('Столбцы CSV: email,password,target_email (последний необязателен)')
    result, found = [], set()
    try:
        for index, row in enumerate(reader, 2):
            if None in row or any(value is None for value in row.values()):
                raise ValueError(f'Строка {index}: неверное количество столбцов')
            account = row['email'].strip()
            target = row.get('target_email', '').strip() or account
            password = row['password']
            if any(len(x) > 240 or x.count('@') != 1 or any(c.isspace() or ord(c) < 32 for c in x)
                   or not all(x.split('@')) for x in (account, target)):
                raise ValueError(f'Строка {index}: некорректный адрес')
            if not password or len(password) > 4096 or any(c in password for c in '\r\n\0'):
                raise ValueError(f'Строка {index}: пустой или неподдерживаемый пароль')
            if account.casefold() in found: raise ValueError(f'Строка {index}: повторный адрес')
            found.add(account.casefold())
            result.append((account, password, target))
            if len(result) > 500: raise ValueError('Не более 500 ящиков за загрузку')
    except csv.Error:
        raise ValueError('Некорректное экранирование CSV; пароли с разделителем заключайте в кавычки') from None
    if not result: raise ValueError('CSV не содержит учётных записей')
    return result


def public(job):
    keys = ('id', 'account', 'target', 'status', 'stage', 'error', 'total', 'downloaded',
            'total_bytes', 'downloaded_bytes', 'tgz_status', 'pst_status', 'want_tgz', 'want_pst')
    return {k: getattr(job, k) for k in keys} | {'has_secret': bool(job.secret)}


def make_router(guard, templates):
    router = APIRouter(dependencies=[Depends(guard)])

    def csrf(request, token):
        expected = request.session.get('migration_csrf', '')
        if not expected or not secrets.compare_digest(expected, str(token or '')):
            raise HTTPException(403, 'Обновите страницу и повторите действие')

    @router.get('/migrations')
    def page(request: Request, db: Session = Depends(db_session)):
        request.session.setdefault('migration_csrf', secrets.token_urlsafe(32))
        return templates.TemplateResponse(request, 'migrations.html', {
            'cfg': get_all(db), 'csrf': request.session['migration_csrf']})

    @router.post('/api/migrations/import')
    async def import_csv(request: Request, db: Session = Depends(db_session)):
        # Browser sends raw CSV; Starlette must not spool plaintext credentials
        # from a large multipart upload into temporary files.
        csrf(request, request.headers.get('x-csrf-token'))
        data = bytearray()
        async for chunk in request.stream():
            data.extend(chunk)
            if len(data) > MAX_CSV: raise HTTPException(413, 'CSV больше 2 МБ')
        try: rows = parse_accounts(bytes(data))
        except ValueError as exc: raise HTTPException(400, str(exc)) from None
        options = request.query_params
        if options.get('preview') == '1':
            return {'count': len(rows), 'accounts': [r[0] for r in rows]}
        host = options.get('host', '').strip()
        tls = options.get('tls', 'ssl')
        try: port = int(options.get('port', '993'))
        except ValueError: raise HTTPException(400, 'Некорректный порт')
        if not host or len(host) > 255 or any(c.isspace() or c in '/\\' for c in host) or not 1 <= port <= 65535 or tls not in ('ssl', 'starttls'):
            raise HTTPException(400, 'Укажите IMAP-сервер, порт и защищённое подключение')
        tgz, pst = options.get('tgz') == '1', options.get('pst') == '1'
        if not tgz and not pst: raise HTTPException(400, 'Выберите TGZ и/или PST')
        try: Fernet(env.app_secret_key.encode())
        except Exception: raise HTTPException(400, 'Для паролей требуется действующий APP_SECRET_KEY; тестовый ключ запрещён')
        try: batch = uuid.UUID(options.get('batch', '')).hex
        except ValueError: raise HTTPException(400, 'Обновите страницу перед загрузкой')
        if db.bind.dialect.name == 'sqlite': db.connection().exec_driver_sql('BEGIN IMMEDIATE')
        previous = list(db.scalars(select(Migration).where(Migration.batch == batch)))
        if previous: return {'count': len(previous), 'ids': [x.id for x in previous]}
        cfg = get_all(db)
        nas = options.get('storage', 'nas') == 'nas'
        if nas and cfg['nas_enabled'] != 'true': raise HTTPException(400, 'Сначала включите NAS в настройках')
        storage = cfg['nas_dir'] if nas else cfg['work_dir']
        if not Path(storage).is_absolute(): raise HTTPException(400, 'Каталог хранения должен быть абсолютным')
        # Prevent repeated active imports of the same mailbox in different CSVs.
        existing = {x.casefold() for x in db.scalars(select(Migration.account).where(
            Migration.host == host, Migration.status.not_in(['COMPLETED', 'CANCELLED', 'FAILED', 'EMPTY'])))}
        if any(account.casefold() in existing for account, _, _ in rows):
            raise HTTPException(409, 'В CSV есть ящик, который уже находится в очереди')
        jobs = []
        for account, password, target in rows:
            job = Migration(batch=batch, account=account, target=target, secret=encrypt(password),
                host=host, port=port, tls=tls, storage=storage, use_nas=nas, want_tgz=tgz, want_pst=pst,
                tgz_status='PENDING' if tgz else 'NOT_SELECTED', pst_status='PENDING' if pst else 'NOT_SELECTED')
            db.add(job); jobs.append(job)
        for key, value in {'imap_host': host, 'imap_port': str(port), 'imap_tls': tls}.items():
            setting = db.get(Setting, key) or Setting(key=key)
            setting.value = value; setting.encrypted = False; db.add(setting)
        db.commit()
        return {'count': len(jobs), 'ids': [j.id for j in jobs]}

    @router.get('/api/migrations')
    def listing(q: str = '', db: Session = Depends(db_session)):
        jobs = db.scalars(select(Migration).order_by(Migration.created_at.desc())).all()
        return [public(j) for j in jobs if q.casefold() in (j.account + ' ' + j.target + ' ' + j.status).casefold()]

    @router.post('/api/migrations/{job_id}/{action}')
    def action(job_id: str, action: str, request: Request, db: Session = Depends(db_session)):
        csrf(request, request.headers.get('x-csrf-token'))
        if db.bind.dialect.name == 'sqlite': db.connection().exec_driver_sql('BEGIN IMMEDIATE')
        job = db.get(Migration, job_id)
        if not job: raise HTTPException(404)
        if action == 'cancel':
            job.cancel = True
            if job.status not in ACTIVE: job.status = 'CANCELLED'
        elif job.status in ACTIVE:
            raise HTTPException(409, 'Сначала отмените задачу и дождитесь остановки')
        elif action == 'retry':
            if job.status == 'EXPIRED': raise HTTPException(400, 'Исходники удалены; создайте новый запуск')
            job.status = 'QUEUED'; job.error = None; job.cancel = False; job.next_try = None; job.retries = 0
        elif action == 'rescan':
            if not job.secret: raise HTTPException(400, 'Пароль удалён; создайте новый запуск')
            job.scanned = False; job.cancel = False; job.error = None; job.status = 'QUEUED'
            job.export_generation += 1; job.next_try = None; job.retries = 0
            job.tgz_path = job.pst_path = job.tgz_sha256 = job.pst_sha256 = None
            job.tgz_status = 'PENDING' if job.want_tgz else 'NOT_SELECTED'
            job.pst_status = 'PENDING' if job.want_pst else 'NOT_SELECTED'
        elif action == 'forget-password':
            job.secret = ''
        elif action == 'delete-files':
            try: base = storage_root(job)
            except (StorageMissing, OSError): raise HTTPException(409, 'Хранилище недоступно; удаление не выполнено')
            # Exact task-owned directory, never a workspace/NAS root.
            if base.name != job.id or base.parent.name != 'imap-migrations': raise HTTPException(400)
            shutil.rmtree(base)
            local = Path(get_all(db)['work_dir']) / 'imap-migrations' / job.id
            if local.resolve() != base.resolve() and local.is_dir(): shutil.rmtree(local)
            db.execute(delete(MigrationMessage).where(MigrationMessage.migration_id == job.id))
            db.execute(delete(MigrationArtifact).where(MigrationArtifact.migration_id == job.id))
            job.secret = ''; job.status = 'EXPIRED'; job.stage = 'Файлы удалены пользователем; история сохранена'
            job.pst_path = job.tgz_path = None; job.pst_status = job.tgz_status = 'EXPIRED'
        else: raise HTTPException(400, 'Неизвестное действие')
        db.commit(); return {'ok': True}

    @router.get('/migrations/{job_id}/download/{kind}')
    def download(job_id: str, kind: str, db: Session = Depends(db_session)):
        job = db.get(Migration, job_id)
        if not job: raise HTTPException(404)
        if kind == 'tgz': path = job.tgz_path if job.want_tgz and job.tgz_status == 'READY' else None
        elif kind == 'pst':
            expired = job.pst_expires and job.pst_expires.replace(tzinfo=timezone.utc) <= now()
            path = job.pst_path if job.pst_status == 'READY' and not expired else None
        elif kind == 'report':
            # Built from DB for failed tasks too; never serialise credentials.
            from fastapi.responses import JSONResponse
            return JSONResponse(public(job) | {'folders': list(db.scalars(select(MigrationMessage.folder).where(
                MigrationMessage.migration_id == job.id).distinct())),
                'tgz_sha256': job.tgz_sha256, 'pst_sha256': job.pst_sha256})
        else: raise HTTPException(404)
        if not path or not Path(path).is_file(): raise HTTPException(404, 'Файл недоступен')
        return FileResponse(path, filename=Path(path).name, headers={'Cache-Control': 'no-store'})
    return router
