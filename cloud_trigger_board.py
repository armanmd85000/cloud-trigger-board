"""
Cloud-to-Cloud Host & Trigger Board
High-Speed MEGA to Google Drive Automated Transfer & Trigger Engine
Includes Residential Proxy Support & Rate-Limit Backoff
"""

import argparse
import base64
import json
import mimetypes
import os
import random
import sys
import threading
import time
import uuid
from typing import Dict, Any, Optional

import requests
from Crypto.Cipher import AES
from Crypto.Util import Counter

import uvicorn
from fastapi import FastAPI, BackgroundTasks, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel

# Load credentials strictly from environment or config.py (secrets never committed to Git)
try:
    import config
    CLIENT_ID = getattr(config, 'GDRIVE_CLIENT_ID', '')
    CLIENT_SECRET = getattr(config, 'GDRIVE_CLIENT_SECRET', '')
    REFRESH_TOKEN = getattr(config, 'GDRIVE_REFRESH_TOKEN', '')
    RESIDENTIAL_PROXY_URL = getattr(config, 'RESIDENTIAL_PROXY_URL', None)
except ImportError:
    CLIENT_ID = os.environ.get('GDRIVE_CLIENT_ID', '')
    CLIENT_SECRET = os.environ.get('GDRIVE_CLIENT_SECRET', '')
    REFRESH_TOKEN = os.environ.get('GDRIVE_REFRESH_TOKEN', '')
    RESIDENTIAL_PROXY_URL = os.environ.get('RESIDENTIAL_PROXY_URL', None)

app = FastAPI(title="Cloud Trigger Board", description="Automated Mega-to-Google Drive Cloner & Trigger Board")
JOBS: Dict[str, Dict[str, Any]] = {}

def base64_urldecode(data: str) -> bytes:
    data = data.replace('-', '+').replace('_', '/')
    data += '=' * (-len(data) % 4)
    return base64.b64decode(data)

def decrypt_attr(attr_b64: str, key_bytes: bytes) -> Optional[dict]:
    iv = b'\x00' * 16
    cipher = AES.new(key_bytes, AES.MODE_CBC, iv)
    data = base64_urldecode(attr_b64)
    decrypted = cipher.decrypt(data)
    if decrypted.startswith(b'MEGA'):
        try:
            decrypted = decrypted[4:].rstrip(b'\x00')
            return json.loads(decrypted.decode('utf-8'))
        except Exception:
            return None
    return None

def decrypt_node_key(k_str: str, master_key_bytes: bytes) -> bytes:
    parts = k_str.split(':')
    enc_k_b64 = parts[-1]
    enc_k = base64_urldecode(enc_k_b64)
    cipher = AES.new(master_key_bytes, AES.MODE_ECB)
    return cipher.decrypt(enc_k)

class GDriveAuth:
    def __init__(self, client_id=CLIENT_ID, client_secret=CLIENT_SECRET, refresh_token=REFRESH_TOKEN):
        self.client_id = client_id
        self.client_secret = client_secret
        self.refresh_token = refresh_token
        self.access_token = None
        self.expires_at = 0
        self.refresh()

    def refresh(self):
        resp = requests.post('https://oauth2.googleapis.com/token', data={
            'client_id': self.client_id,
            'client_secret': self.client_secret,
            'refresh_token': self.refresh_token,
            'grant_type': 'refresh_token'
        }, timeout=15)
        data = resp.json()
        self.access_token = data.get('access_token')
        self.expires_at = time.time() + data.get('expires_in', 3600) - 120
        return self.access_token

    def get_token(self):
        if time.time() >= self.expires_at:
            self.refresh()
        return self.access_token

def get_drive_quota(auth: GDriveAuth) -> dict:
    headers = {'Authorization': f'Bearer {auth.get_token()}'}
    r = requests.get('https://www.googleapis.com/drive/v3/about?fields=user,storageQuota', headers=headers, timeout=15)
    data = r.json()
    user = data.get('user', {})
    quota = data.get('storageQuota', {})
    limit_gb = int(quota.get('limit', 0)) / (1024**3)
    usage_gb = int(quota.get('usage', 0)) / (1024**3)
    free_gb = max(limit_gb - usage_gb, 0)
    return {
        'user': user.get('displayName', 'User'),
        'email': user.get('emailAddress', 'N/A'),
        'limit_gb': round(limit_gb, 2),
        'usage_gb': round(usage_gb, 2),
        'free_gb': round(free_gb, 2),
        'pct_used': round((usage_gb / max(limit_gb, 1)) * 100, 1)
    }

