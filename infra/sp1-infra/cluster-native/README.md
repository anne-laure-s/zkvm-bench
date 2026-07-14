# sp1-cluster — NATIVE (no Docker) on a Vast container with 16 native GPUs

Your Vast instance is itself a Docker container (no nested Docker), but the **GPUs are native**, so
sp1-cluster can run as **plain processes**: redis + postgres (apt) + the cluster binaries
(`api`, `coordinator`, `network-gateway`, `node`×N) extracted from the v2.4.3 images with `skopeo` (daemonless).

> ⚠️ **Experimental.** This reimplements by hand what docker-compose does. First run will likely hit
> issues (shared-lib paths, port/metrics conflicts, postgres auth, GPU driver libs). Every process
> logs to `logs/<name>.log` — we iterate from those. It's the price of no Docker on this box.

> ⚠️ **Box requirement — CUDA ≥ 13.0 (driver ≥ 580).** The v2.4.3 GPU node binary links the CUDA 13.0
> runtime, so the host driver must *natively* support CUDA ≥ 13.0. CUDA forward-compatibility is
> rejected here, so an older driver (e.g. CUDA 12.9 / driver 575) has **no workaround** — pick a box
> whose `nvidia-smi` *CUDA Version* is ≥ 13.0. (Our benchmark boxes ran CUDA 13.2 / driver 595.)

## Order (run on the box, from this folder)

**One-time** (only on a fresh / just-destroyed instance — these persist across stop/start):
```sh
./00-install-once.sh          # apt: redis, postgres, skopeo, zstd
./02-fetch-binaries.sh        # skopeo-pull v2.4.3 images, extract rootfs + binaries
./04-build-runner.sh          # build sp1-runner as the network client (needs ~/sp1-runner)
```
(For `04`, first copy the runner crate from your Mac: `scp -P <port> -r sp1-runner root@ssh.vast.ai:~/sp1-runner`.)

