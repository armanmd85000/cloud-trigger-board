# ⚡ Cloud Trigger Board & MEGA-to-Google Drive Mirroring Engine

> **High-speed, cloud-to-cloud automated cloner and trigger dashboard for transferring large directory structures from MEGA to Google Drive with on-the-fly AES-128-CTR decryption, zero local disk consumption, and residential proxy limit-bypass.**

---

## 🌟 Features

- **Direct In-Memory Streaming**: Never touches local disk storage. Chunks are downloaded, decrypted, and streamed straight into Google Drive via the official Resumable Upload API.
- **Full Folder Tree Preservation**: Recreates 1:1 nested folder hierarchies on Google Drive, preserving all parent-child relationships and folder names.
- **Automated Public Link Sharing**: Automatically assigns `anyoneWithLink -> Viewer` permissions to the generated root folder, delivering instant shareable links.
- **Web Trigger Dashboard**: Modern, responsive dark-mode UI with live progress bars, transfer speeds (MB/s), file counters, and Google Drive quota monitor.
- **REST API & CLI**: Trigger clones programmatically via `POST /api/trigger` or via terminal `python cloud_trigger_board.py --url "..."`.
- **Residential Proxy Integration**: Seamless routing of MEGA CDN streams through rotating residential proxies to bypass bandwidth quotas.

---

## 📊 Bottlenecks, Limits & How to Overcome Them

When streaming large files between cloud providers, three major limits and bottlenecks come into play:

```
┌──────────────────────────────────────────────────────────────────────────────────────────────────┐
│                                CLOUD-TO-CLOUD LIMITS MATRIX                                      │
├───────────────────┬───────────────────────────────┬──────────────────────────────────────────────┤
│ Provider          │ Constraint / Bottleneck       │ Solution / Bypass Strategy                   │
├───────────────────┼───────────────────────────────┼──────────────────────────────────────────────┤
│ MEGA (Source)     │ ~5 GB per 6 hours per IP      │ Rotating Residential Proxies (Oxylabs)       │
│ MEGA (CDN)        │ Max 6-8 concurrent TCP streams│ Concurrency throttling & sticky session IDs  │
│ Google Drive      │ 750 GB per user per 24 hours  │ Google Workspace Service Account Pools       │
│ Google Drive API  │ 10 QPS (1,000 req / 100s)     │ Exponential backoff with randomized jitter   │
│ Network / Compute │ Local RAM & Bandwidth         │ Cloud VPS (UpCloud / AWS / Hetzner 1-10 Gbps)│
└───────────────────┴───────────────────────────────┴──────────────────────────────────────────────┘
```

---

### 1. MEGA Download Limits & Residential Proxy Bypass

#### The Problem:
* MEGA enforces an IP-based transfer quota for free/anonymous downloads: typically **~5 GB per 6 hours per IP address**.
* Once your IP reaches this threshold, MEGA CDN endpoints (`gfs*.us/gfs*.eu`) return **HTTP 509 Bandwidth Limit Exceeded** or error code `-17` (`ETRANSFERLIMIT`).

#### How to Overcome It:
MEGA does not enforce limits on the account or folder level—it strictly tracks the **client's egress IP address** contacting its CDN nodes. By routing the download stream through a **residential proxy fleet** (e.g. Oxylabs, Bright Data, Smartproxy), every chunk or file request originates from a unique, authentic residential ISP address:

1. **Sticky Sessions for Single Large Files**:
   * For files larger than 100 MB, use a sticky session parameter (e.g. `customer-resi-cc-us-sessid-XXXXX`) to prevent TCP disconnection mid-stream.
2. **Fresh IP Rotation Between Files**:
   * Generate a fresh session ID for each new file:
   ```python
   # Oxylabs Residential Proxy Example
   session_id = f"sess_{int(time.time())}_{f['h']}"
   proxy_url = f"http://customer-resi-cc-us-sessid-{session_id}:PASSWORD@pr.oxylabs.io:7777"
   proxies = {"https": proxy_url, "http": proxy_url}
   
   # Download through residential proxy
   enc_bytes = requests.get(dl_url, proxies=proxies, timeout=180).content
   ```
3. **Result**: The 5 GB / 6 hr quota is **completely eliminated**. You can transfer 50 GB, 500 GB, or multiple terabytes continuously without ever encountering HTTP 509.

---

### 2. Google Drive 750 GB/Day Upload Limit

#### The Problem:
* Google Drive enforces a strict limit: **750 GB of upload per Google account per rolling 24-hour window**.
* Once 750 GB is reached, uploads fail with `HTTP 403: userRateLimitExceeded` or `dailyLimitExceeded`.

#### How to Overcome It:
1. **Google Workspace Shared Drives + Service Account Pool**:
   * Google Shared Drives have no individual daily upload limit; the limit is per *identity* (User or Service Account).
   * Create 5–10 Google Cloud Service Accounts in your GCP project.
   * Add all Service Accounts as `Content Manager` to the destination Google Shared Drive.
   * Rotate Service Account credentials whenever an account approaches ~700 GB or returns a 403 daily quota error:
   ```python
   # Service Account Rotation
   sa_pool = ["sa1.json", "sa2.json", "sa3.json", "sa4.json"]
   # 4 Service Accounts = 4 x 750 GB = 3,000 GB (3 TB) per day!
   ```
2. **Exponential Backoff on Google Drive API QPS**:
   * When creating dozens of folders or files, Google Drive may rate-limit with `403 rateLimitExceeded`.
   * Standard mitigation:
   ```python
   for attempt in range(5):
       try:
           r = requests.post(url, headers=headers, json=payload)
           if r.status_code == 200: break
           if r.status_code in (403, 429, 500, 503):
               time.sleep((2 ** attempt) + random.uniform(0.5, 1.5))
       except requests.exceptions.RequestException:
           time.sleep(2 ** attempt)
   ```

---

## 🚀 Quickstart

### Prerequisites
- Python 3.10+
- Google Cloud OAuth credentials (Client ID, Client Secret, Refresh Token)

### Installation
```bash
git clone https://github.com/armanmd85000/cloud-trigger-board.git
cd cloud-trigger-board
pip install -r requirements.txt
```

### Configuration
Copy `config.example.py` to `config.py` and set your credentials:
```python
GDRIVE_CLIENT_ID = "your-client-id.apps.googleusercontent.com"
GDRIVE_CLIENT_SECRET = "GOCSPX-your-client-secret"
GDRIVE_REFRESH_TOKEN = "1//your-refresh-token"

# Optional Residential Proxy (e.g. Oxylabs)
RESIDENTIAL_PROXY_URL = "http://customer-resi:pass@pr.oxylabs.io:7777"
```

### Running the Trigger Board
```bash
python cloud_trigger_board.py --port 8899
```
* **Web UI Dashboard**: Open `http://localhost:8899` in your browser.
* **API Documentation**: Open `http://localhost:8899/docs`.

### CLI Trigger Mode
```bash
python cloud_trigger_board.py --url "https://mega.nz/folder/PfQXVbib#wMOtLNbUsFRDD-8bmuNQ0w/folder/GWRiybaI"
```

---

## 🛠️ UpCloud VPS Deployment

To run 24/7 on an UpCloud Ubuntu VPS (`209.151.147.187`):
```bash
chmod +x setup_vps_trigger_board.sh
sudo ./setup_vps_trigger_board.sh
```
This automatically configures a systemd daemon (`triggerboard.service`) that restarts on system reboot.

---

## 📄 License
MIT License. Built for high-speed cloud infrastructure and automation workflows.
