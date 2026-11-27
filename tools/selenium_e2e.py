#!/usr/bin/python3
"""Exercise the managed Selenium target using an isolated Fedora browser fixture."""
import argparse
import fcntl
import os
from pathlib import Path
import re
import stat
import subprocess
import tempfile


SOURCE = Path(__file__).resolve().parents[1]
CHROME = "/usr/bin/chromium-browser"
DRIVER = Path("usr/lib64/chromium-browser/chromedriver")
RPM_QUERY = "%{VERSION}-%{RELEASE}.%{ARCH}"
PROBE = """\
import sys
from importlib.metadata import version
from pathlib import Path
from tempfile import TemporaryDirectory
import selenium
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By

assert sys.prefix == sys.base_prefix
assert Path(selenium.__file__).is_relative_to('/tmp/packages')
with TemporaryDirectory() as profile:
    options = Options()
    options.binary_location = '/usr/bin/chromium-browser'
    for flag in ('--headless=new', '--no-sandbox', '--disable-gpu',
                 '--disable-dev-shm-usage', '--no-first-run', '--user-data-dir=' + profile):
        options.add_argument(flag)
    with webdriver.Chrome(service=Service('/tmp/chromedriver'), options=options) as browser:
        browser.get('data:text/html,<title>fixture</title><main id="result">ready</main>')
        assert browser.title == 'fixture'
        assert browser.find_element(By.ID, 'result').text == 'ready'
        assert browser.capabilities['browserVersion'] == sys.argv[1]
        assert browser.capabilities['chrome']['chromedriverVersion'].split()[0] == sys.argv[1]
print('Verified Selenium ' + version('selenium') + ' with Fedora Chromium and ChromeDriver ' + sys.argv[1])
"""


def output(args, **kwargs):
    return subprocess.check_output(args, text=True, timeout=45, **kwargs).strip()


def prepare_driver(directory):
    installed = output(["/usr/bin/rpm", "-qf", "--qf", "%{NAME} " + RPM_QUERY, CHROME])
    name, version_release = installed.split()
    if name != "chromium" or not re.fullmatch(r"\d+(?:\.\d+){3}-[0-9A-Za-z.]+\.x86_64", version_release):
        raise ValueError("Expected the Fedora x86_64 Chromium package")
    fedora = output(["/usr/bin/rpm", "--eval", "%fedora"])
    if not fedora.isdecimal():
        raise ValueError("Unable to establish the Fedora signing key")
    key = f"/usr/share/pki/rpm-gpg/RPM-GPG-KEY-fedora-{fedora}-primary"
    key_records = output(["/usr/bin/gpg", "--batch", "--no-options", "--homedir", str(directory),
                          "--with-colons", "--show-keys", key])
    fingerprints = {line.split(":")[9].lower() for line in key_records.splitlines() if line.startswith("fpr:")}
    package = f"chromedriver-{version_release}"
    subprocess.run(["/usr/bin/dnf5", "--repo=fedora", "--repo=updates",
                    f"--setopt=cachedir={directory / 'cache'}", "download",
                    f"--destdir={directory}", package], check=True, timeout=100)
    archive = directory / (package + ".rpm")
    verified = output(["/usr/bin/rpmkeys", "-Kv", str(archive)], env={"LC_ALL": "C", "PATH": "/usr/bin:/bin"})
    if not fingerprints & set(re.findall(r"signature, key fingerprint: ([a-f\d]+): OK", verified)):
        raise ValueError("The ChromeDriver RPM lacks a valid Fedora signature")
    if output(["/usr/bin/rpm", "-qp", "--qf", "%{NAME} " + RPM_QUERY, str(archive)]) != "chromedriver " + version_release:
        raise ValueError("ChromeDriver RPM does not match installed Chromium")
    payload = subprocess.check_output(["/usr/bin/rpm2cpio", str(archive)], timeout=45)
    subprocess.run(["/usr/bin/cpio", "-id", "--quiet", "--no-absolute-filenames", "./" + str(DRIVER)],
                   input=payload, cwd=directory, check=True, timeout=45)
    driver = directory / DRIVER
    if not driver.is_file() or driver.is_symlink() or not os.access(driver, os.X_OK):
        raise ValueError("ChromeDriver executable is missing from the signed RPM")
    return driver, version_release.split("-")[0]


def verify(packages, *, full=False):
    if not packages.is_dir() or (packages / "pyvenv.cfg").exists():
        raise ValueError("Expected the flat Selenium test target")
    with tempfile.TemporaryDirectory(prefix="ccs-webdriver-") as temporary:
        driver, version = prepare_driver(Path(temporary))
        args = ["-m", "unittest", "discover", "-s", "tests/e2e", "-p", "test_note_state_persistence.py", "-v"] \
            if full else ["-S", "-P", "-c", PROBE, version]
        subprocess.run([
            "/usr/bin/bwrap", "--die-with-parent", "--unshare-net", "--unshare-pid",
            "--ro-bind", "/", "/", "--tmpfs", "/home", "--tmpfs", "/run", "--tmpfs", "/tmp",
            "--tmpfs", "/mnt", "--tmpfs", "/media", "--tmpfs", "/var/lib",
            "--proc", "/proc", "--dev", "/dev",
            "--ro-bind", str(SOURCE), "/tmp/work",
            "--ro-bind", str(packages), "/tmp/packages",
            "--ro-bind", str(driver), "/tmp/chromedriver",
            "--chdir", "/tmp/work", "--clearenv", "--setenv", "HOME", "/tmp",
            "--setenv", "PATH", "/usr/bin:/bin", "--setenv", "PYTHONPATH", "/tmp/packages",
            "--setenv", "CCS_E2E_HEADLESS", "1", "--setenv", "CHROME_BINARY", CHROME,
            "--setenv", "CHROMEDRIVER_PATH", "/tmp/chromedriver",
            "/usr/bin/python3", "-B", *args,
        ], check=True, timeout=300 if full else 60)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--full", action="store_true")
    parser.add_argument("staging", type=Path, nargs="*")
    args = parser.parse_args()
    if args.staging:
        if len(args.staging) != 3 or args.full:
            parser.error("FEU staging expects candidate, pins and active paths")
        candidate, pins, active = map(Path.resolve, args.staging)
        if candidate.exists() or candidate.parent == active.parent:
            raise ValueError("Expected a separate empty staging directory")
        subprocess.run([
            "/usr/bin/uv", "--no-config", "--no-cache", "pip", "install",
            "--python", "/usr/bin/python3", "--index-url", "https://pypi.org/simple",
            "--only-binary", ":all:", "--target", str(candidate), "-r", str(pins),
        ], check=True, timeout=90)
        verify(candidate)
        return
    packages = Path.home() / ".local/share/chrome-codex-switcher/selenium-e2e/packages"
    with os.fdopen(os.open(packages.parent / ".dependency-update.lock", os.O_RDONLY | os.O_NOFOLLOW), "rb") as lock:
        info = os.fstat(lock.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
            raise ValueError("Expected the project's owned Selenium dependency lock")
        fcntl.flock(lock, fcntl.LOCK_SH)
        verify(packages, full=args.full)


if __name__ == "__main__":
    main()
