# /// script
# requires-python = ">=3.10"
# dependencies = ["requests"]
# ///
"""shoestring-canary -- measure Akash provider reliability with real leases.

Each run places one REAL, paid deployment, measures whether the workload
actually becomes reachable (and how fast the box crunches a small CPU
benchmark), then closes the lease. Not self-reported uptime -- an actual
test of the marketplace, a few cents per run.

Census design: the marketplace's GPU corner is a village (six providers in
our pilot), but the network claims ~1,800 registered providers. Profiles
widen the sample beyond GPUs:

  gpu        1 GPU (any of the common models) -- the original probe
  cpu-micro  0.5 CPU / 512Mi -- the universal bid; nearly every live
             provider can serve it, so its BID LIST is a census of who is
             actually alive, and its leases test the long tail for cents
  cpu-heavy  8 CPU / 32Gi -- tests real capacity claims
  gpu-pro6000  1x pro6000se (96GB) sized like a real Flash-Next serve
             (8 CPU / 64Gi / 150Gi). MANUAL ONLY (--profile gpu-pro6000):
             excluded from auto-cycle and sweep, since its leases cost
             ~$2/hr. Note Console API injects signed_by any_of
             [akash1365yvmc4...] on every order, so it only reaches
             providers signed by that auditor.

Every run also appends EVERY bid received to data/bids.csv (provider,
price, profile, chosen) -- the marketplace census comes free with each
probe, no lease required per data point.

Selection: cheapest bid wins by default. With probability --explore-p the
run instead tests the LEAST-RECENTLY-TESTED bidder (coverage-driven, not
random: uniform random re-tests the same few forever; least-recently
walks the whole tail). Every row records its mode.

NO DATABASE REQUIRED. Rows append to local CSVs (data/canary.csv +
data/bids.csv). Optionally set SUPABASE_URL/SUPABASE_KEY (any PostgREST
endpoint) to mirror lease rows to the table named by CANARY_TABLE
(default shoestring_canary):

    CREATE TABLE shoestring_canary (
        id BIGSERIAL PRIMARY KEY,
        ts TIMESTAMPTZ NOT NULL,
        dseq TEXT,
        profile TEXT,
        provider_owner TEXT NOT NULL,
        gpu_model TEXT,
        mode TEXT,
        bid_price_uact_per_block REAL,
        reachable BOOLEAN NOT NULL,
        time_to_ready_seconds REAL,
        bench_seconds REAL,
        failure_reason TEXT
    );

Usage:
    export AKASH_API_KEY=...      # console.akash.network
    ./canary.py run                       # auto: cycles profiles
    ./canary.py run --profile cpu-micro   # pin a profile
    ./canary.py sweep                     # one run of every profile

Cron (6x/day cycles all profiles every 12h):
    17 */4 * * *  cd /path/to/shoestring && ./canary.py run
"""
import argparse
import csv
import os
import random
import sys
import time
from datetime import datetime, timezone

import requests

import shoestring as ss

# The workload: crunch a small CPU benchmark (sha256 over ~6.5GB), publish
# the timing, then serve it. Reachability probe = fetching the number.
# No quotes that would fight three layers of YAML/shell escaping.
_WORKLOAD = (
    "cd /tmp && "
    "python3 -c 'import hashlib,time;t=time.time();"
    "[hashlib.sha256(bytes(65536)).digest() for i in range(100000)];"
    "print(round(time.time()-t,3))' > bench.txt && "
    "python3 -m http.server 80"
)

_SDL = """---
version: "2.0"
services:
  web:
    image: python:3.11-slim
    command: ["/bin/bash", "-c"]
    args: ["{workload}"]
    expose:
      - port: 80
        as: 80
        to:
          - global: true
profiles:
  compute:
    web:
      resources:
{resources}
  placement:
    akash:
      pricing:
        web:
          denom: uact
          amount: {price}
deployment:
  web:
    akash:
      profile: web
      count: 1
"""

_GPU_MODELS = ["a100", "h100", "h200", "rtx4090", "rtx3090", "rtx5090",
               "rtx6000", "pro6000se", "v100", "t4"]

