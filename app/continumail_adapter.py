"""Convert a Zimbra TGZ to PST through ContinuMail's Linux CLI."""
import json, os, re, shutil, subprocess, sys, tarfile, tempfile
from email import policy
from email.parser import BytesParser
from pathlib import Path, PurePosixPath

CLI=Path(os.environ.get("CONTINUMAIL_CLI","/opt/continumail/Mail2Pst.Cli"))

def _score_russian(text):
    cyr=sum("А"<=c<="я" or c in "Ёё" for c in text)
    bad=text.count("�")+sum(ord(c)<32 and c not in "\r\n\t" for c in text)
    lower=text.lower()
    common=sum(lower.count(pair) for pair in ("ст","но","на","ен","ов","ни","ра","ко","то","пр","по","ро","го","ал","ер","ть","ли"))
    mojibake=sum(lower.count(mark) for mark in ("Ð","Ñ","î","á","ð","ô","Á","Ï","Ò"))
    return cyr*3+common*5-bad*20-mojibake*8

def _text_candidates(raw,declared):
    """Yield normal text and common Latin-1/CP1252 mojibake repairs.

    The only destination encodings we recognise are UTF-8, Windows-1251 and
    KOI8-R. Latin-1/CP1252 are used only to reverse an earlier bad decode.
    """
    allowed=("utf-8","cp1251","koi8-r")
    names=[]
    normalized=declared.lower().replace("_","-")
    aliases={"windows-1251":"cp1251","windows1251":"cp1251","koi8r":"koi8-r","utf8":"utf-8"}
    normalized=aliases.get(normalized,normalized)
    if normalized in allowed: names.append(normalized)
    names.extend(x for x in allowed if x not in names)
    seen=set()
    for charset in names:
        try: text=raw.decode(charset)
        except UnicodeDecodeError: continue
        if text not in seen:
            seen.add(text); yield charset,text
        # Typical Zimbra legacy damage: KOI8-R/CP1251 bytes were decoded as
        # Latin-1 (or CP1252) and that mojibake was then saved as UTF-8.
        for bridge in ("latin-1","cp1252"):
            try: legacy_bytes=text.encode(bridge)
            except UnicodeEncodeError: continue
            for legacy in ("cp1251","koi8-r"):
                try: repaired=legacy_bytes.decode(legacy)
                except UnicodeDecodeError: continue
                if repaired not in seen:
                    seen.add(repaired); yield f"{charset}/{bridge}->{legacy}",repaired

def _repair_header_text(text):
    """Repair already-decoded mojibake in MIME headers and parameters."""
    candidates=[(_score_russian(text),text)]
    for bridge,target in (("latin-1","cp1251"),("latin-1","koi8-r"),
                          ("cp1252","cp1251"),("cp1252","koi8-r"),
                          ("cp1251","utf-8")):
        try: repaired=text.encode(bridge).decode(target)
        except (UnicodeEncodeError,UnicodeDecodeError): continue
        candidates.append((_score_russian(repaired),repaired))
    return max(candidates,key=lambda item:item[0])[1]

def _normalize_headers(message):
    changed=False
    # Always rewrite these fields. Even correctly declared KOI8-R/CP1251 headers
    # must become canonical UTF-8 encoded-words before MimeKit/ContinuMail sees
    # them; otherwise some valid legacy headers are decoded as a Western codepage.
    for name in ("Subject","From","To","Cc","Bcc","Reply-To","Sender","Content-Description"):
        values=message.get_all(name,[])
        if not values: continue
        repaired=[_repair_header_text(str(value)) for value in values]
        del message[name]
        for value in repaired: message[name]=value
        changed=True
    for part in message.walk():
        for header,param in (("Content-Disposition","filename"),("Content-Type","name")):
            value=part.get_param(param,header=header)
            if not value: continue
            repaired=_repair_header_text(str(value))
            part.set_param(param,repaired,header=header,charset="utf-8",replace=True)
            changed=True
    return changed