def create_gdrive_folder(auth: GDriveAuth, name: str, parent_id: Optional[str] = None) -> str:
    headers = {
        'Authorization': f'Bearer {auth.get_token()}',
        'Content-Type': 'application/json'
    }
    q = f"name = '{name}' and mimeType = 'application/vnd.google-apps.folder' and trashed = false"
    if parent_id:
        q += f" and '{parent_id}' in parents"
        
    for attempt in range(5):
        try:
            r = requests.get('https://www.googleapis.com/drive/v3/files', headers=headers, params={'q': q, 'fields': 'files(id, name)'}, timeout=15)
            if r.status_code == 200:
                existing = r.json().get('files', [])
                if existing:
                    return existing[0]['id']
                break
            elif r.status_code in (403, 429, 500, 503):
                time.sleep((2 ** attempt) + random.uniform(0.5, 1.5))
        except Exception:
            time.sleep(2 ** attempt)

    meta = {'name': name, 'mimeType': 'application/vnd.google-apps.folder'}
    if parent_id:
        meta['parents'] = [parent_id]
        
    r = requests.post('https://www.googleapis.com/drive/v3/files', headers=headers, json=meta, timeout=15)
    folder_id = r.json()['id']
    
    if not parent_id:
        requests.post(
            f'https://www.googleapis.com/drive/v3/files/{folder_id}/permissions',
            headers=headers,
            json={'role': 'reader', 'type': 'anyone'},
            timeout=10
        )
    return folder_id

def upload_bytes_to_gdrive(auth: GDriveAuth, name: str, data_bytes: bytes, mime_type: str, parent_id: str) -> str:
    size = len(data_bytes)
    headers = {
        'Authorization': f'Bearer {auth.get_token()}',
        'Content-Type': 'application/json; charset=UTF-8',
        'X-Upload-Content-Type': mime_type,
        'X-Upload-Content-Length': str(size)
    }
    meta = {'name': name, 'parents': [parent_id]}
    init_url = 'https://www.googleapis.com/upload/drive/v3/files?uploadType=resumable'
    r_init = requests.post(init_url, headers=headers, json=meta, timeout=20)
    if r_init.status_code != 200:
        raise Exception(f"Init upload failed: {r_init.status_code} {r_init.text}")

    upload_url = r_init.headers['Location']
    upload_headers = {'Content-Length': str(size), 'Content-Type': mime_type}
    r_upload = requests.put(upload_url, headers=upload_headers, data=data_bytes, timeout=180)
    if r_upload.status_code not in (200, 201):
        raise Exception(f"Upload chunk failed: {r_upload.status_code} {r_upload.text}")
    return r_upload.json()['id']

def parse_mega_url(url: str):
    clean_url = url.strip()
    if '#None' in clean_url or '#' not in clean_url:
        raise ValueError("Invalid MEGA URL: Missing folder decryption key (#...)")
    
    parts = clean_url.split('#')
    pre_hash = parts[0]
    post_hash = parts[1]
    
    folder_id = pre_hash.rstrip('/').split('/')[-1]
    sub_handle = None
    if '/folder/' in post_hash:
        key_part, sub_handle = post_hash.split('/folder/')
    else:
        key_part = post_hash.split('/')[0]
        
    return folder_id, key_part, sub_handle

