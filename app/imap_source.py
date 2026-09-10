"""Read-only IMAP transport. Never STORE/EXPUNGE or log server credentials."""
import hashlib
import json
import ssl
from contextlib import contextmanager
from datetime import timezone
from imapclient import IMAPClient
from sqlalchemy import select, func
from .migration_models import MigrationMessage


class SourceError(RuntimeError): pass
class AuthenticationError(SourceError): pass
class SourceChanged(SourceError): pass


@contextmanager
def connect(job, password):
    client = IMAPClient(job.host, port=job.port, ssl=job.tls == 'ssl',
                        ssl_context=ssl.create_default_context(), timeout=60)
    try:
        if job.tls == 'starttls':
            client.starttls(ssl_context=ssl.create_default_context())
        try:
            client.login(job.account, password)
        except IMAPClient.AbortError:
            raise
        except IMAPClient.Error:
            raise AuthenticationError('Вход IMAP отклонён. Проверьте пароль / пароль приложения.') from None
        client.normalise_times = False
        yield client
    finally:
        try: client.logout()
        except Exception: client.shutdown()


def folder_component(value):
    # Escape, rather than replace, to avoid merging distinct source folders.
    out = ''.join(f'%{ord(c):02X}' if c in '%/\\:*?"<>|' or ord(c) < 32 else c for c in value)
    if out in ('', '.', '..'): out = ''.join(f'%{ord(c):02X}' for c in value) or '%00'
    return out


def folders(client):
    listed = client.list_folders()
    mapping = {}
    specials = {b'\\sent': 'Sent', b'\\drafts': 'Drafts', b'\\trash': 'Trash', b'\\junk': 'Junk'}
    for flags, delimiter, name in listed:
        if name.upper() == 'INBOX': mapping[name] = 'Inbox'
        for flag in flags:
            if flag.lower() in specials: mapping[name] = specials[flag.lower()]
    result, targets = [], set()
    for flags, delimiter, name in listed:
        if b'\\noselect' in {f.lower() for f in flags}: continue
        delimiter = delimiter.decode() if isinstance(delimiter, bytes) else delimiter
        target = None
        for source in sorted(mapping, key=len, reverse=True):
            if name == source:
                target = mapping[source]; break
            if delimiter and name.startswith(source + delimiter):
                target = mapping[source] + '/' + '/'.join(folder_component(p) for p in name[len(source)+len(delimiter):].split(delimiter))
                break
        if target is None:
            target = '/'.join(folder_component(p) for p in (name.split(delimiter) if delimiter else [name]))
        if target.casefold() in targets:
            target += '-' + hashlib.sha256(name.encode()).hexdigest()[:12]
        targets.add(target.casefold())
        result.append((name, target))
    return result


def scan(db, job, client, checkpoint):
    job.scan_generation += 1
    generation = job.scan_generation
    job.scanned = False
    db.commit()
    summary = []
    for folder, target in folders(client):
        checkpoint('Список писем: ' + folder)
        info = client.select_folder(folder, readonly=True)
        validity = int(info[b'UIDVALIDITY'])
        key = hashlib.sha256(folder.encode()).hexdigest()
        uids = client.search(['ALL'])
        summary.append({'source': folder, 'target': target, 'messages': len(uids), 'uidvalidity': validity})
        for offset in range(0, len(uids), 200):
            checkpoint('Оценка размера: ' + folder)
            batch = uids[offset:offset+200]
            items = client.fetch(batch, ['RFC822.SIZE', 'INTERNALDATE', 'FLAGS'])
            if set(items) != set(batch):
                raise SourceChanged('Письма изменились во время чтения списка; повторите сканирование.')
            for uid, data in items.items():
                stamp = data[b'INTERNALDATE']
                if stamp is None or stamp.tzinfo is None:
                    raise SourceError('IMAP не вернул дату получения с часовым поясом.')
                row = db.scalar(select(MigrationMessage).where(
                    MigrationMessage.migration_id == job.id, MigrationMessage.folder_key == key,
                    MigrationMessage.validity == validity, MigrationMessage.uid == uid))
                if row is None:
                    row = MigrationMessage(migration_id=job.id, folder_key=key, folder=folder,
                        target_folder=target, validity=validity, uid=uid,
                        relative_path=f'{key}/{validity}/{uid}.eml')
                elif row.size != data[b'RFC822.SIZE']:
                    row.sha256 = None
                row.size = int(data[b'RFC822.SIZE'])
                row.received_at = int(stamp.astimezone(timezone.utc).timestamp())
                row.flags = json.dumps([f.decode('ascii', 'replace') for f in data[b'FLAGS']])
                row.target_folder = target
                row.generation = generation
                db.add(row)
            db.commit()
    job.total, job.total_bytes = db.execute(select(func.count(), func.coalesce(func.sum(MigrationMessage.size), 0)).where(
        MigrationMessage.migration_id == job.id, MigrationMessage.generation == generation)).one()
    job.scanned = True
    db.commit()
    return summary


def download_message(client, row, destination, checkpoint):
    """Fetch in 1 MiB ranges to bound memory, including large attachments."""
    digest = hashlib.sha256()
    offset = 0
    with destination.open('wb') as stream:
        while offset < row.size:
            checkpoint('Загрузка письма')
            length = min(1024*1024, row.size-offset)
            response = client.fetch([row.uid], [f'BODY.PEEK[]<{offset}.{length}>'])
            data = response.get(row.uid, {}).get(f'BODY[]<{offset}>'.encode())
            if not isinstance(data, bytes) or len(data) != length:
                raise SourceChanged('IMAP вернул неполное письмо. Повторите сканирование источника.')
            stream.write(data)
            digest.update(data)
            offset += len(data)
        stream.flush()
        import os
        os.fsync(stream.fileno())
    return digest.hexdigest()
