#!/usr/bin/env bash
set -euo pipefail

# Fix Dsh Web UI history/chat loading on insecure origins (http://IP:port).
#
# Root cause: the browser bundles call `crypto.randomUUID()`, which only exists
# in secure contexts. When Dsh is served over plain HTTP (e.g. the nginx
# reverse proxy on :8443), that call throws before any RPC is sent, so the UI
# opens but session list/history stays empty and the server logs:
#   The user aborted a request.
#
# This script replaces those direct calls with Dsh's existing UUID fallback
# (`randomUuid()` / a small getRandomValues-based helper), making the Web UI
# work on insecure origins even without relying on the nginx sub_filter shim.

CLIENT_CONNECTION="/home/myuser/.npm-global/lib/node_modules/@deepseek-ai/dsh/node_modules/@deepseek-ai/dsh-client-connection/lib/client.js"
CLIENT_CONVERSATION="/home/myuser/.npm-global/lib/node_modules/@deepseek-ai/dsh/node_modules/@deepseek-ai/dsh-client-ui-conversation/lib/client.js"

if [[ ! -f "$CLIENT_CONNECTION" || ! -f "$CLIENT_CONVERSATION" ]]; then
  echo "error: Dsh client files not found at expected paths" >&2
  exit 1
fi

python3 - "$CLIENT_CONNECTION" "$CLIENT_CONVERSATION" <<'PY'
import sys
from pathlib import Path

connection = Path(sys.argv[1])
conversation = Path(sys.argv[2])

s = connection.read_text()
orig = s
s = s.replace("id: MessageId(crypto.randomUUID())", "id: MessageId(randomUuid())", 1)
s = s.replace("return RpcId(crypto.randomUUID());", "return RpcId(randomUuid());", 1)
if s == orig:
    print("client-connection: no changes (already patched?)")
else:
    connection.write_text(s)
    print("client-connection: patched")

s = conversation.read_text()
orig = s
old = """\t\t/** Create one browser-only draft descriptor; only its id enters input state. */
\t\tfunction browserDraftAttachment(file) {
\t\t\treturn {
\t\t\t\tkind: "image",
\t\t\t\tid: crypto.randomUUID(),
\t\t\t\tpreviewUrl: URL.createObjectURL(file),
\t\t\t\tfile
\t\t\t};
\t\t}"""
new = """\t\t/** Create one browser-only draft descriptor; only its id enters input state. */
\t\tfunction browserUuid() {
\t\t\tconst bytes = globalThis.crypto.getRandomValues(new Uint8Array(16));
\t\t\tconst view = new DataView(bytes.buffer, bytes.byteOffset, bytes.byteLength);
\t\t\tview.setUint8(6, view.getUint8(6) & 15 | 64);
\t\t\tview.setUint8(8, view.getUint8(8) & 63 | 128);
\t\t\tconst hex = Array.from(bytes, (byte) => byte.toString(16).padStart(2, "0")).join("");
\t\t\treturn `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20)}`;
\t\t}
\t\tfunction browserDraftAttachment(file) {
\t\t\treturn {
\t\t\t\tkind: "image",
\t\t\t\tid: browserUuid(),
\t\t\t\tpreviewUrl: URL.createObjectURL(file),
\t\t\t\tfile
\t\t\t};
\t\t}"""
if old not in s:
    # Fallback: simple replacement if whitespace differs.
    s2 = s.replace("id: crypto.randomUUID(),", "id: browserUuid(),", 1)
    if s2 == s:
        print("client-conversation: no changes (already patched or pattern changed?)")
    else:
        # Insert helper before the first use if browserUuid is not defined yet.
        if "function browserUuid()" not in s2:
            marker = "\t\tfunction browserDraftAttachment(file) {"
            helper = """\t\tfunction browserUuid() {
\t\t\tconst bytes = globalThis.crypto.getRandomValues(new Uint8Array(16));
\t\t\tconst view = new DataView(bytes.buffer, bytes.byteOffset, bytes.byteLength);
\t\t\tview.setUint8(6, view.getUint8(6) & 15 | 64);
\t\t\tview.setUint8(8, view.getUint8(8) & 63 | 128);
\t\t\tconst hex = Array.from(bytes, (byte) => byte.toString(16).padStart(2, "0")).join("");
\t\t\treturn `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20)}`;
\t\t}
"""
            s2 = s2.replace(marker, helper + marker, 1)
        conversation.write_text(s2)
        print("client-conversation: patched (fallback)")
else:
    s = s.replace(old, new, 1)
    conversation.write_text(s)
    print("client-conversation: patched")
PY

echo
echo "Done. Restart Dsh Web UI to load the patched frontend:"
echo "  pkill -f 'dsh web --port 3080' || true"
echo "  cd /home/myuser && nohup dsh web --port 3080 --trusted-host 43.133.253.208:8443 > /tmp/dsh_web.log 2>&1 &"
