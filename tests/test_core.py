import os, tarfile
from email import policy
from email.parser import BytesParser
from app.converter import count_messages, verify_pst
from app.worker import enough, sanitize, sanitize_email
from app.zimbra import parse_df_free
from app.continumail_adapter import mboxrd, normalize_eml, tgz_to_mboxes

def test_space_reserve():
    assert enough(110,100,10)
    assert not enough(109,100,10)

def test_safe_filename(): assert sanitize("a/user@example.com") == "a_user_example.com"
def test_safe_email_filename(): assert sanitize_email("user@example.com") == "user@example.com"

def test_count_messages(tmp_path):
    eml=tmp_path/"one.eml"; eml.write_text("Subject: x\n\nbody")
    tgz=tmp_path/"mail.tgz"
    with tarfile.open(tgz,"w:gz") as tf: tf.add(eml,arcname="Inbox/one.eml")
    assert count_messages(tgz)==1

def test_verify_pst_signature(tmp_path):
    pst=tmp_path/"x.pst"; pst.write_bytes(b"!BDN"+b"0"*508)
    assert verify_pst(pst)==512

def test_parse_posix_df():
    output="Filesystem 1024-blocks Used Available Capacity Mounted on\n/dev/sda1 100000 25000 75000 25% /tmp"
    assert parse_df_free(output)==75000*1024

def test_adapter_builds_mbox_sources(tmp_path):
    source=tmp_path/"source"; source.mkdir()
    (source/"one.eml").write_text("Subject: test\n\nBody")
    (source/"ignore.txt").write_text("ignore")
    tgz=tmp_path/"mail.tgz"
    with tarfile.open(tgz,"w:gz") as tf:
        tf.add(source/"one.eml",arcname="Inbox/one.eml")
        tf.add(source/"ignore.txt",arcname="Inbox/ignore.txt")
    target=tmp_path/"out"; target.mkdir()
    count,sources=tgz_to_mboxes(tgz,target)
    assert count==1 and sources[0]["targetFolderPath"]==["Inbox"]
    assert b"Subject: test" in (target/"source-00000.mbox").read_bytes()

def test_mboxrd_escapes_from_lines():
    assert b"\n>From body\n" in mboxrd(b"Subject: x\r\n\r\nFrom body\r\n")

def test_normalizes_legacy_koi8_body():
    body="Направляю шесть камер".encode("koi8-r")
    eml=b"Subject: test\r\nContent-Type: text/plain; charset=iso-8859-1\r\n\r\n"+body
    normalized=normalize_eml(eml)
    parsed=BytesParser(policy=policy.default).parsebytes(normalized)
    assert parsed.get_payload(decode=True).decode("utf-8")=="Направляю шесть камер"

def test_repairs_utf8_saved_koi8_mojibake():
    wanted="Направляю шесть камер"
    damaged=wanted.encode("koi8-r").decode("latin-1")
    eml=("Subject: test\r\nContent-Type: text/plain; charset=utf-8\r\n"
         "Content-Transfer-Encoding: 8bit\r\n\r\n").encode()+damaged.encode("utf-8")
    normalized=normalize_eml(eml)
    parsed=BytesParser(policy=policy.default).parsebytes(normalized)
    assert parsed.get_payload(decode=True).decode("utf-8")==wanted

def test_repairs_utf8_saved_cp1251_mojibake():
    wanted="Получено новое сообщение"
    damaged=wanted.encode("cp1251").decode("latin-1")
    eml=("Subject: test\r\nContent-Type: text/plain; charset=utf-8\r\n\r\n").encode()+damaged.encode("utf-8")
    normalized=normalize_eml(eml)
    parsed=BytesParser(policy=policy.default).parsebytes(normalized)
    assert parsed.get_payload(decode=True).decode("utf-8")==wanted

def test_repairs_subject_recipient_and_attachment_filename():
    subject="Камера В1.1-4"
    person="Павел Сергеевич"
    filename="Чертёж камеры.dwg"
    damage=lambda value: value.encode("koi8-r").decode("latin-1")
    eml=(f"Subject: {damage(subject)}\r\n"
         f'To: "{damage(person)}" <user@example.com>\r\n'
         "MIME-Version: 1.0\r\n"
         f'Content-Type: application/octet-stream; name="{damage(filename)}"\r\n'
         f'Content-Disposition: attachment; filename="{damage(filename)}"\r\n\r\n').encode("utf-8")
    parsed=BytesParser(policy=policy.default).parsebytes(normalize_eml(eml))
    assert str(parsed["Subject"])==subject
    assert person in str(parsed["To"])
    assert parsed.get_filename()==filename

def test_rewrites_valid_koi8_headers_as_utf8():
    wanted="Камера и чертёж"
    from email.header import Header
    encoded=Header(wanted,"koi8-r").encode()
    normalized=normalize_eml(f"Subject: {encoded}\r\n\r\nBody".encode("ascii"))
    assert b"koi8-r" not in normalized.lower()
    assert b"utf-8" in normalized.lower()
    parsed=BytesParser(policy=policy.default).parsebytes(normalized)
    assert str(parsed["Subject"])==wanted
