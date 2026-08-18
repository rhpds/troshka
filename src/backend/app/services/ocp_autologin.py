"""NSS-based ocp-autologin.py deployed to bastion hosts for Firefox credential stash."""

OCP_AUTOLOGIN_SCRIPT = """\
import ctypes
import ctypes.util
import json
import base64
import glob
import os
import sys
import time
import uuid

console_url = sys.argv[1]
for pw_path in [
    "~/ocp-install/auth/kubeadmin-password",
    "~cloud-user/ocp-install/auth/kubeadmin-password",
]:
    p = os.path.expanduser(pw_path)
    if os.path.exists(p):
        pw = open(p).read().strip()
        break
else:
    print("ERROR: kubeadmin-password not found")
    sys.exit(1)

parts = console_url.split("apps.", 1)
if len(parts) < 2:
    print("Cannot parse domain from " + console_url)
    sys.exit(1)
domain = parts[1].rstrip("/")
oauth_url = "https://oauth-openshift.apps." + domain

profiles = sorted(glob.glob("/home/cloud-user/.mozilla/firefox/*.default*/"))
if not profiles:
    print("ERROR: No Firefox profile found")
    sys.exit(1)
profile = profiles[0].rstrip("/")


class SECItem(ctypes.Structure):
    _fields_ = [("type", ctypes.c_uint), ("data", ctypes.c_void_p), ("len", ctypes.c_uint)]


nss = None
for lib in ["libnss3.so", ctypes.util.find_library("nss3") or ""]:
    if lib:
        try:
            nss = ctypes.CDLL(lib)
            break
        except OSError:
            continue
if not nss:
    print("ERROR: libnss3.so not found")
    sys.exit(1)
if nss.NSS_Init(("sql:" + profile).encode()) != 0:
    print("ERROR: NSS_Init failed")
    sys.exit(1)


def encrypt(text):
    data = text.encode("utf-8")
    buf = ctypes.create_string_buffer(data, len(data))
    inp = SECItem(0, ctypes.cast(buf, ctypes.c_void_p), len(data))
    out = SECItem(0, None, 0)
    if nss.PK11SDR_Encrypt(None, ctypes.byref(inp), ctypes.byref(out), None) != 0:
        return None
    return base64.b64encode(ctypes.string_at(out.data, out.len)).decode()


eu = encrypt("kubeadmin")
ep = encrypt(pw)
if not eu or not ep:
    print("ERROR: Encryption failed")
    nss.NSS_Shutdown()
    sys.exit(1)

now_ms = int(time.time() * 1000)
logins = {
    "nextId": 2,
    "logins": [
        {
            "id": 1,
            "hostname": oauth_url,
            "httpRealm": None,
            "formSubmitURL": oauth_url,
            "usernameField": "inputUsername",
            "passwordField": "inputPassword",  # pragma: allowlist secret
            "encryptedUsername": eu,
            "encryptedPassword": ep,
            "guid": "{" + str(uuid.uuid4()) + "}",
            "encType": 1,
            "timeCreated": now_ms,
            "timeLastUsed": now_ms,
            "timePasswordChanged": now_ms,
            "timesUsed": 1,
        }
    ],
}
with open(os.path.join(profile, "logins.json"), "w") as f:
    json.dump(logins, f)
nss.NSS_Shutdown()
print("Password saved to Firefox")
"""
