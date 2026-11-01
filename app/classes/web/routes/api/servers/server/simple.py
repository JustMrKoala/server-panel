"""API behind the Simple server view: settings, game properties, players, plugins."""

import datetime
import hashlib
import json
import logging
import os
import re
import shutil
import uuid
import zipfile
from urllib.parse import urlparse

import requests
from jsonschema import validate
from jsonschema.exceptions import ValidationError
from tornado.ioloop import IOLoop

from app.classes.helpers.helpers import Helpers
from app.classes.helpers.java_manager import SUPPORTED_FEATURES, JavaManager
from app.classes.models.server_permissions import EnumPermissionsServer
from app.classes.web.base_api_handler import BaseApiHandler

logger = logging.getLogger(__name__)

MODRINTH_API = "https://api.modrinth.com/v2"
MODRINTH_HEADERS = {
    "User-Agent": "server-panel (https://github.com/JustMrKoala/server-panel)"
}
MAX_PLUGIN_BYTES = 256 * 1024 * 1024
PLAYER_NAME = re.compile(r"^[A-Za-z0-9_]{1,16}$")
PROPERTY_KEY = re.compile(r"^[A-Za-z0-9._-]{1,100}$")

# Where each server type keeps its add-ons and which Modrinth loaders fit it
LOADERS = {
    "paper": ("plugins", "plugin", ["paper", "spigot", "bukkit"]),
    "purpur": ("plugins", "plugin", ["purpur", "paper", "spigot", "bukkit"]),
    "folia": ("plugins", "plugin", ["folia"]),
    "spigot": ("plugins", "plugin", ["spigot", "bukkit"]),
    "velocity": ("plugins", "plugin", ["velocity"]),
    "bungeecord": ("plugins", "plugin", ["bungeecord", "waterfall"]),
    "fabric": ("mods", "mod", ["fabric"]),
    "quilt": ("mods", "mod", ["quilt", "fabric"]),
    "forge": ("mods", "mod", ["forge"]),
    "neoforge": ("mods", "mod", ["neoforge"]),
}
NAME_HINTS = [
    ("purpur", "purpur"),
    ("folia", "folia"),
    ("paper", "paper"),
    ("pufferfish", "paper"),
    ("spigot", "spigot"),
    ("bukkit", "spigot"),
    ("velocity", "velocity"),
    ("waterfall", "bungeecord"),
    ("bungee", "bungeecord"),
    ("quilt", "quilt"),
    ("fabric", "fabric"),
    ("neoforge", "neoforge"),
    ("forge", "forge"),
]

settings_schema = {
    "type": "object",
    "properties": {
        "memory_min_mb": {"type": "integer", "minimum": 128, "maximum": 1048576},
        "memory_max_mb": {"type": "integer", "minimum": 256, "maximum": 1048576},
        "java": {
            "type": "string",
            "enum": ["auto", "system"] + [str(f) for f in SUPPORTED_FEATURES],
        },
        "auto_start": {"type": "boolean"},
        "crash_detection": {"type": "boolean"},
    },
    "additionalProperties": False,
    "minProperties": 1,
}
properties_schema = {
    "type": "object",
    "properties": {
        "properties": {
            "type": "object",
            "propertyNames": {"pattern": PROPERTY_KEY.pattern},
            "additionalProperties": {"type": "string", "maxLength": 4096},
        }
    },
    "required": ["properties"],
    "additionalProperties": False,
}
players_schema = {
    "type": "object",
    "properties": {
        "list": {"type": "string", "enum": ["whitelist", "ops", "banned"]},
        "action": {"type": "string", "enum": ["add", "remove"]},
        "name": {"type": "string", "pattern": PLAYER_NAME.pattern},
    },
    "required": ["list", "action", "name"],
    "additionalProperties": False,
}
plugin_install_schema = {
    "type": "object",
    "properties": {"project_id": {"type": "string", "pattern": r"^[A-Za-z0-9]{1,64}$"}},
    "required": ["project_id"],
    "additionalProperties": False,
}
plugin_change_schema = {
    "type": "object",
    "properties": {
        "file": {"type": "string", "maxLength": 255},
        "action": {"type": "string", "enum": ["enable", "disable", "delete"]},
    },
    "required": ["file", "action"],
    "additionalProperties": False,
}

