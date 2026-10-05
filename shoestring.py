# /// script
# requires-python = ">=3.10"
# dependencies = ["requests"]
# ///
"""shoestring -- boot an LLM on the cheapest GPU on Akash.

One command rents the cheapest suitable GPU on the Akash marketplace,
downloads a model, and serves it behind an OpenAI-compatible endpoint
(plus Anthropic /v1/messages when using vLLM) -- optionally reachable
only over your Tailscale tailnet. Closes with one command. Real-world
cost for a 27B coding model: about $0.16/hour, cold start under 10
minutes.

Usage:
    export AKASH_API_KEY=...                    # console.akash.network
    export LLM_API_KEY=$(openssl rand -hex 16)  # gates your endpoint
    export TS_AUTHKEY=tskey-auth-...            # --tailscale mode only

    ./shoestring.py deploy [--engine llamacpp] [--tailscale] [--max-ctx N]
    ./shoestring.py status <dseq>
    ./shoestring.py close  <dseq>                # billing stops here!

Env knobs: MODEL_ID, NO_MTP=1, KV_F16=1, HF_TOKEN, VLLM_IMAGE,
LLAMACPP_IMAGE, COMFY_IMAGE, QWEN_TEXT_ENCODER, QWEN_VAE, COMFY_ARGS,
BLACKLIST (comma-separated provider addresses).

See README.md for engines, network modes, cost data, and field notes.
"""
import argparse
import json
import os
import sys
import time

import requests

AKASH_API = os.environ.get("AKASH_API", "https://console-api.akash.network/v1")


def _headers():
    key = os.environ.get("AKASH_API_KEY")
    if not key:
        sys.exit("Set AKASH_API_KEY (create one at https://console.akash.network).")
    return {"x-api-key": key}


# ---------------------------------------------------------------------------
# Akash Console API client
#
# Ported from a battle-tested deployment loop. The one non-obvious rule:
# the server canonicalizes your YAML SDL into a JSON manifest at
# deployment-creation time, and THAT manifest (not a re-render of your SDL)
# must be submitted at lease time -- a mismatch produces leases that bill
# but never schedule the workload.
# ---------------------------------------------------------------------------

def create_deployment(sdl_yaml: str, deposit_usd: int = 5):
    resp = requests.post(
        f"{AKASH_API}/deployments",
        headers=_headers(),
        json={"data": {"sdl": sdl_yaml, "deposit": deposit_usd}},
        timeout=30,
    )
    resp.raise_for_status()
    data = resp.json()["data"]
    return data["dseq"], data["manifest"]


def get_bids(dseq: str):
    resp = requests.get(f"{AKASH_API}/bids/{dseq}", headers=_headers(), timeout=15)
    resp.raise_for_status()
    return resp.json().get("data", [])


def accept_bid(dseq: str, manifest: str, provider: str):
    resp = requests.post(
        f"{AKASH_API}/leases",
        headers=_headers(),
        json={
            "manifest": manifest,
            "leases": [{"dseq": dseq, "gseq": 1, "oseq": 1, "provider": provider}],
        },
        timeout=30,
    )
    if resp.status_code >= 400:
        # the body is the only place the actual rejection reason lives
        raise requests.HTTPError(
            f"{resp.status_code} from POST /leases: {resp.text[:800]}",
            response=resp,
        )
    return resp.json()


def get_deployment_status(dseq: str):
    resp = requests.get(f"{AKASH_API}/deployments/{dseq}", headers=_headers(), timeout=15)
    resp.raise_for_status()
    return resp.json()["data"]


def get_endpoint(dseq: str, timeout_s: int = 90):
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        data = get_deployment_status(dseq)
        leases = data.get("leases", [])
        if leases:
            status = leases[0].get("status") or {}
            services = status.get("services") or {}
            for svc in services.values():
                uris = svc.get("uris") or []
                if uris:
                    return f"http://{uris[0]}"
        time.sleep(5)
    return None


def close_deployment(dseq: str):
    requests.delete(f"{AKASH_API}/deployments/{dseq}", headers=_headers(), timeout=30)