def execute_clone_task(job_id: str, mega_url: str, custom_name: Optional[str] = None, proxy_url: Optional[str] = None):
    job = JOBS[job_id]
    job['status'] = 'parsing'
    job['started_at'] = time.time()
    
    effective_proxy = proxy_url or RESIDENTIAL_PROXY_URL
    proxies = {"http": effective_proxy, "https": effective_proxy} if effective_proxy else None

    try:
        folder_id, key_b64, target_sub_handle = parse_mega_url(mega_url)
        master_key = base64_urldecode(key_b64)
        auth = GDriveAuth()
        job['status'] = 'fetching_metadata'
        
        mega_api_url = f'https://g.api.mega.co.nz/cs?id=0&n={folder_id}'
        r = requests.post(mega_api_url, json=[{'a': 'f', 'c': 1, 'r': 1, 'ca': 1}], timeout=25)
        nodes = r.json()[0]['f']
        
        nodes_by_h = {}
        for n in nodes:
            h = n.get('h')
            p = n.get('p')
            t = n.get('t')
            s = n.get('s', 0)
            k_str = n.get('k')
            a_str = n.get('a')
            name = f"unknown_{h}"
            raw_k = None
            file_key = None
            if k_str and a_str:
                try:
                    raw_k = decrypt_node_key(k_str, master_key)
                    if t == 0:
                        k0 = raw_k[0:16]
                        k1 = raw_k[16:32]
                        file_key = bytes(a ^ b for a, b in zip(k0, k1))
                        attr = decrypt_attr(a_str, file_key)
                    else:
                        file_key = raw_k[:16]
                        attr = decrypt_attr(a_str, file_key)
                    if attr and 'n' in attr:
                        name = attr['n']
                except Exception:
                    pass
            nodes_by_h[h] = {
                'h': h, 'p': p, 't': t, 'size': s,
                'name': name, 'raw_k': raw_k, 'key': file_key
            }

        root_node_handle = target_sub_handle if target_sub_handle else folder_id
        if not target_sub_handle or target_sub_handle not in nodes_by_h:
            roots = [n for n in nodes_by_h.values() if n['t'] == 1 and (not n['p'] or n['p'] not in nodes_by_h)]
            if roots:
                root_node_handle = roots[0]['h']
                
        def is_descendant(h, target):
            curr = h
            while curr in nodes_by_h:
                if curr == target:
                    return True
                curr = nodes_by_h[curr]['p']
            return False

        sub_folders = []
        sub_files = []
        for h, n in nodes_by_h.items():
            if is_descendant(h, root_node_handle):
                if n['t'] == 1:
                    sub_folders.append(n)
                else:
                    sub_files.append(n)

        root_name = custom_name if custom_name else nodes_by_h.get(root_node_handle, {}).get('name', 'Cloud_Clone_Folder')
        job['folder_name'] = root_name
        job['total_files'] = len(sub_files)
        job['total_bytes'] = sum(f['size'] for f in sub_files)
        job['status'] = 'creating_folders'

        root_gdrive_id = create_gdrive_folder(auth, root_name)
        job['gdrive_folder_id'] = root_gdrive_id
        job['gdrive_public_link'] = f'https://drive.google.com/drive/folders/{root_gdrive_id}'

        gdrive_folder_map = {root_node_handle: root_gdrive_id}
        
        def get_depth(h):
            d = 0
            curr = h
            while curr in nodes_by_h and curr != root_node_handle:
                d += 1
                curr = nodes_by_h[curr]['p']
            return d

        sub_folders.sort(key=lambda x: get_depth(x['h']))
        for f in sub_folders:
            h = f['h']
            if h == root_node_handle:
                continue
            parent_id = gdrive_folder_map.get(f['p'], root_gdrive_id)
            sub_id = create_gdrive_folder(auth, f['name'], parent_id)
            gdrive_folder_map[h] = sub_id

        job['status'] = 'uploading'
        uploaded_bytes = 0
        uploaded_files = 0
        
        for idx, f in enumerate(sub_files, 1):
            job['current_file'] = f['name']
            job['current_index'] = idx
            target_g_folder = gdrive_folder_map.get(f['p'], root_gdrive_id)
            
            dl_req = [{'a': 'g', 'g': 1, 'n': f['h']}]
            r_dl = requests.post(mega_api_url, json=dl_req, timeout=15)
            dl_url = r_dl.json()[0].get('g')
            if not dl_url:
                continue
                
            t0 = time.time()
            # If using residential proxy with session rotation
            file_proxies = proxies
            if effective_proxy and 'sessid' in effective_proxy:
                session_suffix = f"-file{idx}"
                rot_proxy = effective_proxy + session_suffix
                file_proxies = {"http": rot_proxy, "https": rot_proxy}

            enc_bytes = requests.get(dl_url, proxies=file_proxies, timeout=180).content
            
            raw_k = f['raw_k']
            k0 = raw_k[0:16]
            k1 = raw_k[16:32]
            file_key = bytes(a ^ b for a, b in zip(k0, k1))
            iv = raw_k[16:24] + b'\x00' * 8
            ctr = Counter.new(128, initial_value=int.from_bytes(iv, 'big'))
            cipher = AES.new(file_key, AES.MODE_CTR, counter=ctr)
            decrypted = cipher.decrypt(enc_bytes)
            
            mime_type, _ = mimetypes.guess_type(f['name'])
            if not mime_type:
                mime_type = 'application/octet-stream'
                
            upload_bytes_to_gdrive(auth, f['name'], decrypted, mime_type, target_g_folder)
            
            elapsed = max(time.time() - t0, 0.01)
            uploaded_bytes += f['size']
            uploaded_files += 1
            
            job['uploaded_files'] = uploaded_files
            job['uploaded_bytes'] = uploaded_bytes
            job['speed_mb_s'] = round((f['size'] / (1024*1024)) / elapsed, 2)
            job['progress_pct'] = round((uploaded_bytes / max(job['total_bytes'], 1)) * 100, 1)

        job['status'] = 'completed'
        job['completed_at'] = time.time()
        job['duration_seconds'] = round(job['completed_at'] - job['started_at'], 1)
        
    except Exception as e:
        job['status'] = 'failed'
        job['error'] = str(e)

