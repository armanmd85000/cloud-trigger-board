import base64
import json
import mimetypes
import os
import sys
import time
import requests
from Crypto.Cipher import AES
from Crypto.Util import Counter

# Load credentials from config.py or environment variables
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

ROOT_GDRIVE_FOLDER_ID = os.environ.get('ROOT_GDRIVE_FOLDER_ID', '1WzQ9aH9GuCtVtTsATx948cojXBg4iQd8')
MEGA_FOLDER_ID = os.environ.get('MEGA_FOLDER_ID', 'PfQXVbib')
MEGA_FOLDER_KEY = os.environ.get('MEGA_FOLDER_KEY', 'wMOtLNbUsFRDD-8bmuNQ0w')
TARGET_SUBFOLDER_HANDLE = os.environ.get('TARGET_SUBFOLDER_HANDLE', 'GWRiybaI')

def base64_urldecode(data):
    data = data.replace('-', '+').replace('_', '/')
    data += '=' * (-len(data) % 4)
    return base64.b64decode(data)

def decrypt_attr(attr_b64, key_bytes):
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

def decrypt_node_key(k_str, master_key_bytes):
    parts = k_str.split(':')
    enc_k_b64 = parts[-1]
    enc_k = base64_urldecode(enc_k_b64)
    cipher = AES.new(master_key_bytes, AES.MODE_ECB)
    dec_k = cipher.decrypt(enc_k)
    return dec_k

class GDriveAuth:
    def __init__(self):
        self.access_token = None
        self.expires_at = 0
        self.refresh()

    def refresh(self):
        resp = requests.post('https://oauth2.googleapis.com/token', data={
            'client_id': CLIENT_ID,
            'client_secret': CLIENT_SECRET,
            'refresh_token': REFRESH_TOKEN,
            'grant_type': 'refresh_token'
        }, timeout=15)
        data = resp.json()
        self.access_token = data['access_token']
        self.expires_at = time.time() + data.get('expires_in', 3600) - 120
        return self.access_token

    def get_token(self):
        if time.time() >= self.expires_at:
            self.refresh()
        return self.access_token

def get_existing_files_in_folder(auth, parent_id):
    headers = {'Authorization': f'Bearer {auth.get_token()}'}
    q = f"'{parent_id}' in parents and trashed = false"
    files = {}
    page_token = None
    while True:
        params = {'q': q, 'fields': 'nextPageToken, files(id, name, size)', 'pageSize': 100}
        if page_token:
            params['pageToken'] = page_token
        r = requests.get('https://www.googleapis.com/drive/v3/files', headers=headers, params=params, timeout=15)
        res = r.json()
        for f in res.get('files', []):
            files[f['name']] = f['id']
        page_token = res.get('nextPageToken')
        if not page_token:
            break
    return files

def create_gdrive_folder(auth, name, parent_id):
    headers = {
        'Authorization': f'Bearer {auth.get_token()}',
        'Content-Type': 'application/json'
    }
    q = f"'{parent_id}' in parents and name = '{name}' and mimeType = 'application/vnd.google-apps.folder' and trashed = false"
    r = requests.get('https://www.googleapis.com/drive/v3/files', headers=headers, params={'q': q, 'fields': 'files(id, name)'}, timeout=15)
    existing = r.json().get('files', [])
    if existing:
        return existing[0]['id']

    meta = {
        'name': name,
        'mimeType': 'application/vnd.google-apps.folder',
        'parents': [parent_id]
    }
    r = requests.post('https://www.googleapis.com/drive/v3/files', headers=headers, json=meta, timeout=15)
    return r.json()['id']

