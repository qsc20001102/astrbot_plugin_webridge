#!/usr/bin/env bash
# Rebuild the bundled engine in an isolated container; never opens an account.
set -euo pipefail
test "$(uname -s)" = Linux && test "$(uname -m)" = x86_64 || {
  echo 'Build on Linux x86_64 with Docker, Git and Python 3.' >&2; exit 1;
}
root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
commit=39af1eec84f4763e1257406f8c5fb848b31d82e9
work_root=${WEBRIDGE_BUILD_DIR:-"$root/../Test/webridge-native-build"}
mkdir -p "$work_root" "$root/bin/linux-x86_64"
work=$(mktemp -d "$work_root/build-XXXXXX")
docker build -f "$root/native/Dockerfile.build" -t webridge-native-build:1.93.1 "$root/native"
# Check both developer components before applying the source patch.
docker run --rm webridge-native-build:1.93.1 bash -c \
  'rustc +1.93.1 --version && cargo +1.93.1 fmt --version && cargo +1.93.1 clippy --version'
git -c core.autocrlf=false clone --no-checkout https://github.com/thisnick/agent-wechat.git "$work/source"
git -C "$work/source" -c core.autocrlf=false checkout --detach "$commit"
test "$(git -C "$work/source" rev-parse HEAD)" = "$commit"
git -C "$work/source" apply --check "$root/native/patches/original-image.patch"
git -C "$work/source" apply "$root/native/patches/original-image.patch"
git -C "$work/source" apply --check "$root/native/patches/merged-forward.patch"
git -C "$work/source" apply "$root/native/patches/merged-forward.patch"
docker run --rm -v "$work/source/packages/native-client-rust:/work" -v "$root:/out" \
  -e CARGO_BUILD_JOBS="${CARGO_BUILD_JOBS:-3}" -e LOCAL_UID="$(id -u)" -e LOCAL_GID="$(id -g)" \
  webridge-native-build:1.93.1 bash -euc '
    cargo +1.93.1 fmt --all
    cargo +1.93.1 clippy --workspace --all-targets --locked -- -D warnings
    cargo +1.93.1 test --locked -p wechat-core -p wechat-media -p wechat-protocol -p wechat-api -p wechat-simulator -p wechat-store -p wechat-profile
    cargo +1.93.1 build --release --locked -p wx-native
    strip --strip-unneeded target/release/wx
    install -m 0755 target/release/wx /out/bin/linux-x86_64/wx
    chown "$LOCAL_UID:$LOCAL_GID" /out/bin/linux-x86_64/wx
  '
python3 - "$root" <<'PY'
import hashlib
import json
import sys
from pathlib import Path

root = Path(sys.argv[1])
path = root / "native/manifest.json"
manifest = json.loads(path.read_text())
manifest["sha256"] = hashlib.sha256((root / "bin/linux-x86_64/wx").read_bytes()).hexdigest()
manifest["patch_sha256"] = hashlib.sha256((root / "native/patches/original-image.patch").read_bytes()).hexdigest()
manifest["forward_patch_sha256"] = hashlib.sha256((root / "native/patches/merged-forward.patch").read_bytes()).hexdigest()
manifest["merged_forward"] = True
manifest["forward_projection"] = "xml-v1"
path.write_text(json.dumps(manifest, indent=2) + "\n")
print("Built Linux x86_64 engine; SHA256:", manifest["sha256"])
PY
echo "Build workspace retained at: $work"