class TriggerRequest(BaseModel):
    url: str
    folder_name: Optional[str] = None
    proxy: Optional[str] = None

@app.post("/api/trigger")
def trigger_clone(req: TriggerRequest, background_tasks: BackgroundTasks):
    job_id = str(uuid.uuid4())[:8]
    JOBS[job_id] = {
        'id': job_id,
        'url': req.url,
        'folder_name': req.folder_name or 'Auto-Detect',
        'proxy_configured': bool(req.proxy or RESIDENTIAL_PROXY_URL),
        'status': 'queued',
        'total_files': 0,
        'uploaded_files': 0,
        'total_bytes': 0,
        'uploaded_bytes': 0,
        'speed_mb_s': 0.0,
        'progress_pct': 0.0,
        'current_file': 'Initializing...',
        'gdrive_public_link': None,
        'created_at': time.time()
    }
    background_tasks.add_task(execute_clone_task, job_id, req.url, req.folder_name, req.proxy)
    return {"job_id": job_id, "status": "queued"}

@app.get("/api/jobs")
def get_jobs():
    return list(JOBS.values())

@app.get("/api/job/{job_id}")
def get_job(job_id: str):
    if job_id not in JOBS:
        raise HTTPException(status_code=404, detail="Job not found")
    return JOBS[job_id]

@app.get("/api/quota")
def quota_endpoint():
    try:
        auth = GDriveAuth()
        return get_drive_quota(auth)
    except Exception as e:
        return {"error": str(e)}

