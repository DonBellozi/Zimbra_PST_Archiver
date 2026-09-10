import tempfile
import tarfile
import unittest
from pathlib import Path

from app.zimbra_tgz import build_tgz


class ZimbraTgzTests(unittest.TestCase):
    def test_raw_mime_folder_date_and_unread(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            raw = b'Subject: =?koi8-r?B?68HNxdLB?=\r\n\r\n\xff\x80\r\n'
            (root / 'one.eml').write_bytes(raw)
            folder = 'Inbox/' + 'Проверка' * 20
            rows = [dict(file='one.eml', folder=folder, received_at=1609459200, seen=seen)
                    for seen in (True, False)]
            self.assertEqual(build_tgz(root, rows, root / 'mail.tgz'), 2)
            with tarfile.open(root / 'mail.tgz') as archive:
                for entry, row in zip(archive.getmembers(), rows):
                    self.assertEqual(entry.name.rsplit('/', 1)[0], folder)
                    self.assertEqual(entry.mtime, row['received_at'])
                    self.assertEqual(bool(entry.mode & 0o200), row['seen'])
                    self.assertEqual(archive.extractfile(entry).read(), raw)

    def test_rejects_traversal_and_preserves_existing_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / 'one.eml').write_bytes(b'Subject: x\r\n\r\ny')
            row = dict(file='one.eml', folder='../outside', received_at=1, seen=True)
            with self.assertRaises(ValueError):
                build_tgz(root, [row], root / 'mail.tgz')
            self.assertFalse((root / 'mail.tgz').exists())
            (root / 'mail.tgz').write_bytes(b'keep')
            with self.assertRaises(FileExistsError):
                build_tgz(root, [], root / 'mail.tgz')
            self.assertEqual((root / 'mail.tgz').read_bytes(), b'keep')


if __name__ == '__main__':
    unittest.main()
