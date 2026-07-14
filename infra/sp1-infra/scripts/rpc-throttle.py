#!/usr/bin/env python3
"""
rpc-throttle.py — local throttling reverse-proxy for a rate-limited JSON-RPC endpoint (Alchemy).

RSP fires request bursts (mostly eth_getProof) that blow Alchemy's token bucket (free: 500 CU/s,
5000 CU / 10s) -> HTTP 429 -> RSP gives up. This proxy sits in front and runs its OWN token bucket
sized to your tier: when the bucket is empty it makes the caller WAIT (queues) instead of forwarding
a request that would 429. RSP sees latency, never errors.

Wiring:
  1) UPSTREAM=https://eth-mainnet.g.alchemy.com/v2/<KEY> python3 rpc-throttle.py    # terminal A
  2) put  RPC_1=http://localhost:8545  in vendor/rsp/.env, then run block-cycles.py        # terminal B

Tuning (watch the [throttle] CU/s line):
  - still upstream 429  -> raise --default-cu  (or lower --cu-per-sec / --safety)
  - slow with 0 429     -> lower --default-cu
CU costs below are best-effort; eth_getProof (RSP's hot path) uses --default-cu so it's the main knob.
Stdlib only. Binds to localhost (do NOT expose — it forwards your Alchemy key).
"""
import argparse, json, os, sys, threading, time, urllib.request, urllib.error
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# Best-effort Alchemy CU costs for cheap/known methods. Heavy/uncertain ones
# (getProof, getLogs, getBlockReceipts) deliberately fall through to --default-cu.
METHOD_CU = {
    "eth_chainId": 0, "net_version": 0, "eth_blockNumber": 10,
    "eth_getBlockByNumber": 16, "eth_getBlockByHash": 16,
    "eth_getBalance": 19, "eth_getTransactionCount": 26, "eth_getCode": 19,
    "eth_getStorageAt": 17, "eth_call": 26, "eth_getTransactionReceipt": 15,
    "eth_getTransactionByHash": 17, "eth_feeHistory": 17,
}

class Bucket:
    """Blocking token bucket: take(cost) returns only once `cost` tokens are available."""
    def __init__(self, rate, capacity):
        self.rate, self.cap = rate, capacity
        self.tokens, self.t = capacity, time.monotonic()
        self.lock = threading.Lock()
    def take(self, cost):
        cost = min(cost, self.cap)
        while True:
            with self.lock:
                now = time.monotonic()
                self.tokens = min(self.cap, self.tokens + (now - self.t) * self.rate)
                self.t = now
                if self.tokens >= cost:
                    self.tokens -= cost
                    return
                wait = (cost - self.tokens) / self.rate
            time.sleep(min(wait, 1.0))

def cost_of(body, default_cu):
    try:
        obj = json.loads(body)
    except Exception:
        return default_cu
    reqs = obj if isinstance(obj, list) else [obj]
    total = sum(METHOD_CU.get(r.get("method"), default_cu) for r in reqs if isinstance(r, dict))
    return total or default_cu

STATE = {"fwd": 0, "cu": 0, "u429": 0}
SLOCK = threading.Lock()

def make_handler(upstream, bucket, default_cu, sem):
    class H(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        def log_message(self, *a): pass
        def do_POST(self):
            body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
            c = cost_of(body, default_cu)
            bucket.take(c)                       # <-- the throttle (blocks, never rejects)
            with sem:
                try:
                    req = urllib.request.Request(upstream, data=body,
                        headers={"Content-Type": "application/json", "User-Agent": "rpc-throttle/1.0"})
                    with urllib.request.urlopen(req, timeout=60) as r:
                        data, code = r.read(), r.status
                except urllib.error.HTTPError as e:
                    data, code = e.read(), e.code
                    if code == 429:
                        with SLOCK: STATE["u429"] += 1
                except Exception as e:
                    data = json.dumps({"jsonrpc": "2.0", "error": {"code": -32000, "message": str(e)}}).encode()
                    code = 502
            with SLOCK:
                STATE["fwd"] += 1; STATE["cu"] += c
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
    return H

def reporter(interval=10):
    last = dict(STATE)
    while True:
        time.sleep(interval)
        with SLOCK: cur = dict(STATE)
        dfwd, dcu = cur["fwd"] - last["fwd"], cur["cu"] - last["cu"]; last = cur
        print(f"[throttle] {dfwd:>5} req/{interval}s  {dcu/interval:>6.0f} CU/s  "
              f"total={cur['fwd']}  upstream429={cur['u429']}", file=sys.stderr)

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--upstream", default=os.environ.get("UPSTREAM") or os.environ.get("ALCHEMY_URL"))
    ap.add_argument("--port", type=int, default=8545)
    ap.add_argument("--cu-per-sec", type=float, default=500.0, help="your tier's CU/s (free=500)")
    ap.add_argument("--window", type=float, default=10.0, help="bucket window in s (Alchemy=10)")
    ap.add_argument("--safety", type=float, default=0.85, help="use this fraction of the limit (jitter margin)")
    ap.add_argument("--default-cu", type=float, default=30.0, help="CU assumed for getProof & unknown methods")
    ap.add_argument("--max-concurrency", type=int, default=24)
    a = ap.parse_args()
    if not a.upstream: sys.exit("ERROR: set --upstream or UPSTREAM env to your Alchemy URL")
    # footgun guard: if given the whole env line ('RPC_1=https://...'), keep only the URL
    if "://" in a.upstream and "=" in a.upstream.split("://", 1)[0]:
        a.upstream = a.upstream.split("=", 1)[1]
    if not a.upstream.lower().startswith(("http://", "https://")):
        sys.exit(f"ERROR: --upstream must be a bare URL (got {a.upstream!r}). "
                 "Pass the Alchemy URL itself, NOT the 'RPC_1=...' line.")

    rate = a.cu_per_sec * a.safety
    cap  = a.cu_per_sec * a.window * a.safety
    bucket = Bucket(rate, cap)
    sem = threading.Semaphore(a.max_concurrency)
    threading.Thread(target=reporter, daemon=True).start()
    srv = ThreadingHTTPServer(("127.0.0.1", a.port), make_handler(a.upstream, bucket, a.default_cu, sem))
    host = a.upstream.split("/v2/")[0] + "/v2/***" if "/v2/" in a.upstream else a.upstream
    print(f"throttling proxy: http://127.0.0.1:{a.port}  ->  {host}")
    print(f"  budget {rate:.0f} CU/s ({a.cu_per_sec}×{a.safety}), bucket {cap:.0f} CU, default {a.default_cu} CU/req")
    print(f"  -> set  RPC_1=http://localhost:{a.port}  in vendor/rsp/.env")
    try: srv.serve_forever()
    except KeyboardInterrupt: print("\nbye")

if __name__ == "__main__":
    main()