def upload_bytes_to_gdrive(auth, name, data_bytes, mime_type, parent_id):
    size = len(data_bytes)
    headers = {
        'Authorization': f'Bearer {auth.get_token()}',
        'Content-Type': 'application/json; charset=UTF-8',
        'X-Upload-Content-Type': mime_type,
        'X-Upload-Content-Length': str(size)
    }
    meta = {
        'name': name,
        'parents': [parent_id]
    }
    init_url = 'https://www.googleapis.com/upload/drive/v3/files?uploadType=resumable'
    r_init = requests.post(init_url, headers=headers, json=meta, timeout=20)
    if r_init.status_code != 200:
        raise Exception(f"Init upload failed: {r_init.status_code} {r_init.text}")

    upload_url = r_init.headers['Location']
    upload_headers = {
        'Content-Length': str(size),
        'Content-Type': mime_type
    }
    r_upload = requests.put(upload_url, headers=upload_headers, data=data_bytes, timeout=180)
    if r_upload.status_code not in (200, 201):
        raise Exception(f"Upload chunk failed: {r_upload.status_code} {r_upload.text}")
    return r_upload.json()['id']

def main():
    print("=" * 70, flush=True)
    print("⚡ MEGA TO GOOGLE DRIVE CLOUD-TO-CLOUD STREAMING ENGINE", flush=True)
    print("=" * 70, flush=True)

    auth = GDriveAuth()
    print("✓ Google Drive Authentication verified.", flush=True)

    master_key = base64_urldecode(MEGA_FOLDER_KEY)
    mega_api_url = f'https://g.api.mega.co.nz/cs?id=0&n={MEGA_FOLDER_ID}'
    r = requests.post(mega_api_url, json=[{'a': 'f', 'c': 1, 'r': 1, 'ca': 1}], timeout=20)
    nodes = r.json()[0]['f']
    print(f"✓ Fetched {len(nodes)} total nodes from MEGA API.", flush=True)

    # Parse nodes
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
            'h': h,
            'p': p,
            't': t,
            'size': s,
            'name': name,
            'raw_k': raw_k,
            'key': file_key
        }

    # Filter descendants of target subfolder
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
        if is_descendant(h, TARGET_SUBFOLDER_HANDLE):
            if n['t'] == 1:
                sub_folders.append(n)
            else:
                sub_files.append(n)

    print(f"✓ Target Subfolder contains {len(sub_folders)-1} subdirectories and {len(sub_files)} files.", flush=True)
    total_bytes = sum(f['size'] for f in sub_files)
    print(f"✓ Total Payload: {total_bytes / (1024*1024):.2f} MB ({total_bytes / (1024*1024*1024):.2f} GB)\n", flush=True)

    # Map MEGA folder handle to GDrive folder ID
    gdrive_folder_map = {TARGET_SUBFOLDER_HANDLE: ROOT_GDRIVE_FOLDER_ID}

    def get_depth(h):
        d = 0
        curr = h
        while curr in nodes_by_h and curr != TARGET_SUBFOLDER_HANDLE:
            d += 1
            curr = nodes_by_h[curr]['p']
        return d

    sub_folders.sort(key=lambda x: get_depth(x['h']))

    print("📁 Creating/Verifying Google Drive folder hierarchy...", flush=True)
    for f in sub_folders:
        h = f['h']
        if h == TARGET_SUBFOLDER_HANDLE:
            continue
        p = f['p']
        parent_gdrive_id = gdrive_folder_map.get(p, ROOT_GDRIVE_FOLDER_ID)
        g_id = create_gdrive_folder(auth, f['name'], parent_gdrive_id)
        gdrive_folder_map[h] = g_id
        print(f"  [DIR] {f['name']} -> GDrive ID: {g_id}", flush=True)

    # Pre-cache existing files per folder to enable resume
    print("\n🔍 Checking existing files in Google Drive for resume capability...", flush=True)
    existing_by_folder = {}
    for h, g_id in gdrive_folder_map.items():
        existing_by_folder[g_id] = get_existing_files_in_folder(auth, g_id)

    print("\n🚀 Beginning high-speed streaming upload of 66 files...", flush=True)
    manifest = []
    total_uploaded_bytes = 0
    start_time_all = time.time()

    for idx, f in enumerate(sub_files, 1):
        filename = f['name']
        size = f['size']
        h = f['h']
        p = f['p']
        raw_k = f['raw_k']
        target_folder_id = gdrive_folder_map.get(p, ROOT_GDRIVE_FOLDER_ID)

        # Check if already uploaded
        folder_existing = existing_by_folder.get(target_folder_id, {})
        if filename in folder_existing:
            print(f"[{idx:02d}/{len(sub_files):02d}] {filename} ({size / (1024*1024):.2f} MB)... [ALREADY UPLOADED - SKIPPED]", flush=True)
            manifest.append({
                'name': filename,
                'size': size,
                'gdrive_id': folder_existing[filename],
                'status': 'existing'
            })
            total_uploaded_bytes += size
            continue

        mime_type, _ = mimetypes.guess_type(filename)
        if not mime_type:
            mime_type = 'application/octet-stream'

        print(f"[{idx:02d}/{len(sub_files):02d}] {filename} ({size / (1024*1024):.2f} MB)...", end=" ", flush=True)
        t0 = time.time()

        # Step 1: Request MEGA download URL
        dl_req = [{'a': 'g', 'g': 1, 'n': h}]
        r_dl = requests.post(mega_api_url, json=dl_req, timeout=15)
        dl_info = r_dl.json()[0]
        dl_url = dl_info.get('g')
        if not dl_url:
            print("ERROR: Could not get download URL", flush=True)
            continue

        # Step 2: Download encrypted stream
        enc_bytes = requests.get(dl_url, timeout=120).content

        # Step 3: Decrypt with AES-CTR
        k0 = raw_k[0:16]
        k1 = raw_k[16:32]
        file_key = bytes(a ^ b for a, b in zip(k0, k1))
        iv = raw_k[16:24] + b'\x00' * 8
        ctr = Counter.new(128, initial_value=int.from_bytes(iv, 'big'))
        cipher = AES.new(file_key, AES.MODE_CTR, counter=ctr)
        decrypted_bytes = cipher.decrypt(enc_bytes)

        # Step 4: Upload to Google Drive
        try:
            g_file_id = upload_bytes_to_gdrive(auth, filename, decrypted_bytes, mime_type, target_folder_id)
            elapsed = time.time() - t0
            speed_mb = (size / (1024*1024)) / max(elapsed, 0.01)
            total_uploaded_bytes += size
            print(f"DONE in {elapsed:.1f}s ({speed_mb:.1f} MB/s)", flush=True)
            manifest.append({
                'name': filename,
                'size': size,
                'gdrive_id': g_file_id,
                'status': 'uploaded',
                'time_seconds': round(elapsed, 2)
            })
            # Cache it
            folder_existing[filename] = g_file_id
        except Exception as e:
            print(f"FAILED: {e}", flush=True)

    total_time = time.time() - start_time_all
    avg_speed = (total_uploaded_bytes / (1024*1024)) / max(total_time, 0.01)

    print("\n" + "=" * 70, flush=True)
    print("🎉 TRANSFER COMPLETE!", flush=True)
    print(f"Total Files Handled:  {len(manifest)} / {len(sub_files)}", flush=True)
    print(f"Total Volume:         {total_uploaded_bytes / (1024*1024):.2f} MB", flush=True)
    print(f"Total Elapsed Time:   {total_time:.1f} seconds (~{total_time/60:.1f} mins)", flush=True)
    print(f"Average Speed:        {avg_speed:.2f} MB/s", flush=True)
    print(f"Public Google Drive:  https://drive.google.com/drive/folders/{ROOT_GDRIVE_FOLDER_ID}", flush=True)
    print("=" * 70, flush=True)

    # Save manifest
    with open(r'C:\Users\muska\.gemini\antigravity\scratch\MEGA_GDRIVE_TRANSFER_MANIFEST.json', 'w', encoding='utf-8') as mf:
        json.dump({
            'course': '[MVP] PYNK Society - AI Creative Lab',
            'root_folder_id': ROOT_GDRIVE_FOLDER_ID,
            'public_link': f'https://drive.google.com/drive/folders/{ROOT_GDRIVE_FOLDER_ID}',
            'total_files': len(manifest),
            'total_bytes': total_uploaded_bytes,
            'files': manifest
        }, mf, indent=2)

if __name__ == '__main__':
    main()
