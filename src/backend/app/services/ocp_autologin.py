"""Selenium-based ocp-autologin.py deployed to bastion hosts for Firefox credential stash."""

GECKODRIVER_VERSION = "0.37.0"
GECKODRIVER_URL = (
    f"https://github.com/mozilla/geckodriver/releases/download/"
    f"v{GECKODRIVER_VERSION}/geckodriver-v{GECKODRIVER_VERSION}-linux64.tar.gz"
)

OCP_AUTOLOGIN_SCRIPT = """\
import glob
import os
import sys
import time

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


def _has_nss_db(path):
    return (
        os.path.isfile(os.path.join(path, "cert9.db"))
        and os.path.isfile(os.path.join(path, "key4.db"))
    )


def _find_profile():
    candidates = []
    for path in glob.glob("/home/cloud-user/.mozilla/firefox/*/"):
        name = os.path.basename(path.rstrip("/"))
        if name in ("Profile Groups", "Crash Reports", "Pending Pings"):
            continue
        profile = path.rstrip("/")
        if _has_nss_db(profile):
            candidates.append(profile)
    if not candidates:
        return None
    for profile in candidates:
        if profile.endswith(".default-default"):
            return profile
    return sorted(candidates)[-1]


profile = _find_profile()
if not profile:
    print("ERROR: No Firefox profile with NSS database found")
    sys.exit(1)

geckodriver = os.environ.get("GECKODRIVER_PATH", "/usr/local/bin/geckodriver")
if not os.path.isfile(geckodriver):
    print("ERROR: geckodriver not found at " + geckodriver)
    sys.exit(1)

try:
    from selenium import webdriver
    from selenium.webdriver.common.by import By
    from selenium.webdriver.firefox.options import Options
    from selenium.webdriver.firefox.service import Service
    from selenium.webdriver.support import expected_conditions as EC
    from selenium.webdriver.support.ui import WebDriverWait
except ImportError:
    print("ERROR: selenium Python package not installed")
    sys.exit(1)

opts = Options()
opts.add_argument("-headless")
opts.add_argument("-remote-allow-system-access")
opts.add_argument("-profile")
opts.add_argument(profile)
opts.accept_insecure_certs = True
opts.set_preference("signon.rememberSignons", True)
opts.set_preference("signon.autofillForms", True)
opts.set_preference("signon.storeWhenAutocompleteOff", True)
opts.set_preference("browser.startup.page", 1)

service = Service(geckodriver, log_output=os.devnull, service_args=["--allow-system-access"])
driver = webdriver.Firefox(service=service, options=opts)
try:
    base = console_url.rstrip("/")
    driver.get(base)
    time.sleep(2)
    driver.delete_all_cookies()
    driver.get(base + "/logout")
    time.sleep(2)
    driver.delete_all_cookies()
    driver.get(console_url)
    wait = WebDriverWait(driver, 45)
    try:
        username = wait.until(EC.presence_of_element_located((By.ID, "inputUsername")))
    except Exception:
        print(
            "ERROR: login form not found (url="
            + driver.current_url
            + ", title="
            + driver.title
            + ")"
        )
        sys.exit(1)
    password = driver.find_element(By.ID, "inputPassword")
    username.clear()
    username.send_keys("kubeadmin")
    password.clear()
    password.send_keys(pw)
    driver.find_element(By.CSS_SELECTOR, "button[type=submit]").click()
    time.sleep(3)
    driver.set_context("chrome")
    for _ in range(20):
        try:
            driver.find_element(
                By.CSS_SELECTOR,
                "popupnotification[id*=password] "
                "button.popup-notification-primary-button",
            ).click()
            print("Password saved to Firefox")
            break
        except Exception:
            time.sleep(0.5)
    else:
        print("ERROR: Firefox did not offer to save password (url=" + driver.current_url + ")")
        sys.exit(1)
finally:
    driver.quit()
"""