PROFILES = {
    "gpu": (
        "        cpu:\n          units: 1\n"
        "        memory:\n          size: 1Gi\n"
        "        storage:\n          size: 2Gi\n"
        "        gpu:\n          units: 1\n"
        "          attributes:\n            vendor:\n              nvidia:\n"
        + "".join(f"                - model: {m}\n" for m in _GPU_MODELS)
    ),
    "cpu-micro": (
        "        cpu:\n          units: 0.5\n"
        "        memory:\n          size: 512Mi\n"
        "        storage:\n          size: 1Gi\n"
    ),
    "cpu-heavy": (
        "        cpu:\n          units: 8\n"
        "        memory:\n          size: 32Gi\n"
        "        storage:\n          size: 10Gi\n"
    ),
    # Price discovery for a real Flash-Next serve (UD-IQ4_XS is 87 GiB):
    # providers price CPU/RAM/disk too, so request what the serve needs.
    "gpu-pro6000": (
        "        cpu:\n          units: 8\n"
        "        memory:\n          size: 64Gi\n"
        "        storage:\n          size: 150Gi\n"
        "        gpu:\n          units: 1\n"
        "          attributes:\n            vendor:\n              nvidia:\n"
        "                - model: pro6000se\n"
    ),
}

# Profiles the cron auto-cycle and `sweep` rotate through. Pinned-only
# profiles (expensive leases) stay out so a schedule can't run them.
AUTO_PROFILES = ["gpu", "cpu-micro", "cpu-heavy"]

FIELDS = ["ts", "dseq", "profile", "provider_owner", "gpu_model", "mode",
          "bid_price_uact_per_block", "reachable", "time_to_ready_seconds",
          "bench_seconds", "failure_reason", "endpoint"]
BID_FIELDS = ["ts", "profile", "provider", "gpu_model",
              "bid_price_uact_per_block", "chosen"]