@app.get("/", response_class=HTMLResponse)
def dashboard_html():
    return """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <title>⚡ Cloud Trigger Board: MEGA to Google Drive</title>
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <script src="https://cdn.tailwindcss.com"></script>
  <style>
    body { background-color: #0b0f19; color: #e2e8f0; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; }
    .neon-border { box-shadow: 0 0 15px rgba(59, 130, 246, 0.3); }
    .glass-card { background: rgba(17, 24, 39, 0.85); backdrop-filter: blur(12px); border: 1px solid rgba(255, 255, 255, 0.08); }
  </style>
</head>
<body class="min-h-screen p-6">
  <div class="max-w-5xl mx-auto space-y-6">
    <div class="glass-card rounded-2xl p-6 flex flex-col md:flex-row items-center justify-between gap-4">
      <div>
        <h1 class="text-2xl font-bold bg-gradient-to-r from-blue-400 via-indigo-300 to-emerald-400 bg-clip-text text-transparent">
          ⚡ Cloud-to-Cloud Trigger Board
        </h1>
        <p class="text-xs text-slate-400 mt-1">Direct Datacenter Mirroring: MEGA &rarr; Google Drive (Residential Proxy Bypass Ready)</p>
      </div>
      <div id="quotaBox" class="text-right text-xs bg-slate-900/80 px-4 py-2.5 rounded-xl border border-slate-800">
        <span class="text-slate-400">Loading Drive Quota...</span>
      </div>
    </div>

    <div class="glass-card rounded-2xl p-6 space-y-4 neon-border">
      <h2 class="text-sm font-semibold uppercase tracking-wider text-blue-400">⚡ Launch New Clone Trigger</h2>
      <div class="grid grid-cols-1 md:grid-cols-3 gap-3">
        <div class="md:col-span-2">
          <label class="block text-xs font-medium text-slate-400 mb-1">MEGA / Cloud URL</label>
          <input id="megaUrl" type="text" placeholder="https://mega.nz/folder/..." 
                 class="w-full bg-slate-900 border border-slate-700 rounded-xl px-4 py-2.5 text-sm focus:outline-none focus:border-blue-500 text-slate-100">
        </div>
        <div>
          <label class="block text-xs font-medium text-slate-400 mb-1">Custom Folder Name (Optional)</label>
          <input id="folderName" type="text" placeholder="Auto-detect from source" 
                 class="w-full bg-slate-900 border border-slate-700 rounded-xl px-4 py-2.5 text-sm focus:outline-none focus:border-blue-500 text-slate-100">
        </div>
      </div>
      <div>
        <label class="block text-xs font-medium text-slate-400 mb-1">Residential Proxy Override (Optional, e.g. Oxylabs/BrightData)</label>
        <input id="proxyUrl" type="text" placeholder="http://customer-resi:pass@pr.oxylabs.io:7777 (Leave empty for default)" 
               class="w-full bg-slate-900/60 border border-slate-700 rounded-xl px-4 py-2 text-xs focus:outline-none focus:border-blue-500 text-slate-300">
      </div>
      <div class="flex justify-end pt-2">
        <button onclick="launchTrigger()" class="bg-blue-600 hover:bg-blue-500 text-white font-medium text-sm px-6 py-2.5 rounded-xl transition flex items-center gap-2 shadow-lg shadow-blue-600/30">
          <span>🚀 Trigger Cloud Clone</span>
        </button>
      </div>
    </div>

    <div class="glass-card rounded-2xl p-6 space-y-4">
      <div class="flex items-center justify-between">
        <h2 class="text-sm font-semibold uppercase tracking-wider text-slate-300">Live Transfer Pipelines</h2>
        <span class="text-xs text-slate-500" id="refreshTimer">Auto-refreshing (2s)</span>
      </div>
      <div id="jobsList" class="space-y-3">
        <div class="text-center py-8 text-slate-500 text-sm">No active transfer jobs. Paste a link above to start!</div>
      </div>
    </div>
  </div>

  <script>
    async function loadQuota() {
      try {
        const res = await fetch('/api/quota');
        const data = await res.json();
        if (data.email) {
          document.getElementById('quotaBox').innerHTML = `
            <div class="font-medium text-slate-200">${data.email}</div>
            <div class="text-emerald-400 font-semibold mt-0.5">${data.free_gb} GB Free <span class="text-slate-500 font-normal">/ ${data.limit_gb} GB</span></div>
          `;
        }
      } catch (e) {}
    }

    async function launchTrigger() {
      const url = document.getElementById('megaUrl').value.trim();
      const folder_name = document.getElementById('folderName').value.trim();
      const proxy = document.getElementById('proxyUrl').value.trim();
      if (!url) {
        alert("Please provide a valid MEGA URL");
        return;
      }
      try {
        await fetch('/api/trigger', {
          method: 'POST',
          headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({ url, folder_name: folder_name || null, proxy: proxy || null })
        });
        document.getElementById('megaUrl').value = '';
        document.getElementById('folderName').value = '';
        loadJobs();
      } catch (e) {
        alert("Failed to launch trigger: " + e);
      }
    }

    async function loadJobs() {
      try {
        const res = await fetch('/api/jobs');
        const jobs = await res.json();
        const container = document.getElementById('jobsList');
        if (!jobs || jobs.length === 0) {
          container.innerHTML = '<div class="text-center py-8 text-slate-500 text-sm">No active transfer jobs. Paste a link above to start!</div>';
          return;
        }
        container.innerHTML = jobs.map(j => {
          let badge = '';
          if (j.status === 'completed') badge = '<span class="bg-emerald-500/10 text-emerald-400 text-xs px-2.5 py-1 rounded-full font-medium border border-emerald-500/20">✓ Completed</span>';
          else if (j.status === 'failed') badge = '<span class="bg-red-500/10 text-red-400 text-xs px-2.5 py-1 rounded-full font-medium border border-red-500/20">✕ Failed</span>';
          else badge = `<span class="bg-blue-500/10 text-blue-400 text-xs px-2.5 py-1 rounded-full font-medium border border-blue-500/20 flex items-center gap-1.5"><span class="w-2 h-2 rounded-full bg-blue-400 animate-pulse"></span>${j.status}</span>`;

          const gdriveLink = j.gdrive_public_link ? `
            <a href="${j.gdrive_public_link}" target="_blank" class="text-xs bg-slate-800 hover:bg-slate-700 text-blue-400 px-3 py-1.5 rounded-lg border border-slate-700 flex items-center gap-1 transition">
              📂 Open Google Drive &rarr;
            </a>
          ` : '';

          return `
            <div class="bg-slate-900/90 border border-slate-800 rounded-xl p-4 space-y-3">
              <div class="flex items-center justify-between">
                <div>
                  <div class="font-semibold text-slate-100 text-sm">${j.folder_name}</div>
                  <div class="text-xs text-slate-500 truncate max-w-md">${j.url}</div>
                </div>
                <div class="flex items-center gap-3">
                  ${badge}
                  ${gdriveLink}
                </div>
              </div>

              <div class="w-full bg-slate-800 rounded-full h-2 overflow-hidden">
                <div class="bg-gradient-to-r from-blue-500 to-emerald-400 h-2 transition-all duration-300" style="width: ${j.progress_pct || 0}%"></div>
              </div>

              <div class="flex items-center justify-between text-xs text-slate-400">
                <div>File: <span class="text-slate-200 font-mono">${j.current_file || 'Waiting'}</span> (${j.uploaded_files || 0}/${j.total_files || 0})</div>
                <div class="flex items-center gap-4">
                  <div>${j.speed_mb_s ? j.speed_mb_s + ' MB/s' : ''}</div>
                  <div class="font-medium text-emerald-400">${j.progress_pct || 0}%</div>
                </div>
              </div>
            </div>
          `;
        }).join('');
      } catch (e) {}
    }

    loadQuota();
    loadJobs();
    setInterval(loadJobs, 2000);
    setInterval(loadQuota, 30000);
  </script>
</body>
</html>"""

