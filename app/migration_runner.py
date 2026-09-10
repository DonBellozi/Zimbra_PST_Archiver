import errno
import hashlib
import json
import os
import shlex
import signal
import shutil
import ssl
import subprocess
import tempfile
import time
from datetime import timedelta
from pathlib import Path
from sqlalchemy import select, or_, update
from imapclient import IMAPClient
from .migration_models import Migration, MigrationMessage, MigrationArtifact
from .models import now
from .security import decrypt
from .settings import get_all
from . import imap_source
from .zimbra_tgz import build_tgz
from .converter import verify_pst

ACTIVE = {'SCANNING', 'DOWNLOADING', 'BUILDING_TGZ', 'CONVERTING', 'VERIFYING'}
READY = {'QUEUED', 'WAITING_FOR_SPACE', 'WAITING_FOR_SOURCE', 'WAITING_FOR_STORAGE'}
GB = 1024**3

class Cancelled(Exception): pass
class NeedSpace(Exception): pass
class StorageMissing(Exception): pass


def storage_root(job):
    root = Path(job.storage)
    if not root.is_dir(): raise StorageMissing()
    # Refuse to silently start saving beneath a vanished Linux mount.
    if job.use_nas and os.name == 'posix':
        resolved = str(root.resolve())
        mounts = Path('/proc/self/mountinfo').read_text().splitlines()
        mountpaths = [line.split()[4].replace('\\040', ' ') for line in mounts]
        if not any(p != '/' and (resolved == p or resolved.startswith(p + '/')) for p in mountpaths):
            raise StorageMissing()
    path = root / 'imap-migrations' / job.id
    path.mkdir(parents=True, exist_ok=True)
    return path


def free_space(path, amount, reserve):
    if shutil.disk_usage(path).free < amount + reserve: raise NeedSpace()


def sha256(path, checkpoint=lambda _: None):
    digest = hashlib.sha256()
    with Path(path).open('rb') as stream:
        while chunk := stream.read(1024*1024):
            checkpoint('Проверка файла')
            digest.update(chunk)
    return digest.hexdigest()


def message_query(job):
    return select(MigrationMessage).where(MigrationMessage.migration_id == job.id,
        MigrationMessage.generation == job.scan_generation).order_by(MigrationMessage.folder_key, MigrationMessage.uid)


def atomic_json(path, content):
    partial = path.with_suffix(path.suffix + '.part')
    with partial.open('w', encoding='utf-8') as stream:
        json.dump(content, stream, ensure_ascii=False, indent=2)
        stream.flush(); os.fsync(stream.fileno())
    os.replace(partial, path)


def publish(source, destination, checkpoint):
    partial = destination.with_suffix(destination.suffix + '.part')
    expected = sha256(source, checkpoint)
    with source.open('rb') as inp, partial.open('wb') as out:
        while chunk := inp.read(1024*1024):
            checkpoint('Копирование результата')
            out.write(chunk)
        out.flush(); os.fsync(out.fileno())
    if sha256(partial, checkpoint) != expected:
        raise OSError('Result checksum mismatch')
    os.replace(partial, destination)
    return expected


def convert_pst(command, tgz, output, account, checkpoint, temp_root):
    if not command: raise RuntimeError('PST converter is not configured')
    env = os.environ.copy()
    env.update(PST_STORE_NAME=account, TMPDIR=str(temp_root), TEMP=str(temp_root), TMP=str(temp_root))
    log = temp_root / 'converter.log'
    with log.open('wb') as stream:
        process = subprocess.Popen(shlex.split(command) + [str(tgz), str(output)], env=env,
            stdout=stream, stderr=subprocess.STDOUT, start_new_session=os.name == 'posix')
        try:
            deadline = time.monotonic() + 86400
            while process.poll() is None:
                checkpoint('Создание PST')
                if time.monotonic() > deadline: raise TimeoutError()
                time.sleep(1)
            if process.returncode:
                # Do not echo subjects, filenames or server data into UI logs.
                raise RuntimeError('PST converter failed')
        finally:
            if process.poll() is None:
                if os.name == 'posix': os.killpg(process.pid, signal.SIGKILL)
                else: process.kill()
                process.wait()
    verify_pst(output)