def extract_gpu_model(bid_obj):
    """Pull the GPU model out of a bid's resource offer.

    On-chain gpu attributes are flattened key/value pairs like
    {key: "vendor/nvidia/model/rtx3090", value: "true"} (optionally with a
    /ram/24Gi suffix), nested somewhere under resources_offer. The exact
    nesting has shifted between Console API versions, so walk the whole
    bid rather than hardcoding a path.
    """
    found = []

    def walk(o):
        if isinstance(o, dict):
            k = o.get("key")
            if isinstance(k, str) and k.startswith("vendor/nvidia/model/"):
                found.append(k.split("/")[3])
            for v in o.values():
                walk(v)
        elif isinstance(o, list):
            for v in o:
                walk(v)

    walk(bid_obj)
    return found[0] if found else None


# ---------------------------------------------------------------------------
# Engines
# ---------------------------------------------------------------------------

# Providers to skip when ranking bids. Empty by default: shipping named
# addresses in a community tool is shadow governance. Populate via the
# BLACKLIST env var from your own experience or measured data (see
# data/*.csv for our published canary pilot); a criteria-based filter
# ("avoid providers below X% measured reachability") is planned.
PROVIDER_BLACKLIST = set(filter(None, os.environ.get("BLACKLIST", "").split(",")))

_VLLM_ARGS = (
    # first log line names the actual card -- chain attributes like
    # 'rtx6000' are ambiguous between the 24GB Quadro and the 48GB Ada
    "nvidia-smi --query-gpu=name,memory.total --format=csv; "
    "vllm serve {model} --host 0.0.0.0 --port 8000 "
    "--served-model-name {alias} --max-model-len {max_ctx} "
    "--gpu-memory-utilization 0.93 "
    "--enable-auto-tool-choice --tool-call-parser hermes "
    "--api-key $LLM_API_KEY"
)
_VLLM_IMAGE = os.environ.get("VLLM_IMAGE", "vllm/vllm-openai:latest")
ALIAS = os.environ.get("MODEL_ALIAS", "qwen3.8-27b")

# comfyui-qwen-image file set. MODEL_ID selects the diffusion GGUF as
# repo:file (restrictions: repo must also host the TE/VAE paths below).
_QWEN_MODEL = os.environ.get(
    "MODEL_ID",
    "abenzerps/Qwen-Image-2.1-Uncensored-GGUF:qwen-image-2.1-UC-Q4_K_M.gguf")
_QWEN_REPO, _, _QWEN_DIFFUSION_PATH = _QWEN_MODEL.partition(":")
if not _QWEN_DIFFUSION_PATH:
    _QWEN_DIFFUSION_PATH = "qwen-image-2.1-UC-Q4_K_M.gguf"
_QWEN_TE_PATH = os.environ.get(
    "QWEN_TEXT_ENCODER", "text_encoders/qwen3vl_8b_int8_convrot.safetensors")
_QWEN_VAE_PATH = os.environ.get(
    "QWEN_VAE", "vae/qwen_image_2.1_vae_bf16.safetensors")
_QWEN_DIFFUSION_URL = f"https://huggingface.co/{_QWEN_REPO}/resolve/main/{_QWEN_DIFFUSION_PATH}"
_QWEN_TE_URL = f"https://huggingface.co/{_QWEN_REPO}/resolve/main/{_QWEN_TE_PATH}"
_QWEN_VAE_URL = f"https://huggingface.co/{_QWEN_REPO}/resolve/main/{_QWEN_VAE_PATH}"
_QWEN_FILES = [_QWEN_DIFFUSION_URL, _QWEN_TE_URL, _QWEN_VAE_URL]

