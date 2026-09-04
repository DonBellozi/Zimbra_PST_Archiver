"""Experimental adapter for EDB Explorer's external eml2pst module."""
import os, re, shutil, subprocess, sys, tarfile, tempfile
from pathlib import Path, PurePosixPath

EDB_EXPLORER=Path(os.environ.get("EDB_EXPLORER_PATH","/opt/EDB_Explorer"))

def extract_eml(tgz, target):
    count=0
    max_bytes=max(os.path.getsize(tgz)*30,1024**3)
    total=0
    with tarfile.open(tgz,"r:gz") as tf:
        for member in tf:
            if not member.isfile() or not member.name.lower().endswith(".eml"): continue
            parts=[p for p in PurePosixPath(member.name).parts if p not in ("",".")]
            if not parts or ".." in parts: raise RuntimeError("unsafe path in TGZ")
            total+=member.size
            if total>max_bytes: raise RuntimeError("TGZ extraction limit exceeded")
            output=target.joinpath(*parts); output.parent.mkdir(parents=True,exist_ok=True)
            source=tf.extractfile(member)
            if source is None: continue
            with source, output.open("wb") as dest: shutil.copyfileobj(source,dest)
            count+=1
    if not count: raise RuntimeError("Zimbra TGZ contains no .eml files")
    return count

def convert(tgz,pst):
    if not EDB_EXPLORER.joinpath("eml2pst").is_dir(): raise RuntimeError("external eml2pst module is not installed")
    Path(pst).parent.mkdir(parents=True,exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="eml2pst-") as tmp:
        root=Path(tmp,"mailbox"); root.mkdir(); expected=extract_eml(tgz,root)
        log=Path(tmp,"converter.log")
        env=os.environ.copy(); env["PYTHONPATH"]=str(EDB_EXPLORER)
        with log.open("wb") as stream:
            result=subprocess.run([sys.executable,"-m","eml2pst",str(root),"-o",pst,"-n","Zimbra Archive"],cwd=EDB_EXPLORER,env=env,stdout=stream,stderr=subprocess.STDOUT,timeout=86400)
        tail=log.read_bytes()[-131072:].decode(errors="replace")
        if result.returncode: raise RuntimeError(f"eml2pst failed: {tail[-2000:]}")
        match=re.search(r"Done:\s+(\d+) messages",tail)
        converted=int(match.group(1)) if match else -1
        errors=re.search(r"\((\d+) errors\)",tail)
        if errors and int(errors.group(1)): raise RuntimeError(f"eml2pst skipped messages: {tail[-2000:]}")
        if converted!=expected: raise RuntimeError(f"eml2pst count mismatch: {converted} of {expected}")

def main():
    if len(sys.argv)!=3: raise SystemExit("usage: eml2pst_adapter INPUT.tgz OUTPUT.pst")
    convert(sys.argv[1],sys.argv[2])

if __name__=="__main__": main()
