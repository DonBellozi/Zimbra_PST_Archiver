import os, shutil
from datetime import datetime, timezone
from fastapi import FastAPI, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware
from sqlalchemy import or_, select
from sqlalchemy.orm import Session
from .config import env
from .db import Base, engine, db_session, initialize_schema
from .models import User, Job, JobEvent, JobStatus
from .security import pwd
from .settings import get_all, save
from .zimbra import ZimbraClient
from .migration_api import make_router

app=FastAPI(title="Zimbra PST Archiver",version="0.2.0")
app.add_middleware(SessionMiddleware,secret_key=env.app_secret_key or "development-only",https_only=False,same_site="lax")
app.mount("/static",StaticFiles(directory="app/static"),name="static")
templates=Jinja2Templates(directory="app/templates")

@app.on_event("startup")
def startup():
    if env.database_url.startswith("sqlite:////"):
        os.makedirs(os.path.dirname(env.database_url.removeprefix("sqlite:///")),exist_ok=True)
    initialize_schema()

def current(request: Request,db: Session):
    uid=request.session.get("uid"); return db.get(User,uid) if uid else None

def guard(request: Request,db: Session=Depends(db_session)):
    user=current(request,db)
    if not user: raise HTTPException(401,"Authentication required")
    return user

app.include_router(make_router(guard, templates))

def initialized(db): return db.query(User).count()>0
def human(n):
    if n is None:return "—"
    for u in ("Б","КБ","МБ","ГБ","ТБ"):
        if abs(n)<1024:return f"{n:.1f} {u}" if isinstance(n,float) else f"{n} {u}"
        n=n/1024
    return f"{n:.1f} ПБ"
templates.env.filters["size"]=human

@app.get("/health")
def health(): return {"status":"ok"}

@app.get("/",response_class=HTMLResponse)
def home(request:Request,db:Session=Depends(db_session)):
    if not initialized(db): return RedirectResponse("/setup",303)
    if not current(request,db): return RedirectResponse("/login",303)
    jobs=db.scalars(select(Job).order_by(Job.created_at.desc()).limit(100)).all(); cfg=get_all(db)
    disks={}
    for key in ("work_dir","pst_dir","nas_dir"):
        path=cfg[key]
        try:
            d=shutil.disk_usage(path); disks[key]={"total":d.total,"free":d.free,"used":d.used}
        except OSError: disks[key]=None
    return templates.TemplateResponse(request,"index.html",{"jobs":jobs,"disks":disks,"statuses":JobStatus})

@app.get("/setup",response_class=HTMLResponse)
def setup_page(request:Request,db:Session=Depends(db_session)):
    if initialized(db): return RedirectResponse("/",303)
    return templates.TemplateResponse(request,"setup.html",{"cfg":get_all(db)})

@app.post("/setup")
async def setup(request:Request,username:str=Form(...),password:str=Form(...),db:Session=Depends(db_session)):
    if initialized(db): raise HTTPException(409,"Already initialized")
    if len(password)<10: raise HTTPException(400,"Password must contain at least 10 characters")
    form=dict(await request.form()); user=User(username=username,password_hash=pwd.hash(password)); db.add(user); db.flush()
    save(db,form); request.session["uid"]=user.id; return RedirectResponse("/",303)

@app.get("/login",response_class=HTMLResponse)
def login_page(request:Request): return templates.TemplateResponse(request,"login.html",{"error":None})

@app.post("/login")
def login(request:Request,username:str=Form(...),password:str=Form(...),db:Session=Depends(db_session)):
    user=db.scalar(select(User).where(User.username==username))
    if not user or not pwd.verify(password,user.password_hash): return templates.TemplateResponse(request,"login.html",{"error":"Неверный логин или пароль"},status_code=401)
    request.session["uid"]=user.id; return RedirectResponse("/",303)

@app.post("/logout")
def logout(request:Request): request.session.clear(); return RedirectResponse("/login",303)

@app.get("/settings",response_class=HTMLResponse)
def settings_page(request:Request,db:Session=Depends(db_session),_=Depends(guard)):
    return templates.TemplateResponse(request,"settings.html",{"cfg":get_all(db),"saved":request.query_params.get("saved")})

@app.post("/settings")
async def settings_save(request:Request,db:Session=Depends(db_session),_=Depends(guard)):
    save(db,dict(await request.form())); return RedirectResponse("/settings?saved=1",303)

@app.post("/api/zimbra/test")
def zimbra_test(db:Session=Depends(db_session),_=Depends(guard)):
    try:
        with ZimbraClient(get_all(db,secrets=True)) as z: return {"ok":True,"version":z.check()}
    except Exception as e: return JSONResponse({"ok":False,"error":str(e)},status_code=400)

@app.get("/api/accounts")
def accounts(q:str="",db:Session=Depends(db_session),_=Depends(guard)):
    try:
        with ZimbraClient(get_all(db,secrets=True)) as z: return {"accounts":z.accounts(q)}
    except Exception as e: raise HTTPException(502,str(e))

@app.post("/api/jobs")
async def create_jobs(request:Request,db:Session=Depends(db_session),_=Depends(guard)):
    data=await request.json(); accounts=data.get("accounts",[])
    if not accounts or len(accounts)>500: raise HTTPException(400,"Select 1–500 accounts")
    jobs=[]
    for account in dict.fromkeys(accounts):
        if not isinstance(account,str) or "@" not in account: raise HTTPException(400,f"Invalid account: {account}")
        j=Job(account=account.strip(),status=JobStatus.QUEUED); db.add(j); db.flush(); db.add(JobEvent(job_id=j.id,status="QUEUED",message="Added from Web UI")); jobs.append(j.id)
    db.commit(); return {"job_ids":jobs}

@app.get("/api/jobs")
def jobs(q:str="",db:Session=Depends(db_session),_=Depends(guard)):
    stmt=select(Job).order_by(Job.created_at.desc()).limit(200)
    if q: stmt=stmt.where(or_(Job.account.ilike(f"%{q}%"),Job.status==q))
    return [{"id":j.id,"account":j.account,"status":j.status,"progress":j.progress,"mailbox_bytes":j.mailbox_bytes,"tgz_bytes":j.tgz_bytes,"pst_bytes":j.pst_bytes,"message_count":j.message_count,"error":j.error} for j in db.scalars(stmt)]

@app.post("/api/jobs/{job_id}/{action}")
def job_action(job_id:int,action:str,db:Session=Depends(db_session),_=Depends(guard)):
    job=db.get(Job,job_id)
    if not job: raise HTTPException(404)
    if action=="cancel": job.cancel_requested=True
    elif action=="retry": job.status=JobStatus.STORED_ON_NAS if job.nas_tgz else JobStatus.QUEUED; job.cancel_requested=False; job.error=None
    elif action=="delete":
        for p in (job.local_tgz,job.nas_tgz,job.pst_path):
            if p and os.path.isfile(p): os.remove(p)
        db.delete(job); db.commit(); return {"ok":True}
    else: raise HTTPException(400)
    db.add(JobEvent(job_id=job.id,status=job.status.value,message=f"Action: {action}")); db.commit(); return {"ok":True}

@app.get("/jobs/{job_id}/download")
def download(job_id:int,db:Session=Depends(db_session),_=Depends(guard)):
    job=db.get(Job,job_id)
    if not job or job.status!=JobStatus.COMPLETED or not job.pst_path or not os.path.isfile(job.pst_path): raise HTTPException(404)
    return FileResponse(job.pst_path,filename=os.path.basename(job.pst_path),media_type="application/vnd.ms-outlook")