ENGINES = {
    # llamacpp: the battle-tested path. Cheapest cards (24GB) work.
    # OpenAI-compatible only; bridge to Claude Code via LiteLLM (README).
    "llamacpp": {
        "image": os.environ.get("LLAMACPP_IMAGE", "ghcr.io/ggml-org/llama.cpp:server-cuda"),
        "model": os.environ.get("MODEL_ID", "ggml-org/Qwen3.8-27B-GGUF:Q4_K_M"),
        "gpus": ["rtx5090", "a100", "h100", "h200", "rtx6000", "pro6000se"],
        "storage": "60Gi",
        # Context self-sizes to whatever card wins the bid: the SDL is
        # locked before bids arrive, so the container reads its own VRAM
        # at boot and picks a tier. Tiers assume q8_0 KV on a hybrid-mamba
        # model; --max-ctx pins an explicit value via CTX_FORCE instead.
        # NO_MTP=1 drops the speculative draft (the second GGUF must fit
        # in VRAM beside the main model -- 40GB+ cards only). KV_F16=1
        # keeps full-precision KV (halves max context, zero quant loss).
        "args": (
            "nvidia-smi --query-gpu=name,memory.total --format=csv; "
            "V=$(nvidia-smi --query-gpu=memory.total --format=csv,noheader,nounits | head -1); "
            "if [ $V -ge 70000 ]; then C=262144; "
            "elif [ $V -ge 38000 ]; then C=131072; "
            "elif [ $V -ge 30000 ]; then C=65536; "
            "else C=32768; fi; "
            "C=${{CTX_FORCE:-$C}}; echo llama ctx-size: $C; "
            "/app/llama-server -hf {model} "
             + ("" if os.environ.get("NO_MTP") else
                "-hfd ggml-org/Qwen3.8-27B-GGUF "
                "-md mtp-Qwen3.8-27B-Q4_0.gguf "
                "--spec-default --spec-type draft-mtp ")
            + "--reasoning-preserve "
            "--host 0.0.0.0 --port 8000 "
            "--ctx-size $C -ngl 99 --jinja --alias {alias} "
            + ("" if os.environ.get("KV_F16") else
               "--cache-type-k q8_0 --cache-type-v q8_0 ")
            + "--chat-template-kwargs '{{\\\"reasoning_effort\\\":\\\"low\\\"}}' "
            "--api-key $LLM_API_KEY"
        ),
        "max_ctx": None,  # self-sizing; --max-ctx pins
    },
    # vllm: serves BOTH OpenAI /v1/chat/completions and Anthropic
    # /v1/messages (Claude Code connects directly via ANTHROPIC_BASE_URL).
    # AWQ-INT4 runs on Ampere+; needs a true >=32GB card (24GB OOMs).
    "vllm": {
        "image": _VLLM_IMAGE,
        "model": os.environ.get("MODEL_ID", "cyankiwi/Qwen3.8-27B-AWQ-INT4"),
        "max_ctx": int(os.environ.get("MAX_CTX", "32768")),
        "gpus": ["a100", "h100", "h200", "rtx5090", "pro6000se"],
        "storage": "60Gi",
        "args": _VLLM_ARGS,
    },
    # vllm-fp8: first-party quant, 40GB+ cards.
    "vllm-fp8": {
        "image": _VLLM_IMAGE,
        "model": os.environ.get("MODEL_ID", "Qwen/Qwen3.8-27B-FP8"),
        "max_ctx": int(os.environ.get("MAX_CTX", "65536")),
        "gpus": ["a100", "h100", "h200", "pro6000se"],
        "storage": "80Gi",
        "args": _VLLM_ARGS,
    },
    # vllm-nvfp4: Blackwell native / recent-Hopper; NOT a100/Ampere.
    "vllm-nvfp4": {
        "image": _VLLM_IMAGE,
        "model": os.environ.get("MODEL_ID", "unsloth/Qwen3.8-27B-NVFP4"),
        "max_ctx": int(os.environ.get("MAX_CTX", "32768")),
        "gpus": ["rtx5090", "h100", "h200", "pro6000se"],
        "storage": "60Gi",
        "args": _VLLM_ARGS,
    },
    # comfyui-flux: image generation (prototype). ComfyUI + FLUX.1-schnell
    # fp8 single-file checkpoint (Apache 2.0 -- safe for commercial use).
    # ComfyUI has NO authentication of its own, so this engine REQUIRES
    # --tailscale: the tailnet is the access control. Browse to
    # http://<hostname>:8000 for the UI.
    "comfyui-flux": {
        # torch >=2.8 required: ComfyUI's comfy_kitchen registers custom
        # ops with PEP 585 annotations (list[int]) that older torch
        # infer_schema rejects -- 2.5.1 crash-looped (flight-recorder
        # verified 2026-08-27)
        "image": os.environ.get("COMFY_IMAGE",
                                "pytorch/pytorch:2.9.1-cuda12.8-cudnn9-runtime"),
        "model": os.environ.get(
            "MODEL_ID", "Comfy-Org/flux1-schnell:flux1-schnell-fp8.safetensors"),
        "max_ctx": None,
        "gpus": ["a100", "h100", "h200", "pro6000se"],
        "storage": "60Gi",
        "health": "/",          # ComfyUI web root; no /v1/models here
        "tailscale_only": True,
        # Flight recorder: the whole workload runs through tee; if it ever
        # exits, the container serves the boot log on the same port instead
        # of dying -- a crash-looping lease debugs itself over the tailnet
        # (curl http://<host>:8000/boot.log) instead of restarting blind.
        "args": (
            "( nvidia-smi --query-gpu=name,memory.total --format=csv; "
            "apt-get update && apt-get install -y git wget curl ca-certificates && "
            "git clone --depth 1 https://github.com/comfyanonymous/ComfyUI /opt/ComfyUI && "
            "cd /opt/ComfyUI && pip install --no-cache-dir -r requirements.txt && "
            "wget -nv -O models/checkpoints/flux1-schnell-fp8.safetensors "
            "https://huggingface.co/Comfy-Org/flux1-schnell/resolve/main/flux1-schnell-fp8.safetensors && "
            "python main.py --listen 0.0.0.0 --port 8000 "
            ") 2>&1 | tee /tmp/boot.log; "
            "echo '=== WORKLOAD EXITED; serving boot.log ==='; "
            "cd /tmp && python -m http.server 8000 --bind 127.0.0.1"
        ),
    },
    # comfyui-qwen-image: text-to-image via ComfyUI + ComfyUI-GGUF (uncensored
    # Qwen-Image 2.1 GGUF, Qwen Research License -- NOT Apache like Qwen 3.8).
    # Three files: diffusion GGUF in GPU VRAM (~4.6GB Q4_K_M), Qwen3-VL text
    # encoder (int8 default, ~9GB -- ComfyUI offloads it to CPU RAM, where it
    # costs nothing per step since encoding runs once per prompt), VAE.
    # Fits the cheapest 24GB cards. Unauthenticated UI => REQUIRES --tailscale.
    # The official Comfy-Org t2i template is preloaded under Workflows; swap
    # its UNETLoader for Unet Loader (GGUF) and queue.
    "comfyui-qwen-image": {
        "image": os.environ.get("COMFY_IMAGE",
                                "pytorch/pytorch:2.9.1-cuda12.8-cudnn9-runtime"),
        "model": _QWEN_MODEL,
        "max_ctx": None,
        "gpus": ["rtx5090", "a100", "h100", "h200", "rtx6000", "pro6000se"],
        "storage": "60Gi",
        "health": "/",
        "tailscale_only": True,
        "hf_files": _QWEN_FILES,
        "ui_help": (
            "Open the UI in a browser on your tailnet:  {endpoint}\n"
            "\n"
            "Starter workflow: left sidebar > Workflows > qwen_image_2_1_gguf_t2i\n"
            "(preloaded, GGUF-ready -- just Queue; edit the prompt text inline).\n"
            "\n"
            "--- teardown (billing stops only when closed!) -----------------------------\n"
            "./shoestring.py close {dseq}"
        ),
        "args": (
            "( nvidia-smi --query-gpu=name,memory.total --format=csv; "
            "apt-get update && apt-get install -y git wget curl ca-certificates && "
            "git clone --depth 1 https://github.com/comfyanonymous/ComfyUI /opt/ComfyUI && "
            "cd /opt/ComfyUI && pip install --no-cache-dir -r requirements.txt && "
            "git clone --depth 1 https://github.com/leejet/ComfyUI-GGUF custom_nodes/ComfyUI-GGUF && "
            "( [ -f custom_nodes/ComfyUI-GGUF/requirements.txt ] && pip install --no-cache-dir -r custom_nodes/ComfyUI-GGUF/requirements.txt || true ) && "
            "mkdir -p models/diffusion_models models/text_encoders models/vae user/default/workflows && "
            f"wget -nv -O models/diffusion_models/{_QWEN_DIFFUSION_PATH.rsplit('/', 1)[-1]} {_QWEN_DIFFUSION_URL} && "
            f"wget -nv -O models/text_encoders/{_QWEN_TE_PATH.rsplit('/', 1)[-1]} {_QWEN_TE_URL} && "
            f"wget -nv -O models/vae/{_QWEN_VAE_PATH.rsplit('/', 1)[-1]} {_QWEN_VAE_URL} && "
            "( wget -nv -O user/default/workflows/qwen_image_2_1_gguf_t2i.json "
            "https://raw.githubusercontent.com/themailman05/shoestring/main/workflows/qwen_image_2_1_gguf_t2i.json "
            "|| echo 'workflow preload failed; import the template URL manually' ) && "
            f"python main.py --listen 0.0.0.0 --port 8000 {os.environ.get('COMFY_ARGS', '')}"
            ") 2>&1 | tee /tmp/boot.log; "
            "echo '=== WORKLOAD EXITED; serving boot.log ==='; "
            "cd /tmp && python -m http.server 8000 --bind 127.0.0.1"
        ),
    },
}