PLAYER_FILES = {
    "whitelist": "whitelist.json",
    "ops": "ops.json",
    "banned": "banned-players.json",
}
PLAYER_COMMANDS = {
    ("whitelist", "add"): "whitelist add {}",
    ("whitelist", "remove"): "whitelist remove {}",
    ("ops", "add"): "op {}",
    ("ops", "remove"): "deop {}",
    ("banned", "add"): "ban {}",
    ("banned", "remove"): "pardon {}",
}


# ---------------------------------------------------------------------- helpers


def detect_loader(server_path, executable):
    name = (executable or "").lower()
    for hint, loader in NAME_HINTS:
        if hint in name:
            return loader
    checks = [
        ("libraries/net/neoforged", "neoforge"),
        ("libraries/net/minecraftforge", "forge"),
        ("fabric-server-launcher.properties", "fabric"),
        (".fabric", "fabric"),
    ]
    for rel, loader in checks:
        if os.path.exists(os.path.join(server_path, rel)):
            return loader
    jar = os.path.join(server_path, executable or "")
    if jar.endswith(".jar") and os.path.isfile(jar):
        try:
            with zipfile.ZipFile(jar) as zf:
                manifest = zf.read("META-INF/MANIFEST.MF").decode("utf-8", "ignore")
            if "paperclip" in manifest.lower():
                return "paper"
            if "velocitypowered" in manifest:
                return "velocity"
        except (OSError, KeyError, zipfile.BadZipFile):
            pass
    return "vanilla"


def split_command(command):
    """Split off the first token (the program) of a start command."""
    match = re.match(r'\s*("[^"]*"|\S+)(.*)$', command or "", re.S)
    if not match:
        return "", ""
    return match.group(1), match.group(2)


def is_java_token(token):
    base = os.path.basename(token.strip('"')).lower()
    return base in ("java", "java.exe", "javaw", "javaw.exe")


