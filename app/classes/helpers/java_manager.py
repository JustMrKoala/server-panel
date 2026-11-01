import glob
import hashlib
import json
import logging
import os
import platform
import re
import shutil
import tarfile
import tempfile
import threading
import zipfile

import requests

logger = logging.getLogger(__name__)

ADOPTIUM_API = "https://api.adoptium.net/v3/assets/latest/{feature}/hotspot"
# Java releases Temurin ships as a JRE. Anything else is rounded up to the next one.
SUPPORTED_FEATURES = [8, 11, 17, 21, 25]


def java_for_mc_version(mc_version):
    """Return the Java release a Minecraft version needs, or None if unknown."""
    match = re.match(r"^(\d+)\.(\d+)(?:\.(\d+))?", str(mc_version or ""))
    if not match:
        return None
    major, minor, patch = (int(x or 0) for x in match.groups())
    if major >= 26:
        # Year based versions (26.1 and later) need Java 25
        return 25
    if major != 1:
        return None
    if (minor, patch) >= (20, 5):
        return 21
    if minor >= 18:
        return 17
    if minor == 17:
        return 16
    return 8


def round_feature(feature):
    for supported in SUPPORTED_FEATURES:
        if feature <= supported:
            return supported
    return SUPPORTED_FEATURES[-1]