SDL_TEMPLATE = """---
version: "2.0"
services:
  llm:
    image: {image}
    command: ["/bin/sh", "-c"]
    args:
      - "{args}"
    env:
      - LLM_API_KEY={api_key}{hf_token_env}{extra_env}
    expose:
      - port: 8000
        as: {expose_as}
        to:
          - global: true
profiles:
  compute:
    llm:
      resources:
        cpu:
          units: 8
        memory:
          size: 32Gi
        storage:
          size: {storage}
        gpu:
          units: 1
          attributes:
            vendor:
              nvidia:
{gpu_models}
  placement:
    akash:
      pricing:
        llm:
          denom: uact
          amount: {price}
deployment:
  llm:
    akash:
      profile: llm
      count: 1
"""
# Note the expose stays global even in --tailscale mode: Akash rejects
# manifests with zero global services ("invalid manifest: zero global
# services"). In tailscale mode the model server binds to loopback, so
# the public URI connects to nothing; real traffic enters via the tailnet.

BLOCKS_PER_HOUR = 3600 / 6.1  # ~6.1s Akash block time

# Joins your tailnet as an ephemeral node before starting the model
# server, which then binds to 127.0.0.1 only; `tailscale serve` is the
# supported inbound path under userspace networking. End-to-end WireGuard;
# the node self-removes when the lease closes.
TAILSCALE_BOOTSTRAP = (
    "export PATH=$PATH:/usr/sbin:/sbin; "
    "command -v curl >/dev/null || (apt-get update && apt-get install -y curl ca-certificates); "
    "command -v tailscale >/dev/null || (curl -fsSL https://tailscale.com/install.sh | sh); "
    "mkdir -p /var/lib/tailscale; "
    "tailscaled --tun=userspace-networking --statedir=/var/lib/tailscale & "
    "sleep 5; "
    "tailscale up --authkey=$TS_AUTHKEY --hostname={ts_hostname}; "
    "(tailscale serve --bg --tcp=8000 tcp://localhost:8000 "
    "|| tailscale serve --bg --tcp 8000 localhost:8000); "
)


