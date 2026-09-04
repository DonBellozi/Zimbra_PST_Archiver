import os, tarfile
from app.converter import count_messages, verify_pst
from app.worker import enough, sanitize
from app.zimbra import parse_df_free
from app.continumail_adapter import mboxrd, tgz_to_mboxes

def test_space_reserve():
    assert enough(110,100,10)
    assert not enough(109,100,10)

def test_safe_filename(): assert sanitize("a/user@example.com") == "a_user_example.com"

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