def parse_memory(command):
    def mb(flag):
        match = re.search(rf"-{flag}(\d+)([kKmMgG]?)\b", command or "")
        if not match:
            return None
        value, unit = int(match.group(1)), match.group(2).lower()
        return {"k": value // 1024, "g": value * 1024}.get(unit, value)

    return {"min": mb("Xms"), "max": mb("Xmx")}


def set_memory(command, flag, megabytes):
    pattern = rf"-{flag}\d+[kKmMgG]?\b"
    if re.search(pattern, command):
        return re.sub(pattern, f"-{flag}{megabytes}M", command, count=1)
    first, rest = split_command(command)
    return f"{first} -{flag}{megabytes}M{rest}"


def read_properties(path):
    """Return (lines, {key: value}) keeping comments and order."""
    lines, values = [], {}
    if os.path.isfile(path):
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            lines = f.read().splitlines()
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped[0] in "#!":
            continue
        parts = re.split(r"[=:]", line, maxsplit=1)
        if len(parts) == 2:
            values[parts[0].strip()] = parts[1]
    return lines, values


def offline_uuid(name):
    digest = bytearray(hashlib.md5(f"OfflinePlayer:{name}".encode()).digest())
    digest[6] = (digest[6] & 0x0F) | 0x30
    digest[8] = (digest[8] & 0x3F) | 0x80
    return str(uuid.UUID(bytes=bytes(digest)))


def lookup_uuid(name, online_mode):
    if not online_mode:
        return offline_uuid(name), name
    res = requests.get(
        f"https://api.mojang.com/users/profiles/minecraft/{name}", timeout=10
    )
    if res.status_code != 200:
        raise LookupError(f"No Minecraft account named {name}")
    data = res.json()
    raw = data["id"]
    return str(uuid.UUID(raw)), data.get("name", name)


def run_blocking(fn, *args):
    return IOLoop.current().run_in_executor(None, fn, *args)


class SimpleApiBase(BaseApiHandler):
    """Shared access checks for the Simple view endpoints."""

    def deny(self, auth_data, error="NOT_AUTHORIZED", message=None, status=400):
        self.finish_json(
            status,
            {
                "status": "error",
                "error": error,
                "error_data": message
                or self.helper.translation.translate(
                    "validators", "insufficientPerms", auth_data[4]["lang"]
                ),
            },
        )

    def access(self, server_id, permission=None):
        """Return (auth_data, server_instance, permissions) or None after replying."""
        auth_data = self.authenticate_user()
        if not auth_data:
            return None
        if server_id not in [str(x["server_id"]) for x in auth_data[0]]:
            self.deny(auth_data)
            return None
        mask = self.controller.server_perms.get_lowest_api_perm_mask(
            self.controller.server_perms.get_user_permissions_mask(
                auth_data[4]["user_id"], server_id
            ),
            auth_data[5],
        )
        permissions = self.controller.server_perms.get_permissions(mask)
        if permission is not None and permission not in permissions:
            self.deny(auth_data)
            return None
        server = self.controller.servers.get_server_instance_by_id(server_id)
        if server is None:
            self.deny(auth_data, "NOT_FOUND", "Server not found", 404)
            return None
        return auth_data, server, permissions

    def body_json(self, auth_data, schema):
        try:
            data = json.loads(self.request.body)
            validate(data, schema)
            return data
        except (json.decoder.JSONDecodeError, ValidationError) as why:
            message = getattr(why, "message", str(why))
            self.deny(auth_data, "INVALID_JSON", message)
            return None

    @staticmethod
    def server_path(server):
        return Helpers.get_os_understandable_path(server.settings["path"])

    def audit(self, auth_data, server_id, message):
        self.controller.management.add_to_audit_log(
            auth_data[4]["user_id"], message, server_id, self.get_remote_ip()
        )


# ---------------------------------------------------------------------- overview


class ApiServersServerSimpleHandler(SimpleApiBase):
    def get(self, server_id: str):
        found = self.access(server_id)
        if not found:
            return
        _, server, permissions = found
        path = self.server_path(server)
        manager = JavaManager(self.helper)
        detected = manager.detect(path, server.settings["executable"])
        command = server.settings["execution_command"]
        first, _ = split_command(command)

        if first == "java":
            java_choice = "auto"
        elif manager.feature_of_path(first):
            java_choice = str(manager.feature_of_path(first))
        elif is_java_token(first):
            java_choice = "system"
        else:
            java_choice = "custom"

        _, props = read_properties(os.path.join(path, "server.properties"))
        loader = detect_loader(path, server.settings["executable"])
        self.finish_json(
            200,
            {
                "status": "ok",
                "data": {
                    "type": server.settings["type"],
                    "mc_version": detected["mc_version"],
                    "java_required": detected["java"],
                    "java_choice": java_choice,
                    "java_installed": manager.installed(),
                    "java_options": SUPPORTED_FEATURES,
                    "java_editable": is_java_token(first),
                    "auto_java": self.helper.get_all_settings().get("auto_java", True),
                    "memory": parse_memory(command),
                    "auto_start": bool(server.settings["auto_start"]),
                    "crash_detection": bool(server.settings["crash_detection"]),
                    "port": props.get("server-port", "25565").strip(),
                    "loader": loader,
                    "addons": LOADERS.get(loader, (None, None, None))[0],
                    "running": server.check_running(),
                    "can": {
                        "config": EnumPermissionsServer.CONFIG in permissions,
                        "players": EnumPermissionsServer.PLAYERS in permissions,
                        "files": EnumPermissionsServer.FILES in permissions,
                        "commands": EnumPermissionsServer.COMMANDS in permissions,
                        "terminal": EnumPermissionsServer.TERMINAL in permissions,
                    },
                },
            },
        )


class ApiServersServerSimpleSettingsHandler(SimpleApiBase):
    async def patch(self, server_id: str):
        found = self.access(server_id, EnumPermissionsServer.CONFIG)
        if not found:
            return
        auth_data, server, _ = found
        data = self.body_json(auth_data, settings_schema)
        if data is None:
            return

        server_obj = self.controller.servers.get_server_obj(server_id)
        command = server_obj.execution_command
        first, rest = split_command(command)

        if "java" in data:
            if not is_java_token(first):
                return self.deny(
                    auth_data, "NOT_JAVA", "This server is not started with Java."
                )
            choice = data["java"]
            if choice == "auto":
                first = "java"
            elif choice == "system":
                system_java = shutil.which("java")
                if not system_java:
                    return self.deny(
                        auth_data, "NO_SYSTEM_JAVA", "Java is not installed on the host."
                    )
                first = f'"{system_java}"'
            else:
                manager = JavaManager(self.helper)
                try:
                    path = await run_blocking(manager.ensure, int(choice))
                except Exception as e:
                    logger.exception(f"Java install failed: {e}")
                    return self.deny(
                        auth_data, "JAVA_INSTALL_FAILED", f"Could not install Java: {e}"
                    )
                first = f'"{path}"'
            command = f"{first}{rest}"

        memory = parse_memory(command)
        new_min = data.get("memory_min_mb", memory["min"])
        new_max = data.get("memory_max_mb", memory["max"])
        if "memory_min_mb" in data or "memory_max_mb" in data:
            if new_min and new_max and new_min > new_max:
                return self.deny(
                    auth_data,
                    "INVALID_MEMORY",
                    "Minimum memory can not be higher than maximum memory.",
                )
            if "memory_max_mb" in data:
                command = set_memory(command, "Xmx", new_max)
            if "memory_min_mb" in data:
                command = set_memory(command, "Xms", new_min)

        server_obj.execution_command = command.strip()
        for key in ("auto_start", "crash_detection"):
            if key in data:
                setattr(server_obj, key, data[key])
        self.controller.servers.update_server(server_obj)
        self.audit(auth_data, server_id, "changed server settings in the simple view")
        self.finish_json(200, {"status": "ok"})


# ---------------------------------------------------------------------- properties


class ApiServersServerSimplePropertiesHandler(SimpleApiBase):
    def get(self, server_id: str):
        found = self.access(server_id, EnumPermissionsServer.CONFIG)
        if not found:
            return
        _, server, _ = found
        path = os.path.join(self.server_path(server), "server.properties")
        _, values = read_properties(path)
        self.finish_json(
            200,
            {
                "status": "ok",
                "data": {"exists": os.path.isfile(path), "properties": values},
            },
        )

    def put(self, server_id: str):
        found = self.access(server_id, EnumPermissionsServer.CONFIG)
        if not found:
            return
        auth_data, server, _ = found
        data = self.body_json(auth_data, properties_schema)
        if data is None:
            return
        updates = data["properties"]
        if any("\n" in v or "\r" in v for v in updates.values()):
            return self.deny(auth_data, "INVALID_VALUE", "Values can not contain newlines.")

        path = os.path.join(self.server_path(server), "server.properties")
        lines, _ = read_properties(path)
        pending = dict(updates)
        output = []
        for line in lines:
            stripped = line.strip()
            if stripped and stripped[0] not in "#!":
                key = re.split(r"[=:]", line, maxsplit=1)[0].strip()
                if key in pending:
                    output.append(f"{key}={pending.pop(key)}")
                    continue
            output.append(line)
        output += [f"{k}={v}" for k, v in pending.items()]
        with open(path, "w", encoding="utf-8", newline="\n") as f:
            f.write("\n".join(output) + "\n")
        self.audit(auth_data, server_id, "edited server.properties in the simple view")
        self.finish_json(200, {"status": "ok"})


# ---------------------------------------------------------------------- players


class ApiServersServerSimplePlayersHandler(SimpleApiBase):
    def get(self, server_id: str):
        found = self.access(server_id, EnumPermissionsServer.PLAYERS)
        if not found:
            return
        _, server, _ = found
        path = self.server_path(server)
        lists = {}
        for key, file_name in PLAYER_FILES.items():
            try:
                with open(os.path.join(path, file_name), "r", encoding="utf-8") as f:
                    entries = json.load(f)
            except (OSError, ValueError):
                entries = []
            lists[key] = sorted(
                (e.get("name") for e in entries if isinstance(e, dict) and e.get("name")),
                key=str.lower,
            )
        self.finish_json(200, {"status": "ok", "data": lists})

    async def post(self, server_id: str):
        found = self.access(server_id, EnumPermissionsServer.PLAYERS)
        if not found:
            return
        auth_data, server, _ = found
        data = self.body_json(auth_data, players_schema)
        if data is None:
            return
        kind, action, name = data["list"], data["action"], data["name"]

        if server.check_running():
            server.send_command(PLAYER_COMMANDS[(kind, action)].format(name))
            self.audit(auth_data, server_id, f"{action} {name} ({kind}) in the simple view")
            return self.finish_json(200, {"status": "ok", "data": {"live": True}})

        path = self.server_path(server)
        file_path = os.path.join(path, PLAYER_FILES[kind])
        try:
            with open(file_path, "r", encoding="utf-8") as f:
                entries = json.load(f)
        except (OSError, ValueError):
            entries = []

        if action == "remove":
            entries = [
                e for e in entries if str(e.get("name", "")).lower() != name.lower()
            ]
        elif not any(str(e.get("name", "")).lower() == name.lower() for e in entries):
            _, props = read_properties(os.path.join(path, "server.properties"))
            online = props.get("online-mode", "true").strip().lower() != "false"
            try:
                player_uuid, name = await run_blocking(lookup_uuid, name, online)
            except (LookupError, requests.RequestException) as e:
                return self.deny(auth_data, "PLAYER_NOT_FOUND", str(e))
            entry = {"uuid": player_uuid, "name": name}
            if kind == "ops":
                entry.update({"level": 4, "bypassesPlayerLimit": False})
            elif kind == "banned":
                now = datetime.datetime.now().astimezone()
                entry.update(
                    {
                        "created": now.strftime("%Y-%m-%d %H:%M:%S %z"),
                        "source": "Server Panel",
                        "expires": "forever",
                        "reason": "Banned by an operator.",
                    }
                )
            entries.append(entry)

        with open(file_path, "w", encoding="utf-8") as f:
            json.dump(entries, f, indent=2)
        self.audit(auth_data, server_id, f"{action} {name} ({kind}) in the simple view")
        self.finish_json(200, {"status": "ok", "data": {"live": False}})


# ---------------------------------------------------------------------- plugins


def modrinth_get(path, **params):
    res = requests.get(
        f"{MODRINTH_API}{path}", params=params, headers=MODRINTH_HEADERS, timeout=15
    )
    res.raise_for_status()
    return res.json()


def identify_jars(paths):
    """Map file names to Modrinth projects using their SHA1 hashes."""
    hashes = {}
    for path in paths:
        sha1 = hashlib.sha1()
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(1024 * 256), b""):
                sha1.update(chunk)
        hashes[sha1.hexdigest()] = os.path.basename(path)
    if not hashes:
        return {}
    try:
        res = requests.post(
            f"{MODRINTH_API}/version_files",
            json={"hashes": list(hashes), "algorithm": "sha1"},
            headers=MODRINTH_HEADERS,
            timeout=10,
        )
        res.raise_for_status()
        versions = res.json()
        project_ids = sorted({v["project_id"] for v in versions.values()})
        projects = {}
        if project_ids:
            for p in modrinth_get("/projects", ids=json.dumps(project_ids)):
                projects[p["id"]] = p
    except (requests.RequestException, ValueError, KeyError):
        return {}
    found = {}
    for sha1, version in versions.items():
        project = projects.get(version.get("project_id"), {})
        found[hashes[sha1]] = {
            "project_id": version.get("project_id"),
            "title": project.get("title"),
            "icon_url": project.get("icon_url"),
            "version": version.get("version_number"),
        }
    return found


