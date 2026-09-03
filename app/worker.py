import os, shutil, time, traceback
from datetime import timedelta
from sqlalchemy import select
from .db import Base, engine, SessionLocal
from .models import Job, JobEvent, JobStatus, now
from .settings import get_all
from .zimbra import ZimbraClient
from .converter import CommandConverter, ConverterUnavailable, count_messages, verify_pst

GB=1024**3

def transition(db,job,status,message="",progress=None):
    job.status=status
    if status!=JobStatus.FAILED: job.error=None
    if progress is not None: job.progress=progress
    db.add(JobEvent(job_id=job.id,status=status.value,message=message)); db.commit()

def local_free(path):
    os.makedirs(path,exist_ok=True); return shutil.disk_usage(path).free

def enough(free,needed,reserve): return free >= needed+reserve

def claim(db):
    retry_before=now()-timedelta(seconds=60)
    q=(select(Job).where((Job.status==JobStatus.QUEUED)|((Job.status.in_([JobStatus.WAITING_FOR_SPACE,JobStatus.STORED_ON_NAS]))&(Job.updated_at<retry_before)))
       .order_by(Job.created_at).with_for_update(skip_locked=True).limit(1))
    job=db.execute(q).scalar_one_or_none()
    if job: job.updated_at=now(); db.commit()
    return job

def cancelled(db,job):
    db.refresh(job)
    if job.cancel_requested:
        transition(db,job,JobStatus.CANCELLED,"Cancelled by administrator",job.progress); return True
    return False

def sanitize(value): return "".join(c if c.isalnum() or c in ".-_" else "_" for c in value)

def process(db,job):
    cfg=get_all(db,secrets=True); reserve=int(float(cfg["min_free_gb"])*GB)
    work=os.path.join(cfg["work_dir"],str(job.id)); os.makedirs(work,exist_ok=True)
    local_tgz=os.path.join(work,"mailbox.tgz"); pst=os.path.join(cfg["pst_dir"],f"{job.id}-{sanitize(job.account)}.pst")
    os.makedirs(cfg["pst_dir"],exist_ok=True)
    if job.nas_tgz and not os.path.exists(local_tgz):
        transition(db,job,JobStatus.CHECKING_SPACE,"Checking space to restore TGZ",35)
        if not enough(local_free(cfg["work_dir"]),job.tgz_bytes or 0,reserve):
            job.resume_status=JobStatus.STORED_ON_NAS.value; transition(db,job,JobStatus.WAITING_FOR_SPACE,"Not enough Docker storage to restore TGZ",35); return
        shutil.copy2(job.nas_tgz,local_tgz); job.local_tgz=local_tgz; db.commit()
    if not job.remote_tgz and not os.path.exists(local_tgz):
        with ZimbraClient(cfg) as z:
            transition(db,job,JobStatus.CHECKING_SPACE,"Checking Zimbra free space",5)
            job.mailbox_bytes=z.mailbox_size(job.account); db.commit()
            remote=f"/tmp/zimbra-pst-{job.id}.tgz"
            if not enough(z.free_bytes("/tmp"),int(job.mailbox_bytes*float(cfg["space_factor"])),reserve):
                job.resume_status=JobStatus.PREPARING_TGZ.value; transition(db,job,JobStatus.WAITING_FOR_SPACE,"Not enough space on Zimbra",5); return
            transition(db,job,JobStatus.PREPARING_TGZ,"Creating TGZ on Zimbra",10)
            job.tgz_bytes=z.prepare_tgz(job.account,remote); job.remote_tgz=remote; db.commit()
            transition(db,job,JobStatus.TGZ_READY,"TGZ ready on Zimbra",25)
    if cancelled(db,job): return
    if not os.path.exists(local_tgz):
        transition(db,job,JobStatus.CHECKING_SPACE,"Checking Docker storage",30)
        if not enough(local_free(cfg["work_dir"]),job.tgz_bytes or 0,reserve):
            if cfg["nas_enabled"]=="true":
                os.makedirs(cfg["nas_dir"],exist_ok=True)
                if enough(local_free(cfg["nas_dir"]),job.tgz_bytes or 0,reserve):
                    nas=os.path.join(cfg["nas_dir"],f"{job.id}-{sanitize(job.account)}.tgz")
                    with ZimbraClient(cfg) as z: z.download(job.remote_tgz,nas)
                    job.nas_tgz=nas; transition(db,job,JobStatus.STORED_ON_NAS,"TGZ moved to NAS; waiting for Docker space",30); return
            job.resume_status=JobStatus.DOWNLOADING.value; transition(db,job,JobStatus.WAITING_FOR_SPACE,"Not enough Docker/NAS space",30); return
        transition(db,job,JobStatus.DOWNLOADING,"Downloading TGZ",40)
        with ZimbraClient(cfg) as z: z.download(job.remote_tgz,local_tgz)
        job.local_tgz=local_tgz; db.commit()
    if cancelled(db,job): return
    job.message_count=count_messages(local_tgz); db.commit()
    transition(db,job,JobStatus.CHECKING_SPACE,"Checking space for PST",55)
    estimated=int((job.tgz_bytes or 0)*float(cfg["space_factor"]))
    if not enough(local_free(cfg["pst_dir"]),estimated,reserve):
        job.resume_status=JobStatus.CONVERTING.value; transition(db,job,JobStatus.WAITING_FOR_SPACE,"Not enough PST storage",55); return
    transition(db,job,JobStatus.CONVERTING,"Running configured PST writer",60)
    CommandConverter(cfg["converter_command"]).convert(local_tgz,pst)
    transition(db,job,JobStatus.VERIFYING,"Verifying PST signature and size",90)
    job.pst_bytes=verify_pst(pst); job.pst_path=pst
    if job.remote_tgz:
        with ZimbraClient(cfg) as z: z.remove(job.remote_tgz)
    if os.path.exists(local_tgz): os.remove(local_tgz)
    if job.nas_tgz and os.path.exists(job.nas_tgz): os.remove(job.nas_tgz)
    job.remote_tgz=job.local_tgz=job.nas_tgz=None; job.completed_at=now(); job.expires_at=now()+timedelta(days=14)
    transition(db,job,JobStatus.COMPLETED,"PST completed and TGZ removed",100)

def cleanup(db):
    for job in db.scalars(select(Job).where(Job.status==JobStatus.COMPLETED,Job.expires_at<now())):
        if job.pst_path and os.path.exists(job.pst_path): os.remove(job.pst_path)
        job.status=JobStatus.EXPIRED; job.pst_path=None; db.add(JobEvent(job_id=job.id,status="EXPIRED",message="PST retention expired"))
    db.commit()

def main():
    Base.metadata.create_all(engine)
    while True:
        with SessionLocal() as db:
            try:
                cleanup(db); job=claim(db)
                if job:
                    try: process(db,job)
                    except ConverterUnavailable as e:
                        job.error=str(e); transition(db,job,JobStatus.FAILED,str(e),job.progress)
                    except Exception as e:
                        job.error=f"{type(e).__name__}: {e}"; db.add(JobEvent(job_id=job.id,status="FAILED",message=job.error)); job.status=JobStatus.FAILED; db.commit()
            except Exception: traceback.print_exc()
        time.sleep(5)

if __name__=="__main__": main()
