import os, shlex, subprocess, tarfile

class ConverterUnavailable(RuntimeError): pass

class CommandConverter:
    def __init__(self,command): self.command=command
    def convert(self,tgz,pst,store_name=None):
        if not self.command: raise ConverterUnavailable("PST converter is not configured; TGZ retained for retry")
        process_env=os.environ.copy()
        if store_name: process_env["PST_STORE_NAME"]=store_name
        result=subprocess.run(shlex.split(self.command)+[tgz,pst],capture_output=True,text=True,timeout=86400,env=process_env)
        if result.returncode: raise RuntimeError(result.stderr[-2000:] or "converter failed")
        if not os.path.isfile(pst) or os.path.getsize(pst)<512: raise RuntimeError("converter did not create a usable PST")

def count_messages(tgz):
    with tarfile.open(tgz,"r:gz") as tf:
        return sum(1 for m in tf if m.isfile() and (m.name.lower().endswith(".eml") or "/message" in m.name.lower()))

def verify_pst(path):
    with open(path,"rb") as f: magic=f.read(4)
    if magic != b"!BDN": raise RuntimeError("invalid PST signature")
    return os.path.getsize(path)