class ApiServersServerSimplePluginsHandler(SimpleApiBase):
    def addon_dir(self, server):
        path = self.server_path(server)
        loader = detect_loader(path, server.settings["executable"])
        if loader not in LOADERS:
            return loader, None
        return loader, os.path.join(path, LOADERS[loader][0])

    async def get(self, server_id: str):
        found = self.access(server_id, EnumPermissionsServer.FILES)
        if not found:
            return
        _, server, _ = found
        loader, folder = self.addon_dir(server)
        installed = []
        if folder and os.path.isdir(folder):
            jars = [
                os.path.join(folder, f)
                for f in sorted(os.listdir(folder), key=str.lower)
                if f.endswith((".jar", ".jar.disabled"))
            ]
            known = await run_blocking(identify_jars, jars)
            for jar in jars:
                file_name = os.path.basename(jar)
                info = known.get(file_name, {})
                installed.append(
                    {
                        "file": file_name,
                        "enabled": file_name.endswith(".jar"),
                        "size": os.path.getsize(jar),
                        "title": info.get("title")
                        or re.sub(r"\.jar(\.disabled)?$", "", file_name),
                        "icon_url": info.get("icon_url"),
                        "version": info.get("version"),
                        "project_id": info.get("project_id"),
                    }
                )
        self.finish_json(
            200,
            {
                "status": "ok",
                "data": {
                    "loader": loader,
                    "supported": folder is not None,
                    "folder": LOADERS.get(loader, (None,))[0],
                    "kind": LOADERS.get(loader, (None, None))[1],
                    "installed": installed,
                },
            },
        )

    async def post(self, server_id: str):
        found = self.access(server_id, EnumPermissionsServer.FILES)
        if not found:
            return
        auth_data, server, _ = found
        data = self.body_json(auth_data, plugin_install_schema)
        if data is None:
            return
        loader, folder = self.addon_dir(server)
        if not folder:
            return self.deny(auth_data, "UNSUPPORTED", "This server type has no plugins.")
        mc_version = JavaManager(self.helper).detect(
            self.server_path(server), server.settings["executable"]
        )["mc_version"]
        try:
            file_name = await run_blocking(
                self.install, data["project_id"], loader, mc_version, folder
            )
        except LookupError as e:
            return self.deny(auth_data, "NO_COMPATIBLE_VERSION", str(e))
        except (requests.RequestException, ValueError, OSError) as e:
            logger.exception(f"Plugin install failed: {e}")
            return self.deny(auth_data, "INSTALL_FAILED", f"Install failed: {e}")
        self.audit(auth_data, server_id, f"installed {file_name} from Modrinth")
        self.finish_json(200, {"status": "ok", "data": {"file": file_name}})

    @staticmethod
    def install(project_id, loader, mc_version, folder):
        params = {"loaders": json.dumps(LOADERS[loader][2])}
        if mc_version:
            params["game_versions"] = json.dumps([mc_version])
        versions = modrinth_get(f"/project/{project_id}/version", **params)
        if not versions:
            raise LookupError(
                f"No version of this project works with {loader} {mc_version or ''}."
            )
        files = versions[0]["files"]
        chosen = next((f for f in files if f.get("primary")), files[0])
        url = chosen["url"]
        if urlparse(url).hostname != "cdn.modrinth.com":
            raise ValueError("Unexpected download host")
        file_name = os.path.basename(chosen["filename"])
        if not re.match(r"^[A-Za-z0-9._+() -]+\.jar$", file_name):
            raise ValueError("Unexpected file name")

        os.makedirs(folder, exist_ok=True)
        target = os.path.join(folder, file_name)
        partial = target + ".part"
        digest = hashlib.sha512()
        size = 0
        with requests.get(url, stream=True, headers=MODRINTH_HEADERS, timeout=60) as dl:
            dl.raise_for_status()
            with open(partial, "wb") as f:
                for chunk in dl.iter_content(chunk_size=1024 * 256):
                    size += len(chunk)
                    if size > MAX_PLUGIN_BYTES:
                        raise ValueError("Download is too large")
                    digest.update(chunk)
                    f.write(chunk)
        try:
            if digest.hexdigest() != chosen["hashes"]["sha512"]:
                raise ValueError("Checksum mismatch")
            os.replace(partial, target)
        finally:
            if os.path.exists(partial):
                os.remove(partial)
        return file_name

    def patch(self, server_id: str):
        found = self.access(server_id, EnumPermissionsServer.FILES)
        if not found:
            return
        auth_data, server, _ = found
        data = self.body_json(auth_data, plugin_change_schema)
        if data is None:
            return
        _, folder = self.addon_dir(server)
        file_name = data["file"]
        if (
            not folder
            or file_name != os.path.basename(file_name)
            or not file_name.endswith((".jar", ".jar.disabled"))
            or not os.path.isfile(os.path.join(folder, file_name))
        ):
            return self.deny(auth_data, "NOT_FOUND", "Plugin file not found.", 404)

        path = os.path.join(folder, file_name)
        action = data["action"]
        if action == "delete":
            os.remove(path)
        elif action == "disable" and file_name.endswith(".jar"):
            os.replace(path, path + ".disabled")
        elif action == "enable" and file_name.endswith(".disabled"):
            os.replace(path, path[: -len(".disabled")])
        self.audit(auth_data, server_id, f"{action} plugin {file_name}")
        self.finish_json(200, {"status": "ok"})