def run_cli_mode(url: str, custom_name: Optional[str] = None, proxy: Optional[str] = None):
    job_id = str(uuid.uuid4())[:8]
    JOBS[job_id] = {
        'id': job_id, 'url': url, 'folder_name': custom_name or 'Auto-Detect',
        'status': 'queued', 'total_files': 0, 'uploaded_files': 0,
        'total_bytes': 0, 'uploaded_bytes': 0, 'speed_mb_s': 0.0,
        'progress_pct': 0.0, 'current_file': 'Initializing...',
        'gdrive_public_link': None, 'created_at': time.time()
    }
    print(f"[*] Starting Trigger Clone Job [{job_id}] for {url}")
    execute_clone_task(job_id, url, custom_name, proxy)
    j = JOBS[job_id]
    if j['status'] == 'completed':
        print(f"\n[✓] SUCCESS: {j['uploaded_files']} files cloned into Google Drive!")
        print(f"[✓] Link: {j['gdrive_public_link']}")
    else:
        print(f"\n[✕] FAILED: {j.get('error')}")

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description="Cloud Trigger Board")
    parser.add_argument("--url", type=str, help="CLI trigger: MEGA URL to clone immediately")
    parser.add_argument("--name", type=str, help="CLI trigger: Custom folder name")
    parser.add_argument("--proxy", type=str, help="Residential proxy URL (http://user:pass@host:port)")
    parser.add_argument("--port", type=int, default=8899, help="Web dashboard port (default: 8899)")
    args = parser.parse_args()

    if args.url:
        run_cli_mode(args.url, args.name, args.proxy)
    else:
        print("=" * 65)
        print("⚡ CLOUD TRIGGER BOARD DASHBOARD & API ACTIVE")
        print(f"👉 Web Dashboard:  http://localhost:{args.port}")
        print(f"👉 API Docs:       http://localhost:{args.port}/docs")
        print("=" * 65)
        uvicorn.run(app, host="0.0.0.0", port=args.port)