class JavaManager:
    """Downloads and tracks Eclipse Temurin runtimes for Minecraft servers."""

    _locks = {}
    _locks_guard = threading.Lock()

    def __init__(self, helper):
        self.helper = helper
        self.java_dir = os.path.join(helper.root_dir, "java")

    # ------------------------------------------------------------------ detection

    @staticmethod
    def read_jar_version(jar_path):
        """Read version.json from a server jar (vanilla, Paper, Purpur and friends)."""
        try:
            with zipfile.ZipFile(jar_path) as jar:
                with jar.open("version.json") as f:
                    data = json.load(f)
            return {"id": data.get("id"), "java": data.get("java_version")}
        except (OSError, KeyError, zipfile.BadZipFile, ValueError):
            return None

    @staticmethod
    def _version_from_libraries(server_path):
        """Find the Minecraft version of Forge, NeoForge and Fabric installs."""
        patterns = [
            ("libraries/net/minecraft/server/*/server-*.jar", r"server/([^/\\]+)/"),
            ("libraries/net/minecraftforge/forge/*", r"forge[/\\]([0-9.]+)-"),
            ("libraries/net/neoforged/neoforge/*", r"neoforge[/\\]([0-9.]+)"),
        ]
        for pattern, regex in patterns:
            for path in sorted(glob.glob(os.path.join(server_path, pattern))):
                match = re.search(regex, path.replace("\\", "/"))
                if not match:
                    continue
                found = match.group(1)
                if "neoforge" in pattern:
                    # NeoForge 21.4.x targets Minecraft 1.21.4, 26.1.x targets 26.1
                    major, minor = found.split(".")[:2]
                    if int(major) >= 26:
                        found = f"{major}.{minor}"
                    elif minor == "0":
                        found = f"1.{major}"
                    else:
                        found = f"1.{major}.{minor}"
                return found
        return None

    def detect(self, server_path, executable):
        """Return {"mc_version", "java"} for a server, values may be None."""
        candidates = [os.path.join(server_path, executable or "")]
        candidates += sorted(glob.glob(os.path.join(server_path, "*.jar")))
        for jar in candidates:
            if not jar.endswith(".jar") or not os.path.isfile(jar):
                continue
            info = self.read_jar_version(jar)
            if info and info.get("id"):
                java = info.get("java") or java_for_mc_version(info["id"])
                return {"mc_version": info["id"], "java": java}

        mc_version = self._version_from_libraries(server_path)
        if not mc_version:
            match = re.search(r"(\d+\.\d+(?:\.\d+)?)", executable or "")
            mc_version = match.group(1) if match else None
        return {"mc_version": mc_version, "java": java_for_mc_version(mc_version)}

    # ------------------------------------------------------------------ installs

    @staticmethod
    def _platform():
        system = platform.system().lower()
        os_name = {"windows": "windows", "darwin": "mac"}.get(system, "linux")
        if os_name == "linux" and os.path.exists("/etc/alpine-release"):
            os_name = "alpine-linux"
        machine = platform.machine().lower()
        arch = {
            "amd64": "x64",
            "x86_64": "x64",
            "aarch64": "aarch64",
            "arm64": "aarch64",
            "armv7l": "arm",
        }.get(machine, machine)
        return os_name, arch

    def install_dir(self, feature):
        return os.path.join(self.java_dir, f"temurin-{feature}")

    def java_binary(self, feature):
        """Path to an installed managed java binary, or None."""
        exe = "java.exe" if platform.system() == "Windows" else "java"
        for path in (
            os.path.join(self.install_dir(feature), "bin", exe),
            os.path.join(self.install_dir(feature), "Contents", "Home", "bin", exe),
        ):
            if os.path.isfile(path):
                return path
        return None

    def installed(self):
        return [f for f in SUPPORTED_FEATURES if self.java_binary(f)]

    def feature_of_path(self, path):
        """Return the managed feature a java path belongs to, or None."""
        if not path:
            return None
        norm = os.path.normcase(os.path.abspath(path.strip('"')))
        for feature in SUPPORTED_FEATURES:
            root = os.path.normcase(self.install_dir(feature)) + os.sep
            if norm.startswith(root):
                return feature
        return None

    @classmethod
    def _lock_for(cls, feature):
        with cls._locks_guard:
            return cls._locks.setdefault(feature, threading.Lock())

    def ensure(self, feature, progress=None):
        """Return a java binary for this feature, downloading it when missing."""
        feature = round_feature(int(feature))
        with self._lock_for(feature):
            existing = self.java_binary(feature)
            if existing:
                return existing
            if progress:
                progress(f"Downloading Java {feature} (Eclipse Temurin)...")
            self._download(feature)
            if progress:
                progress(f"Java {feature} is installed.")
            return self.java_binary(feature)

    def _download(self, feature):
        os_name, arch = self._platform()
        res = requests.get(
            ADOPTIUM_API.format(feature=feature),
            params={
                "architecture": arch,
                "image_type": "jre",
                "os": os_name,
                "vendor": "eclipse",
            },
            timeout=30,
        )
        res.raise_for_status()
        assets = res.json()
        if not assets:
            raise RuntimeError(f"No Java {feature} build for {os_name} {arch}")
        package = assets[0]["binary"]["package"]

        os.makedirs(self.java_dir, exist_ok=True)
        work = tempfile.mkdtemp(prefix=f".temurin-{feature}-", dir=self.java_dir)
        try:
            archive = os.path.join(work, package["name"])
            digest = hashlib.sha256()
            with requests.get(package["link"], stream=True, timeout=60) as dl:
                dl.raise_for_status()
                with open(archive, "wb") as f:
                    for chunk in dl.iter_content(chunk_size=1024 * 256):
                        digest.update(chunk)
                        f.write(chunk)
            if digest.hexdigest() != package["checksum"]:
                raise RuntimeError(f"Checksum mismatch for {package['name']}")

            extract_to = os.path.join(work, "x")
            self._extract(archive, extract_to)
            entries = os.listdir(extract_to)
            top = os.path.join(extract_to, entries[0]) if len(entries) == 1 else None
            source = top if top and os.path.isdir(top) else extract_to

            target = self.install_dir(feature)
            if os.path.exists(target):
                shutil.rmtree(target)
            shutil.move(source, target)
            logger.info(f"Installed Java {feature} into {target}")
        finally:
            shutil.rmtree(work, ignore_errors=True)

    @staticmethod
    def _extract(archive, dest):
        os.makedirs(dest)
        real_dest = os.path.realpath(dest)

        def inside(name):
            path = os.path.realpath(os.path.join(dest, name))
            return path == real_dest or path.startswith(real_dest + os.sep)

        if archive.endswith(".zip"):
            with zipfile.ZipFile(archive) as zf:
                if not all(inside(n) for n in zf.namelist()):
                    raise RuntimeError("Unsafe path in Java archive")
                zf.extractall(dest)
            return
        with tarfile.open(archive) as tf:
            if hasattr(tarfile, "data_filter"):
                tf.extractall(dest, filter="data")
                return
            members = tf.getmembers()
            if not all(inside(m.name) and not m.isdev() for m in members):
                raise RuntimeError("Unsafe path in Java archive")
            tf.extractall(dest, members=members)
