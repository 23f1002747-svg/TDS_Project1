from fastapi import FastAPI, HTTPException
from starlette.responses import JSONResponse
from models import TaskRequest
from config import get_settings
import asyncio, httpx, json, os, base64, re, git, shutil, stat, time

cfg = get_settings()
app = FastAPI(title="TaskBot", description="Handles AI code tasks and pushes to GitHub")

last_task = {}

GITHUB_API = "https://api.github.com"
PAGES_BASE = f"https://{get_settings().GIT_USERNAME}.github.io"

def check_secret(sec):
    return sec == cfg.STUDENT_SECRET

async def prep_repo(local, name, auth_url, http_url, round_num):
    gh_user = cfg.GIT_USERNAME
    gh_token = cfg.GIT_TOKEN
    headers = {
        "Authorization": f"token {gh_token}",
        "Accept": "application/vnd.github.v3+json",
        "X-GitHub-Api-Version": "2022-11-28"
    }

    async with httpx.AsyncClient(timeout=45) as client:
        try:
            if round_num == 1:
                print(f"creating repo {name}")
                data = {"name": name, "private": False, "auto_init": True}
                res = await client.post(f"{GITHUB_API}/user/repos", json=data, headers=headers)
                res.raise_for_status()
                repo = git.Repo.init(local)
                repo.create_remote('origin', auth_url)
            else:
                print(f"cloning {name}")
                repo = git.Repo.clone_from(auth_url, local)
            return repo
        except Exception as e:
            print("repo setup failed:", e)
            raise

async def push_repo(repo, tid, rnd, rname):
    gh_user = cfg.GIT_USERNAME
    gh_token = cfg.GIT_TOKEN
    headers = {
        "Authorization": f"token {gh_token}",
        "Accept": "application/vnd.github.v3+json",
        "X-GitHub-Api-Version": "2022-11-28"
    }
    http_url = f"https://github.com/{gh_user}/{rname}"

    async with httpx.AsyncClient(timeout=45) as client:
        try:
            repo.git.add(A=True)
            msg = f"Task {tid} round {rnd}"
            repo.index.commit(msg)
            sha = repo.head.object.hexsha
            repo.git.branch('-M', 'main')
            repo.git.push('--set-upstream', 'origin', 'main', force=True)
            await asyncio.sleep(10)
            pages_api = f"{GITHUB_API}/repos/{gh_user}/{rname}/pages"
            data = {"source": {"branch": "main", "path": "/"}}
            for i in range(5):
                try:
                    r = await client.get(pages_api, headers=headers)
                    ok = (r.status_code == 200)
                    if ok:
                        await client.put(pages_api, json=data, headers=headers)
                    else:
                        await client.post(pages_api, json=data, headers=headers)
                    break
                except Exception as e:
                    await asyncio.sleep(3 * (2 ** i))
            await asyncio.sleep(5)
            pages = f"{PAGES_BASE}/{rname}/"
            return {"repo": http_url, "sha": sha, "pages": pages}
        except Exception as e:
            print("push failed:", e)
            raise

def img_part(uri):
    if not uri.startswith("data:"): 
        return None
    m = re.search(r"data:(?P<m>[^;]+);base64,(?P<b>.*)", uri)
    if not m:
        return None
    mime = m.group('m')
    data = m.group('b')
    if not mime.startswith("image/"): 
        return None
    return {"inlineData": {"data": data, "mimeType": mime}}

def is_img(uri):
    return bool(re.search(r"data:image/[^;]+;base64,", uri, re.IGNORECASE))

async def save_files(tid, files):
    base = os.path.join(os.getcwd(), "generated_tasks")
    path = os.path.join(base, tid)
    os.makedirs(path, exist_ok=True)
    for n, c in files.items():
        with open(os.path.join(path, n), "w", encoding="utf-8") as f:
            f.write(c)
    return path


def safe_json_loads(s):
    # replace unescaped backslashes with escaped ones
    s_fixed = re.sub(r'\\(?=[^"\\/bfnrt])', r'\\\\', s)
    return json.loads(s_fixed)