def check_model_exists(model):
    repo = model.split(":")[0]
    try:
        code = requests.get(
            f"https://huggingface.co/api/models/{repo}", timeout=15
        ).status_code
        if code != 200:
            print(f"WARNING: {repo} returned HTTP {code} from Hugging Face -- "
                  "the container may fail to download it. Continuing anyway.")
    except requests.RequestException:
        pass


def deploy(args):
    api_key = os.environ.get("LLM_API_KEY")
    if not api_key:
        sys.exit("Set LLM_API_KEY (e.g. `export LLM_API_KEY=$(openssl rand -hex 16)`) "
                 "-- without --tailscale this endpoint is on the public internet.")

    eng = ENGINES[args.engine]
    max_ctx = args.max_ctx or eng["max_ctx"] or 0
    print(f"engine={args.engine}  model={eng['model']}"
          + (f"  ctx={max_ctx}" if max_ctx else "  ctx=auto (self-sized by VRAM)"))
    check_model_exists(eng["model"])
    for url in eng.get("hf_files", []):
        try:
            code = requests.head(url, allow_redirects=True, timeout=15).status_code
            if code != 200:
                print(f"WARNING: {url} returned HTTP {code} -- "
                      "the container will fail to download it. Continuing anyway.")
        except requests.RequestException:
            pass

    hf_token_env = ""
    if os.environ.get("HF_TOKEN"):
        hf_token_env = f"\n      - HF_TOKEN={os.environ['HF_TOKEN']}"

    run_args = eng["args"].format(model=eng["model"], max_ctx=max_ctx or 32768, alias=ALIAS)
    extra_env = ""
    if args.engine == "llamacpp" and args.max_ctx:
        # explicit --max-ctx pins the context; otherwise the container
        # self-sizes from the winning card's VRAM
        extra_env += f"\n      - CTX_FORCE={args.max_ctx}"
    if eng.get("tailscale_only") and not args.tailscale:
        sys.exit(f"--engine {args.engine} serves an unauthenticated UI and "
                 "REQUIRES --tailscale; refusing to expose it publicly.")
    if args.tailscale:
        ts_key = os.environ.get("TS_AUTHKEY")
        if not ts_key:
            sys.exit("--tailscale needs TS_AUTHKEY (ephemeral + pre-approved, "
                     "from https://login.tailscale.com/admin/settings/keys).")
        # model server binds to loopback; only tailscaled can reach it
        run_args = (TAILSCALE_BOOTSTRAP.format(ts_hostname=args.ts_hostname)
                    + run_args.replace("--host 0.0.0.0", "--host 127.0.0.1")
                              .replace("--listen 0.0.0.0", "--listen 127.0.0.1"))
        extra_env += f"\n      - TS_AUTHKEY={ts_key}"

    sdl = SDL_TEMPLATE.format(
        image=eng["image"],
        args=run_args,
        api_key=api_key,
        hf_token_env=hf_token_env,
        extra_env=extra_env,
        expose_as="8000" if args.tailscale else "80",
        storage=eng["storage"],
        gpu_models="\n".join(f"                - model: {m}" for m in eng["gpus"]),
        price=args.max_price,
    )

    print(f"Creating deployment (deposit ${args.deposit})...")
    dseq, manifest = create_deployment(sdl, deposit_usd=args.deposit)
    print(f"  dseq={dseq}")

    deadline = time.time() + 90
    bids = []
    while time.time() < deadline and not bids:
        time.sleep(10)
        bids = [
            b for b in get_bids(dseq)
            if b["bid"]["id"]["provider"] not in PROVIDER_BLACKLIST
        ]
    if not bids:
        close_deployment(dseq)
        sys.exit("No (non-blacklisted) bids -- supply for these cards is thin "
                 "right now; try again, try another --engine, or raise --max-price.")

    ranked = sorted(bids, key=lambda b: float(b["bid"]["price"]["amount"]))
    print("Bids:")
    for b in ranked:
        p = float(b["bid"]["price"]["amount"])
        # "uact" is micro-USD per block (console-verified), not micro-AKT
        usd_hr = p * BLOCKS_PER_HOUR / 1e6
        print(f"  {b['bid']['id']['provider']}  gpu={extract_gpu_model(b) or '?'}  "
              f"{p:.0f} uact/blk = ${usd_hr:.2f}/hr")

    chosen = ranked[0]
    provider = chosen["bid"]["id"]["provider"]
    print(f"Accepting cheapest: {provider}")
    try:
        accept_bid(dseq, manifest, provider)
    except Exception as e:
        print(f"accept_bid failed: {e}")
        close_deployment(dseq)
        sys.exit(f"Deployment {dseq} closed after failed lease.")

    if args.tailscale:
        # No public ingress; the node appears on the tailnet by hostname.
        # Run this from a tailnet device with MagicDNS on -- and use a
        # FRESH hostname per deploy: a dead ephemeral node squatting on
        # the name forces the new node to register with a -N suffix,
        # which this readiness poll would miss.
        endpoint = f"http://{args.ts_hostname}:8000"
        print(f"Endpoint (tailnet-only): {endpoint}")
    else:
        endpoint = get_endpoint(dseq, timeout_s=180)
        if not endpoint:
            close_deployment(dseq)
            sys.exit("Lease active but no endpoint after 180s -- closed the deployment.")
        print(f"Endpoint: {endpoint}")
    print("Waiting for the server (image pull + model download; "
          "typically 5-20 min)...")

    health_path = eng.get("health", "/v1/models")
    deadline = time.time() + 30 * 60
    ready = False
    while time.time() < deadline:
        try:
            r = requests.get(
                f"{endpoint}{health_path}",
                headers={"Authorization": f"Bearer {api_key}"},
                timeout=10,
            )
            if r.status_code == 200:
                ready = True
                break
        except requests.RequestException:
            pass
        time.sleep(20)

    if not ready:
        print("\nNot ready after 30 min. Leaving the lease up -- check with "
              f"`./shoestring.py status {dseq}`, or close it:\n"
              f"  ./shoestring.py close {dseq}")
        return

    p = float(chosen["bid"]["price"]["amount"])
    usd_hr = p * BLOCKS_PER_HOUR / 1e6
    print(f"\nREADY  dseq={dseq}  provider={provider}  "
          f"cost: ${usd_hr:.2f}/hr (~${usd_hr * 730:.0f}/mo)\n")
    if eng.get("health") == "/":
        # web-UI engine (e.g. comfyui-flux): no API configs to print
        print(eng.get("ui_help",
              "Open the UI in a browser on your tailnet:  {endpoint}\n"
              "\n"
              "--- teardown (billing stops only when closed!) -----------------------------\n"
              "./shoestring.py close {dseq}").format(endpoint=endpoint, dseq=dseq))
        return
    print(f"""--- smoke test -------------------------------------------------------------
curl {endpoint}/v1/chat/completions \\
  -H "Authorization: Bearer $LLM_API_KEY" -H "Content-Type: application/json" \\
  -d '{{"model":"{ALIAS}","messages":[{{"role":"user","content":"hi"}}],"max_tokens":50}}'

--- opencode (~/.config/opencode/opencode.json) ----------------------------
{{
  "$schema": "https://opencode.ai/config.json",
  "provider": {{
    "shoestring": {{
      "npm": "@ai-sdk/openai-compatible",
      "name": "shoestring",
      "options": {{ "baseURL": "{endpoint}/v1", "apiKey": "{{env:LLM_API_KEY}}" }},
      "models": {{ "{ALIAS}": {{ "name": "{ALIAS} (Akash)" }} }}
    }}
  }},
  "model": "shoestring/{ALIAS}"
}}
""")
    if args.engine.startswith("vllm"):
        print(f"""--- Claude Code (vLLM serves Anthropic /v1/messages natively) --------------
export ANTHROPIC_BASE_URL={endpoint}
export ANTHROPIC_API_KEY=$LLM_API_KEY
export ANTHROPIC_DEFAULT_OPUS_MODEL={ALIAS}
claude
""")
    else:
        print(f"""--- Claude Code (llama.cpp is OpenAI-only: bridge with LiteLLM) ------------
pip install 'litellm[proxy]'
cat > /tmp/litellm-shoestring.yaml <<EOF
model_list:
  - model_name: {ALIAS}
    litellm_params:
      model: openai/{ALIAS}
      api_base: "{endpoint}/v1"
      api_key: "os.environ/LLM_API_KEY"
EOF
litellm --config /tmp/litellm-shoestring.yaml --port 4000 &
export ANTHROPIC_BASE_URL=http://localhost:4000
export ANTHROPIC_API_KEY=$LLM_API_KEY
export ANTHROPIC_DEFAULT_OPUS_MODEL={ALIAS}
claude
""")
    print(f"""--- teardown (billing stops only when closed!) -----------------------------
./shoestring.py close {dseq}""")


