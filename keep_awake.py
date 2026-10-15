import time
import urllib.request
import urllib.error
from datetime import datetime

TARGET_URL = "https://universal-file-converter-xmxl.onrender.com/api/files"
INTERVAL_SECONDS = 600  # 10 minutes (Render sleeps after 15 minutes of inactivity)
USER_AGENT = "UniversalConverterKeepAwake/1.0"


def ping_server():
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    req = urllib.request.Request(
        TARGET_URL,
        headers={"User-Agent": USER_AGENT},
        method="GET"
    )
    start_time = time.time()
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            elapsed = time.time() - start_time
            print(f"[{timestamp}] PING SUCCESS -> Status: {resp.status} ({elapsed:.2f}s) | {TARGET_URL}", flush=True)
            return True, resp.status
    except urllib.error.HTTPError as e:
        elapsed = time.time() - start_time
        print(f"[{timestamp}] PING HTTP {e.code} ({elapsed:.2f}s) | Reason: {e.reason}", flush=True)
        return False, e.code
    except Exception as e:
        elapsed = time.time() - start_time
        print(f"[{timestamp}] PING ERROR ({elapsed:.2f}s) | Error: {e}", flush=True)
        return False, str(e)


def main():
    print(f"============================================================")
    print(f" Render Keep-Awake Anti-Hibernation Daemon Initialized")
    print(f" Target: {TARGET_URL}")
    print(f" Interval: {INTERVAL_SECONDS} seconds (every 10 minutes)")
    print(f"============================================================", flush=True)

    # Initial ping on launch
    ping_server()

    # Loop forever
    while True:
        time.sleep(INTERVAL_SECONDS)
        ping_server()


if __name__ == "__main__":
    main()