**Every instance start** (processes don't survive a stop; installed packages + rootfs do):
```sh
./boot.sh                     # (re)start redis + postgres
NUM_GPUS=1 ./03-start.sh      # START SMALL: 1 GPU first to validate the wiring
#   ... check logs/coordinator.log + logs/gpu0.log + logs/network-gateway.log ...
./submit.sh ~/rsp.elf ~/1-20500000.bin       # prove (RAW .bin, not .stdin) -> saves proof
# once 1 GPU works:
./stop.sh ; NUM_GPUS=16 ./03-start.sh         # scale to all 16
```

`submit.sh` proves through the **network-gateway** with our `sp1-runner`, so it actually **retrieves
and saves the proof**. The run record `runs/<tag>-<ts>/` contains: `proof.bin`, `pv.bin`, `vkey.txt`,
`report.json` (timings + `proof_bytes`), `prove.log`, plus the cluster-side logs
(`coordinator.log`, `network-gateway.log`, `cpu-node*.log`, `gpu*.log`) and `env.txt`.
⚠️ 2nd arg is the **raw witness `.bin`** (e.g. `../../guests/rsp/inputs/1-20500000.bin`), not the `.stdin` — the
runner wraps it itself. (The old `cli bench` path only timed; it never saved the proof.)

### After a stop → start (data intact, only need to relaunch)
```sh
./boot.sh && NUM_GPUS=16 ./03-start.sh
```
No reinstall, no re-fetch. ⚠️ Only works if the instance can actually restart — use an **on-demand**
instance (an interruptible one may be unrecoverable if the GPU got taken while stopped).

### Add witnesses / swap the guest on the fly
`~/elfs/` (guests) and `~/witnesses/` (blocks) are just writable dirs — add more anytime:
```sh
scp -P <port> ../../guests/rsp/inputs/1-25367437.bin root@<host>:~/witnesses/    # more blocks
scp -P <port> ../../guests/<guest>/<guest>.elf   root@<host>:~/elfs/    # another guest
```
Then submit against whatever you want — **changing the guest needs no cluster restart** (the gateway
runs `PROGRAM_STORE=memory`; the runner ships the ELF with each request):
```sh
./submit.sh ~/elfs/other-guest.elf ~/witnesses/1-25367437.bin
```

## What you copy from your Mac first
```sh
scp -P <port> -r cluster-native root@ssh.vast.ai:~/cluster-native
scp -P <port> ../../guests/rsp/rsp.elf ../../guests/rsp/inputs/1-20500000.bin root@ssh.vast.ai:~/   # RAW .bin (not .stdin)
```

## 2×8 GPU (two 8-GPU instances, when a single 16-GPU box won't schedule)
sp1-cluster is distributed: one **head** runs the control plane + 8 GPUs, one **worker** runs 8 GPUs
and joins the head. Both boxes need the one-time setup (`./00` + `./02`); only the head needs `./04`
(the runner) and `./boot.sh` (redis/postgres). The witnesses + ELF live on the **head** (that's where
you `./submit.sh`).

**Networking — SSH tunnel (recommended: simple, secure, redis stays private):**
On the **worker**, open a background tunnel to the head, then start workers pointing at localhost:
```sh
# worker box — tunnel head's redis+coordinator to the worker's localhost
ssh -fN -L 6379:localhost:6379 -L 50052:localhost:50052 -p <HEAD_SSH_PORT> root@<HEAD_SSH_HOST>
```
```sh
# HEAD box
./boot.sh
ROLE=head NUM_GPUS=8 RUST_LOG=warn ./03-start.sh
```
```sh
# WORKER box (after the tunnel is up; HEAD_ADDR defaults to localhost = the tunnel)
ROLE=worker NUM_GPUS=8 RUST_LOG=warn ./03-start.sh
```
On the head, `tail -f logs/coordinator.log` should now show **16** gpu_workers. Then `./submit.sh` on the head.

**Networking — direct (no tunnel):** expose the head's `6379`+`50052` and set a redis password.
```sh
# HEAD:    REDIS_BIND=0.0.0.0 REDIS_PASSWORD=<pass> ./boot.sh
#          ROLE=head NUM_GPUS=8 ./03-start.sh
# WORKER:  ROLE=worker NUM_GPUS=8 HEAD_ADDR=<head-ip> COORD_PORT=<ext> REDIS_PORT=<ext> REDIS_PASSWORD=<pass> ./03-start.sh
```
⚠️ Cross-box network carries every shard artifact (redis). Use two instances in the **same Vast region**
or the inter-box latency/bandwidth will eat the GPU gains. `./03-start.sh` (worker) fails fast if it
can't reach the head.

## Ports (native, de-conflicted — they'd collide otherwise)
- redis `6379`, postgres `5432`, api gRPC `50051`, **coordinator `50052`** (≠ api), api http `3000`,
  network-gateway gRPC `50061` / http `8081` (what `submit.sh` proves through).
- The CLI talks to the **api** (`CLI_CLUSTER_RPC=http://localhost:50051`); workers talk to the
  **coordinator** (`http://localhost:50052`).
- **Metrics ports** (each process binds its own — natively they'd otherwise all collide on 9090):
  coordinator `9090`, cpu-nodes `9101+`, gpu-nodes `9200+` (set via `WORKER_METRICS_ADDR`).

## Likely first failures (and where to look)
- `02` **skopeo 401/403** → the images need auth: `skopeo login ghcr.io` (or add
  `--src-creds <ghuser>:<token>` to the `skopeo copy` lines). They're normally public, so this
  shouldn't happen — but if it does, that's the fix.
- `logs/api.log` — postgres connection/auth (the script sets a password + auto-migrate).
- `logs/gpu0.log` — missing CUDA/driver libs → we extend the library path (host `libcuda` + image libs).
- `logs/coordinator.log` — should show workers registering. If none register, it's a worker crash.
- Port already in use → another process bound it; `./stop.sh` then retry.
