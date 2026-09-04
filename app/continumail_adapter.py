"""Convert a Zimbra TGZ to PST through ContinuMail's Linux CLI."""
import json, os, re, shutil, subprocess, sys, tarfile, tempfile
from pathlib import Path, PurePosixPath

CLI=Path(os.environ.get("CONTINUMAIL_CLI","/opt/continumail/Mail2Pst.Cli"))

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
                with source: handles[folder].write(mboxrd(source.read()))
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