async def ask_llm(prompt, tid, imgs):
    print("talking to aipipe...")
    sys_msg = "Make 3 files: index.html, README.md, LICENSE (MIT). Respond ONLY in JSON like {\"index.html\":\"...\", \"README.md\":\"...\", \"LICENSE\":\"...\"}"

    contents = [{"parts": imgs + [{"text": prompt}]}] if imgs else [{"parts": [{"text": prompt}]}]
    body = {
        "model": "openai/gpt-4.1-nano",
        "messages": [{"role": "system", "content": sys_msg}, {"role": "user", "content": prompt}],
        "temperature": 0.7
    }

    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {cfg.AIPIPE_KEY}"
    }

    for i in range(3):
        try:
            async with httpx.AsyncClient(timeout=60) as client:
                r = await client.post(cfg.AIPIPE_URL, json=body, headers=headers)
                r.raise_for_status()
                data = r.json()
                text_content = data['choices'][0]['message']['content']
                return safe_json_loads(text_content)
        except Exception as e:
            print(f"LLM attempt {i+1} failed:", e)
            await asyncio.sleep(2 ** i)

    raise Exception("llm fail")



async def ping_eval(url, email, tid, rnd, nonce, repo, sha, pages):
    stuff = {
        "email": email, "task": tid, "round": rnd, "nonce": nonce,
        "repo_url": repo, "commit_sha": sha, "pages_url": pages
    }
    for i in range(3):
        try:
            async with httpx.AsyncClient(timeout=10) as c:
                r = await c.post(url, json=stuff)
                r.raise_for_status()
                print("eval ping ok")
                return True
        except Exception:
            await asyncio.sleep(2 ** i)
    print("eval ping fail")
    return False

async def save_attach(folder, items):
    saved = []
    for a in items:
        fn = a.name
        uri = a.url
        m = re.search(r"base64,(.*)", uri)
        if not m: 
            continue
        b64 = m.group(1)
        data = base64.b64decode(b64)
        with open(os.path.join(folder, fn), "wb") as f:
            f.write(data)
        saved.append(fn)
    return saved

async def makeAndPushStuff(info: TaskRequest):
    tid = info.task
    email = info.email
    rnd = info.round
    txt = info.brief
    eval_url = info.evaluation_url
    nonce = info.nonce
    attach = info.attachments

    print(f"running {tid} round {rnd}")
    repo_name = tid.replace(' ', '-').lower()
    gh_user = cfg.GIT_USERNAME
    gh_token = cfg.GIT_TOKEN
    auth = f"https://{gh_user}:{gh_token}@github.com/{gh_user}/{repo_name}.git"
    http_url = f"https://github.com/{gh_user}/{repo_name}"
    base = os.path.join(os.getcwd(), "generated_tasks")
    loc = os.path.join(base, tid)

    if os.path.exists(loc):
        def fix_err(fn, path, err):
            os.chmod(path, stat.S_IWUSR)
            fn(path)
        shutil.rmtree(loc, onerror=fix_err)
    os.makedirs(loc, exist_ok=True)

    repo = await prep_repo(loc, repo_name, auth, http_url, rnd)

    imgs = []
    names = []
    for a in attach:
        if is_img(a.url):
            p = img_part(a.url)
            if p: imgs.append(p)
        names.append(a.name)
    files = ", ".join(names)

    if rnd > 1:
        prompt = f"update old files for '{txt}', rebuild index.html README.md LICENSE completely."
    else:
        prompt = f"make full html app for: {txt}. use Tailwind, include README.md and MIT LICENSE."
    if files:
        prompt += f" these files exist: {files}"

    gen = await ask_llm(prompt, tid, imgs)
    await save_files(tid, gen)
    await save_attach(loc, attach)
    result = await push_repo(repo, tid, rnd, repo_name)
    await ping_eval(eval_url, email, tid, rnd, nonce, result['repo'], result['sha'], result['pages'])
    print(f"done {tid}")

@app.post("/ready", status_code=200)
async def ready(taskInfo: TaskRequest):
    global last_task
    if not check_secret(taskInfo.secret):
        raise HTTPException(status_code=401, detail="bad secret")
    last_task = taskInfo.dict()
    asyncio.create_task(makeAndPushStuff(taskInfo))
    return JSONResponse(status_code=200, content={"status": "ok", "msg": f"{taskInfo.task} started"})

@app.get("/")
async def root():
    return {"msg": "server on"}

@app.get("/status")
async def status():
    if last_task:
        return {"last": last_task}
    return {"msg": "no tasks yet"}