def process(db, job, connector=imap_source.connect):
    cfg = get_all(db, secrets=True)
    reserve = int(float(cfg['min_free_gb']) * GB)
    base = storage_root(job)
    sources = base / 'source'
    sources.mkdir(exist_ok=True)
    local = Path(cfg['work_dir']) / 'imap-migrations' / job.id
    local.mkdir(parents=True, exist_ok=True)
    last_commit = [0.0]

    def checkpoint(stage):
        db.refresh(job, attribute_names=['cancel'])
        if job.cancel: raise Cancelled()
        if time.monotonic() - last_commit[0] > 2:
            free_space(base, 0, reserve)
            if job.status == 'CONVERTING': free_space(local, 0, reserve)
            job.stage = stage; db.commit(); last_commit[0] = time.monotonic()

    def state(status, stage):
        checkpoint(stage)
        job.status = status; job.stage = stage; db.commit()

    # A successful completed scan and saved messages can be packaged offline.
    missing = db.scalar(message_query(job).where(MigrationMessage.sha256.is_(None)).limit(1))
    if not job.scanned or missing is not None:
        if not job.secret: raise imap_source.AuthenticationError('Пароль удалён. Создайте новый запуск с CSV.')
        state('SCANNING', 'Подключение к IMAP')
        with connector(job, decrypt(job.secret)) as client:
            if not job.scanned:
                if job.tgz_sha256:
                    job.export_generation += 1
                    job.tgz_sha256 = job.pst_sha256 = job.tgz_path = job.pst_path = None
                    job.tgz_status = 'PENDING' if job.want_tgz else 'NOT_SELECTED'
                    job.pst_status = 'PENDING' if job.want_pst else 'NOT_SELECTED'
                summary = imap_source.scan(db, job, client, checkpoint)
                atomic_json(base / 'folders.json', summary)
            state('DOWNLOADING', 'Скачивание исходных писем')
            job.downloaded = 0; job.downloaded_bytes = 0
            selected = None
            for row in db.scalars(message_query(job)).yield_per(100):
                checkpoint('Загрузка: ' + row.folder)
                target = sources / row.relative_path
                valid = target.is_file() and target.stat().st_size == row.size and row.sha256
                if not valid:
                    pair = row.folder, row.validity
                    if selected != pair:
                        response = client.select_folder(row.folder, readonly=True)
                        if int(response[b'UIDVALIDITY']) != row.validity:
                            raise imap_source.SourceChanged('UIDVALIDITY изменился; необходимо перечитать список.')
                        selected = pair
                    free_space(base, row.size, reserve)
                    target.parent.mkdir(parents=True, exist_ok=True)
                    partial = target.with_suffix('.eml.part')
                    row.sha256 = imap_source.download_message(client, row, partial, checkpoint)
                    os.replace(partial, target)
                    db.commit()  # checkpoint only after the file is durable
                job.downloaded += 1; job.downloaded_bytes += row.size
            db.commit()
    # Revalidate each stored message while writing the manifest. A damaged file
    # must be downloaded again, never accepted merely because its size matches.
    state('VERIFYING', 'Проверка исходников')
    manifest = base / 'manifest.jsonl'
    partial_manifest = base / 'manifest.jsonl.part'
    with partial_manifest.open('w', encoding='utf-8') as stream:
        for row in db.scalars(message_query(job)).yield_per(100):
            path = sources / row.relative_path
            if not path.is_file() or sha256(path, checkpoint) != row.sha256:
                row.sha256 = None; db.commit()
                raise imap_source.SourceChanged('Исходный файл повреждён или отсутствует; будет скачан повторно.')
            stream.write(json.dumps(dict(file=row.relative_path, folder=row.target_folder,
                received_at=row.received_at, seen='\\Seen' in json.loads(row.flags),
                flags=json.loads(row.flags), uid=row.uid, uidvalidity=row.validity,
                source_folder=row.folder, sha256=row.sha256), ensure_ascii=False) + '\n')
    os.replace(partial_manifest, manifest)
    job.downloaded = job.total; job.downloaded_bytes = job.total_bytes
    if not job.total:
        job.status = 'EMPTY'; job.stage = 'Ящик пуст; отчёт сохранён, файлы почты не создавались'
        atomic_json(base / 'report.json', {'messages': 0, 'status': 'EMPTY'})
        db.commit(); return

    def records():
        with manifest.open(encoding='utf-8') as stream:
            for line in stream:
                checkpoint('Упаковка TGZ')
                yield json.loads(line)

    safe_email = ''.join(c if c.isalnum() or c in '@._-' else '_' for c in job.target)
    filename = job.created_at.strftime('%Y%m%d') + '-' + safe_email
    # Each task and each delta export has its own directory; public filenames
    # remain exactly YYYYMMDD-email without job numbers or collision overwrites.
    exports = base / f'export-{job.export_generation}'
    exports.mkdir(exist_ok=True)
    tgz = exports / (filename + '.tgz')
    ready_tgz = job.tgz_sha256 and tgz.is_file() and sha256(tgz, checkpoint) == job.tgz_sha256
    if not ready_tgz:
        state('BUILDING_TGZ', 'Создание Zimbra TGZ')
        job.tgz_status = 'BUILDING'; db.commit()
        free_space(base, int(job.total_bytes * 1.02) + 1024**2, reserve)
        partial = tgz.with_suffix('.tgz.part')
        partial.unlink(missing_ok=True)
        count = build_tgz(sources, records(), partial)
        if count != job.total: raise RuntimeError('Message count mismatch')
        job.tgz_sha256 = sha256(partial, checkpoint)
        os.replace(partial, tgz)
        job.tgz_path = str(tgz)
        job.tgz_status = 'READY' if job.want_tgz else 'INTERMEDIATE'
        db.commit()
    if job.want_pst and job.pst_status != 'READY':
        state('CONVERTING', 'Создание PST')
        job.pst_status = 'BUILDING'; db.commit()
        # Expansion and writer temporary files all live on the work volume.
        output = local / 'result.pst'
        ready_file = local / 'pst-ready.json'
        ready = json.loads(ready_file.read_text()) if ready_file.exists() else {}
        cached = (ready.get('generation') == job.export_generation and output.is_file()
                  and sha256(output, checkpoint) == ready.get('sha256'))
        if not cached:
            free_space(local, int(job.total_bytes * max(4.0, float(cfg['space_factor']))) + GB, reserve)
            output.unlink(missing_ok=True)
            try:
                with tempfile.TemporaryDirectory(prefix='convert-', dir=local) as temp:
                    convert_pst(cfg['converter_command'], tgz, output, job.target, checkpoint, Path(temp))
                atomic_json(ready_file, {'generation': job.export_generation, 'sha256': sha256(output, checkpoint)})
            except (Cancelled, NeedSpace, OSError): raise
            except Exception:
                job.pst_status = 'FAILED'
                raise RuntimeError('Ошибка конвертера PST. TGZ и исходники сохранены; повтор не скачивает почту заново.') from None
        state('VERIFYING', 'Копирование PST на хранилище')
        free_space(base, output.stat().st_size, reserve)
        final = exports / (filename + '.pst')
        job.pst_sha256 = publish(output, final, checkpoint)
        job.pst_path = str(final); job.pst_status = 'READY'; job.pst_expires = now() + timedelta(days=14)
        artifact = db.get(MigrationArtifact, str(final)) or MigrationArtifact(path=str(final), migration_id=job.id)
        artifact.expires_at = job.pst_expires
        db.add(artifact)
        db.commit()
        output.unlink()
        ready_file.unlink(missing_ok=True)
    atomic_json(exports / 'report.json', dict(account=job.account, target=job.target,
        messages=job.total, bytes=job.total_bytes, tgz_sha256=job.tgz_sha256,
        pst_sha256=job.pst_sha256, generation=job.export_generation,
        limitations=['Mail only', 'TGZ: folders, received dates and Seen flag; other flags in manifest',
                     'PST experimental; validate in Outlook', 'Repeated TGZ import may duplicate messages']))
    job.status = 'COMPLETED'; job.stage = 'Выбранные файлы сохранены и проверены'; job.retries = 0
    job.error = None; db.commit()