def normalize_eml(data):
    """Normalize legacy text bodies (not attachments) to UTF-8."""
    try: message=BytesParser(policy=policy.SMTP).parsebytes(data)
    except Exception: return data
    changed=_normalize_headers(message)
    for part in message.walk():
        if part.get_content_maintype()!="text" or part.is_multipart(): continue
        raw=part.get_payload(decode=True)
        if raw is None: continue
        declared=part.get_content_charset() or "ascii"
        candidates=[(_score_russian(text),charset,text) for charset,text in _text_candidates(raw,declared)]
        if not candidates: continue
        _,chosen,text=max(candidates,key=lambda x:x[0])
        if chosen.lower()!=declared.lower() or declared.lower() not in ("utf-8","us-ascii","ascii"):
            if part.get("Content-Transfer-Encoding"): del part["Content-Transfer-Encoding"]
            part.set_payload(text,charset="utf-8"); changed=True
    return message.as_bytes(policy=policy.SMTP) if changed else data

def mboxrd(data):
    data=data.replace(b"\r\n",b"\n").replace(b"\r",b"\n")
    data=re.sub(br"(?m)^(>*From )",br">\1",data)
    return b"From MAILER-DAEMON Sat Jan 01 00:00:00 2000\n"+data.rstrip(b"\n")+b"\n\n"

def tgz_to_mboxes(tgz,root):
    handles={}; counts={}; total=0; expanded=0
    limit=max(os.path.getsize(tgz)*30,1024**3)
    try:
        with tarfile.open(tgz,"r:gz") as tf:
            for member in tf:
                if not member.isfile() or not member.name.lower().endswith(".eml"): continue
                parts=[p for p in PurePosixPath(member.name).parts if p not in ("",".")]
                if not parts or ".." in parts: raise RuntimeError("unsafe path in TGZ")
                expanded+=member.size
                if expanded>limit: raise RuntimeError("TGZ expansion limit exceeded")
                folder=tuple(parts[:-1]) or ("Root",)
                if folder not in handles:
                    path=root/(f"source-{len(handles):05d}.mbox")
                    handles[folder]=path.open("ab"); counts[folder]=0
                source=tf.extractfile(member)
                if source is None: continue
                with source: handles[folder].write(mboxrd(normalize_eml(source.read())))
                counts[folder]+=1; total+=1
    finally:
        for handle in handles.values(): handle.close()
    if not total: raise RuntimeError("Zimbra TGZ contains no .eml files")
    sources=[]
    for folder,handle in handles.items():
        sources.append({"path":handle.name,"type":"mbox","targetFolderPath":list(folder),"displayName":folder[-1]})
    return total,sources

def convert(tgz,pst):
    if not CLI.is_file(): raise RuntimeError("ContinuMail CLI is not installed in the image")
    with tempfile.TemporaryDirectory(prefix="continumail-") as tmp:
        root=Path(tmp); expected,sources=tgz_to_mboxes(tgz,root)
        config={"outputs":[{"name":Path(pst).stem,"maxSizeMB":50000,"folderMapping":"mirror","includeEmptyFolders":False,"sources":sources}]}
        cfg=root/"config.json"; cfg.write_text(json.dumps(config,ensure_ascii=False),encoding="utf-8")
        out=root/"out"; out.mkdir(); log=root/"converter.log"
        with log.open("wb") as stream:
            result=subprocess.run([str(CLI),"convert","--config",str(cfg),"--output",str(out)],stdout=stream,stderr=subprocess.STDOUT,timeout=86400)
        lines=log.read_text(errors="replace").splitlines(); terminal=None
        for line in reversed(lines):
            try:
                event=json.loads(line)
                if event.get("type") in ("done","error","cancelled"): terminal=event; break
            except (json.JSONDecodeError,AttributeError): pass
        if result.returncode or not terminal or terminal.get("type")!="done":
            raise RuntimeError(f"ContinuMail failed (exit {result.returncode}): {terminal or lines[-5:]}")
        if int(terminal.get("skipped",0))>0: raise RuntimeError(f"ContinuMail skipped messages: {terminal}")
        if int(terminal.get("converted",-1))!=expected: raise RuntimeError(f"ContinuMail count mismatch: {terminal.get('converted')} of {expected}")
        outputs=[Path(x) for x in terminal.get("outputs",[])]
        candidates=[x if x.is_absolute() else out/x.name for x in outputs]
        candidates=[x for x in candidates if x.is_file()]
        if len(candidates)!=1: raise RuntimeError(f"ContinuMail produced {len(candidates)} PST files; expected one")
        Path(pst).parent.mkdir(parents=True,exist_ok=True); shutil.copy2(candidates[0],pst)

def main():
    if len(sys.argv)!=3: raise SystemExit("usage: continumail_adapter INPUT.tgz OUTPUT.pst")
    convert(sys.argv[1],sys.argv[2])

if __name__=="__main__": main()