def status(args):
    print(json.dumps(get_deployment_status(args.dseq), indent=2)[:4000])


def close(args):
    close_deployment(args.dseq)
    print(f"Closed {args.dseq}.")


def main():
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    sub = ap.add_subparsers(dest="cmd", required=True)
    d = sub.add_parser("deploy", help="rent the cheapest GPU and serve the model")
    d.add_argument("--engine", choices=list(ENGINES), default="llamacpp")
    d.add_argument("--tailscale", action="store_true",
                   help="tailnet-only endpoint (needs TS_AUTHKEY)")
    d.add_argument("--ts-hostname", default="shoestring",
                   help="tailnet hostname; use a FRESH name each deploy")
    d.add_argument("--deposit", type=int, default=10, help="escrow deposit in USD")
    d.add_argument("--max-price", type=int, default=20000,
                   help="pricing ceiling in uact/block (uact = micro-USD; 20000 ~= $12/hr)")
    d.add_argument("--max-ctx", type=int, default=None,
                   help="pin context length (llamacpp default: self-size by VRAM)")
    d.set_defaults(fn=deploy)
    s = sub.add_parser("status", help="deployment status JSON")
    s.add_argument("dseq")
    s.set_defaults(fn=status)
    c = sub.add_parser("close", help="close the deployment (stops billing)")
    c.add_argument("dseq")
    c.set_defaults(fn=close)
    args = ap.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()