def recover(db):
    for job in db.scalars(select(Migration).where(Migration.status.in_(ACTIVE))):
        job.status = 'QUEUED'; job.stage = 'Продолжение после перезапуска'
    db.commit()


def cleanup(db):
    for artifact in db.scalars(select(MigrationArtifact).where(MigrationArtifact.expires_at < now())):
        try:
            job = db.get(Migration, artifact.migration_id)
            storage_root(job)
            Path(artifact.path).unlink(missing_ok=True)
            if job.pst_path == artifact.path:
                job.pst_path = None; job.pst_status = 'EXPIRED'
            db.delete(artifact); db.commit()
        except OSError: continue
        except StorageMissing: continue


def tick(db):
    cleanup(db)
    job = db.scalar(select(Migration).where(Migration.status.in_(READY),
        or_(Migration.next_try.is_(None), Migration.next_try <= now())).order_by(Migration.created_at).limit(1))
    if job is None: return False
    claimed = db.execute(update(Migration).where(Migration.id == job.id, Migration.status.in_(READY)).values(status='SCANNING'))
    if not claimed.rowcount:
        db.rollback(); return False
    job.status = 'SCANNING'; job.error = None; db.commit()
    try:
        process(db, job)
    except Cancelled:
        job.status = 'CANCELLED'; job.stage = 'Отменено; исходники сохранены'
    except NeedSpace:
        job.status = 'WAITING_FOR_SPACE'; job.stage = 'Недостаточно свободного места с учётом резерва'
    except (StorageMissing, PermissionError):
        job.status = 'WAITING_FOR_STORAGE'; job.stage = 'Хранилище недоступно или нет разрешения на запись'
    except ssl.SSLCertVerificationError:
        job.status = 'FAILED'; job.error = 'Проверка TLS-сертификата IMAP не пройдена'
    except imap_source.AuthenticationError as exc:
        job.status = 'FAILED'; job.error = str(exc)
    except (imap_source.SourceChanged, IMAPClient.AbortError, ConnectionError, TimeoutError):
        job.scanned = False; job.retries += 1
        job.status = 'WAITING_FOR_SOURCE' if job.retries <= 8 else 'FAILED'
        job.error = 'Источник изменился или соединение прервано. Исходники сохранены.'
    except imap_source.SourceError as exc:
        job.status = 'FAILED'; job.error = str(exc)
    except IMAPClient.Error:
        job.status = 'FAILED'; job.error = 'IMAP отклонил чтение списка папок или писем; проверьте доступ к ящику.'
    except OSError as exc:
        job.status = 'WAITING_FOR_SPACE' if exc.errno in (errno.ENOSPC, errno.EDQUOT) else 'WAITING_FOR_STORAGE'
        job.error = 'Ошибка диска/сети; проверьте доступность IMAP и хранилища'
    except Exception:
        job.status = 'FAILED'
        job.error = ('Не удалось создать PST; готовый TGZ доступен.' if job.pst_status == 'FAILED'
                     else 'Ошибка обработки. Проверьте доступы и отчёт; исходники сохранены.')
    if job.status in READY:
        job.next_try = now() + timedelta(seconds=min(1800, 60 * 2**min(job.retries, 5)))
    else: job.next_try = None
    db.commit()
    return True