class ApiServersServerSimplePluginSearchHandler(SimpleApiBase):
    async def get(self, server_id: str):
        found = self.access(server_id, EnumPermissionsServer.FILES)
        if not found:
            return
        auth_data, server, _ = found
        path = self.server_path(server)
        loader = detect_loader(path, server.settings["executable"])
        if loader not in LOADERS:
            return self.deny(auth_data, "UNSUPPORTED", "This server type has no plugins.")
        _, kind, loaders = LOADERS[loader]
        mc_version = JavaManager(self.helper).detect(path, server.settings["executable"])[
            "mc_version"
        ]
        facets = [
            [f"categories:{name}" for name in loaders],
            [f"project_type:{kind}"],
        ]
        if mc_version:
            facets.append([f"versions:{mc_version}"])
        if kind == "mod":
            facets.append(["server_side:required", "server_side:optional"])
        query = self.get_query_argument("q", "")[:100]
        try:
            offset = max(0, int(self.get_query_argument("offset", "0")))
        except ValueError:
            offset = 0
        try:
            result = await run_blocking(
                lambda: modrinth_get(
                    "/search",
                    query=query,
                    facets=json.dumps(facets),
                    index="relevance" if query else "downloads",
                    limit=20,
                    offset=offset,
                )
            )
        except requests.RequestException as e:
            return self.deny(auth_data, "SEARCH_FAILED", f"Modrinth is not reachable: {e}")
        hits = [
            {
                "project_id": h["project_id"],
                "slug": h.get("slug"),
                "title": h.get("title"),
                "description": h.get("description"),
                "icon_url": h.get("icon_url"),
                "author": h.get("author"),
                "downloads": h.get("downloads", 0),
            }
            for h in result.get("hits", [])
        ]
        self.finish_json(
            200,
            {
                "status": "ok",
                "data": {
                    "hits": hits,
                    "total": result.get("total_hits", 0),
                    "offset": offset,
                    "mc_version": mc_version,
                    "loader": loader,
                },
            },
        )
