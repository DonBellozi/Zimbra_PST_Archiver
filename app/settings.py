from sqlalchemy.orm import Session
from .models import Setting
from .security import encrypt, decrypt

SECRET_KEYS={"zimbra_password","zimbra_private_key","zimbra_host_fingerprint","nas_password","converter_command"}
DEFAULTS={"zimbra_host":"","zimbra_port":"22","zimbra_user":"root","zimbra_auth":"key","zimbra_sudo_prefix":"sudo -u zimbra","work_dir":"/data/work","pst_dir":"/data/pst","nas_enabled":"false","nas_dir":"/data/nas","retention_days":"14","min_free_gb":"10","space_factor":"4.0","concurrency":"1","converter_command":"/usr/local/bin/python -m app.continumail_adapter","delete_tgz_after_success":"false"}
DEFAULTS.update(imap_host='', imap_port='993', imap_tls='ssl')

def get_all(db: Session, secrets=False):
    rows={x.key:(decrypt(x.value) if x.encrypted else x.value) for x in db.query(Setting).all()}
    if rows.get("converter_command","").endswith("app.eml2pst_adapter"):
        rows["converter_command"]=DEFAULTS["converter_command"]
    result=DEFAULTS|rows
    if not secrets:
        for k in SECRET_KEYS: result[k]="********" if rows.get(k) else ""
    return result

def save(db: Session, values: dict):
    for key,value in values.items():
        if key not in DEFAULTS and key not in SECRET_KEYS: continue
        if key in SECRET_KEYS and (not value or value=="********"): continue
        row=db.get(Setting,key) or Setting(key=key)
        row.encrypted=key in SECRET_KEYS; row.value=encrypt(value) if row.encrypted else str(value); db.add(row)
    db.commit()
