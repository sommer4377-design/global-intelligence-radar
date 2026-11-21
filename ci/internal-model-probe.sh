#!/usr/bin/env bash
set -euo pipefail
probe="$(mktemp -d "${RUNNER_TEMP}/radar-model.XXXXXX")"
asset=llama-b12606-bin-ubuntu-x64.tar.gz
curl -qfsSL --connect-timeout 10 --max-time 90 --max-filesize 25000000   "https://github.com/ggml-org/llama.cpp/releases/download/b12606/$asset" -o "$probe/runtime.tar.gz"
echo "23ad17d2052e35a7c0a16a7d84a46b3f44c0f90d8ef438e27712c1c158321024  $probe/runtime.tar.gz" | sha256sum -c -
tar -xzf "$probe/runtime.tar.gz" -C "$probe"
curl -qfsSL --connect-timeout 10 --max-time 240 --max-filesize 1120000000   'https://modelscope.cn/models/Qwen/Qwen2.5-1.5B-Instruct-GGUF/resolve/master/qwen2.5-1.5b-instruct-q4_k_m.gguf'   -o "$probe/model.gguf" -w 'model download %{time_total} seconds, %{size_download} bytes\n'
echo "6a1a2eb6d15622bf3c96857206351ba97e1af16c30d7a74ee38970e434e9407e  $probe/model.gguf" | sha256sum -c -
RADAR_PROBE="$probe" python - <<'PY'
import os
from pathlib import Path
from intelligence.local_writer import inference
root = Path(os.environ["RADAR_PROBE"])
runtime = next(root.rglob("llama-completion"))
result = inference(runtime, root/"model.gguf",
                   {"A1":"仅供模型冷启动测试的合成输入。这里没有真实新闻。"},
                   diagnostic=True, tokens=32, timeout=65)
print({key:result.get(key) for key in ("seconds","peak_mib","returncode","timing")})
if result.get("returncode") != 0 or not result.get("draft"):
    raise SystemExit("Linux model cold probe failed")
PY