def _append(path, fields, rows):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    new = not os.path.exists(path)
    if not new:
        with open(path) as f:
            header = f.readline().strip().split(",")
        if header != fields and set(header) < set(fields):
            # schema grew: rewrite with new columns, old rows padded empty
            old = list(csv.DictReader(open(path)))
            with open(path, "w", newline="") as f:
                w = csv.DictWriter(f, fieldnames=fields)
                w.writeheader()
                for r in old:
                    w.writerow({k: r.get(k, "") for k in fields})
    with open(path, "a", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        if new:
            w.writeheader()
        for r in rows:
            w.writerow(r)


def _mirror(row):
    url, key = os.environ.get("SUPABASE_URL"), os.environ.get("SUPABASE_KEY")
    if not (url and key):
        return
    table = os.environ.get("CANARY_TABLE", "shoestring_canary")
    try:
        requests.post(
            f"{url.rstrip('/')}/rest/v1/{table}",
            headers={"apikey": key, "Authorization": f"Bearer {key}",
                     "Content-Type": "application/json",
                     "Prefer": "return=minimal"},
            json=row, timeout=30,
        ).raise_for_status()
    except requests.RequestException as e:
        print(f"[canary] warning: DB mirror failed ({e}); CSV row is safe")


def _last_tested(out_path):
    """provider -> ts of last lease we placed with them, from history."""
    seen = {}
    if os.path.exists(out_path):
        for r in csv.DictReader(open(out_path)):
            seen[r["provider_owner"]] = r["ts"]
    return seen


def _pick_profile(out_path):
    n = 0
    if os.path.exists(out_path):
        n = sum(1 for _ in open(out_path)) - 1
    return AUTO_PROFILES[n % len(AUTO_PROFILES)]


def run_one(profile, args):
    dseq = provider = gpu_model = mode = None
    bid_price = ttr = bench = None
    reachable = False
    failure_reason = None
    run_endpoint = [None]  # captured for write-time attribution
    start = time.time()
    ts = datetime.now(timezone.utc).isoformat()

    try:
        sdl = _SDL.format(workload=_WORKLOAD, resources=PROFILES[profile],
                          price=args.max_price)
        dseq, manifest = ss.create_deployment(sdl, deposit_usd=5)

        deadline = time.time() + args.bid_wait
        bids = []
        while time.time() < deadline and not bids:
            time.sleep(10)
            bids = ss.get_bids(dseq)
        if not bids:
            failure_reason = "no_bids"
            return

        ranked = sorted(bids, key=lambda b: float(b["bid"]["price"]["amount"]))
        # census sidecar: every bid is a data point, leased or not
        bid_rows = [{
            "ts": ts, "profile": profile,
            "provider": b["bid"]["id"]["provider"],
            "gpu_model": ss.extract_gpu_model(b),
            "bid_price_uact_per_block": float(b["bid"]["price"]["amount"]),
            "chosen": False,
        } for b in ranked]

        # Per-provider cooldown (GPU profile only): a probe reserves the
        # whole GPU for minutes, so no provider gets GPU-probed more than
        # once per 24h. Raised by a provider in review -- fair point.
        seen = _last_tested(args.out)
        pool = ranked
        if profile.startswith("gpu"):
            cutoff = datetime.now(timezone.utc).timestamp() - 24 * 3600
            def fresh(b):
                ts_prev = seen.get(b["bid"]["id"]["provider"])
                if not ts_prev:
                    return True
                return datetime.fromisoformat(ts_prev).timestamp() < cutoff
            eligible = [b for b in ranked if fresh(b)]
            if eligible:
                pool = eligible
        if len(pool) > 1 and random.random() < args.explore_p:
            # coverage-driven: least-recently-tested non-cheapest bidder
            chosen = min(pool[1:],
                         key=lambda b: seen.get(b["bid"]["id"]["provider"], ""))
            mode = "coverage"
        else:
            chosen, mode = pool[0], "cheapest"
        provider = chosen["bid"]["id"]["provider"]
        bid_price = float(chosen["bid"]["price"]["amount"])
        gpu_model = ss.extract_gpu_model(chosen)
        for br in bid_rows:
            if br["provider"] == provider:
                br["chosen"] = True
        _append(args.bids_out, BID_FIELDS, bid_rows)

        ss.accept_bid(dseq, manifest, provider)

        endpoint = ss.get_endpoint(dseq, timeout_s=90)
        run_endpoint[0] = endpoint
        if not endpoint:
            failure_reason = "no_endpoint"
            return

        deadline = time.time() + 150
        while time.time() < deadline:
            try:
                r = requests.get(f"{endpoint}/bench.txt", timeout=8)
                if r.status_code == 200:
                    bench = float(r.text.strip())
                    reachable = True
                    ttr = round(time.time() - start, 1)
                    break
            except (requests.RequestException, ValueError):
                pass
            time.sleep(10)
        if not reachable:
            failure_reason = failure_reason or "never_reachable"

    except Exception as e:
        failure_reason = f"exception:{type(e).__name__}:{e}"[:200]
    finally:
        if dseq:
            ss.close_deployment(dseq)

    row = {
        "ts": ts, "dseq": dseq, "profile": profile,
        "provider_owner": provider or "none", "gpu_model": gpu_model,
        "mode": mode, "bid_price_uact_per_block": bid_price,
        "reachable": reachable, "time_to_ready_seconds": ttr,
        "bench_seconds": bench, "failure_reason": failure_reason,
        "endpoint": run_endpoint[0],
    }
    _append(args.out, FIELDS, [row])
    _mirror(row)
    print(f"[canary] profile={profile} dseq={dseq} provider={provider} "
          f"mode={mode} gpu={gpu_model} reachable={reachable} ttr={ttr} "
          f"bench={bench}s reason={failure_reason}")


def main():
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    def common(p):
        p.add_argument("--explore-p", type=float, default=0.5)
        p.add_argument("--bid-wait", type=int, default=60)
        p.add_argument("--max-price", type=int, default=20000)
        p.add_argument("--out", default="data/canary.csv")
        p.add_argument("--bids-out", default="data/bids.csv")

    r = sub.add_parser("run", help="one probe (auto-cycles profiles)")
    r.add_argument("--profile", choices=list(PROFILES), default=None)
    common(r)
    s = sub.add_parser("sweep", help="one probe of every auto-cycle profile")
    common(s)
    args = ap.parse_args()

    if args.cmd == "run":
        run_one(args.profile or _pick_profile(args.out), args)
    else:
        for p in AUTO_PROFILES:
            run_one(p, args)


if __name__ == "__main__":
    main()
