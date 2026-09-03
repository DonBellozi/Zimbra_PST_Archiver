import base64, hashlib, io, shlex
import paramiko

class FingerprintPolicy(paramiko.MissingHostKeyPolicy):
    def __init__(self, expected): self.expected=expected.removeprefix("SHA256:").strip()
    def missing_host_key(self,client,hostname,key):
        actual=base64.b64encode(hashlib.sha256(key.asbytes()).digest()).decode().rstrip("=")
        if not self.expected or actual!=self.expected: raise paramiko.SSHException(f"SSH host key mismatch (SHA256:{actual})")
        client.get_host_keys().add(hostname,key.get_name(),key)

class ZimbraClient:
    def __init__(self,cfg): self.cfg=cfg; self.client=None
    def __enter__(self):
        c=paramiko.SSHClient(); c.load_system_host_keys(); c.set_missing_host_key_policy(FingerprintPolicy(self.cfg.get("zimbra_host_fingerprint","")))
        args=dict(hostname=self.cfg["zimbra_host"],port=int(self.cfg["zimbra_port"]),username=self.cfg["zimbra_user"],timeout=20)
        if self.cfg.get("zimbra_auth")=="password": args["password"]=self.cfg.get("zimbra_password")
        else: args["pkey"]=paramiko.RSAKey.from_private_key(io.StringIO(self.cfg["zimbra_private_key"]))
        c.connect(**args); self.client=c; return self
    def __exit__(self,*_): self.client.close()
    def run(self,command,timeout=3600):
        _,out,err=self.client.exec_command(command,timeout=timeout); code=out.channel.recv_exit_status()
        stdout=out.read().decode(errors="replace"); stderr=err.read().decode(errors="replace")
        if code: raise RuntimeError(stderr.strip() or f"remote command failed ({code})")
        return stdout.strip()
    def zrun(self,command): return self.run(f'{self.cfg["zimbra_sudo_prefix"]} {command}')
    def check(self):
        version=self.zrun("zmcontrol -v"); self.zrun("command -v zmprov && command -v zmmailbox"); return version
    def accounts(self,query="",limit=200):
        return [x for x in self.zrun("zmprov -l gaa").splitlines() if query.lower() in x.lower()][:limit]
    def mailbox_size(self,account):
        raw=self.zrun(f"zmmailbox -z -m {shlex.quote(account)} gms")
        return int(raw.split()[0].replace(",",""))
    def prepare_tgz(self,account,remote_path):
        self.zrun(f"zmmailbox -z -m {shlex.quote(account)} getRestURL '//?fmt=tgz' > {shlex.quote(remote_path)}")
        return int(self.run(f"stat -c %s {shlex.quote(remote_path)}"))
    def free_bytes(self,path): return int(self.run(f"df -PB1 --output=avail {shlex.quote(path)} | tail -1"))
    def download(self,remote,local): self.client.open_sftp().get(remote,local)
    def remove(self,remote): self.run(f"rm -f -- {shlex.quote(remote)}")
