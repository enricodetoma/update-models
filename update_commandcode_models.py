#!/usr/bin/env python3
"""Refresh the CommandCode model catalog in the local OpenCode, ZCode and Codex
configs.

The catalog is written to the OpenCode config, the ZCode CLI config, ZCode's
provider_config.json (the file the ZCode app keeps its provider rules, model ids
and per-model context windows in) and the two Codex files (config.toml, which
both the Codex CLI and the Codex desktop app read, and the generated
~/.codex/codex-models.json model catalog they both show in their model picker)
first; the API key is requested afterwards and stored in a second pass. So the
models are updated even if the key prompt is skipped or aborted, and a blank
answer leaves stored keys untouched.
That prompt is the only interaction: the script exits as soon as the run ends,
pass --log-file and diagnose from the log when it fails after the prompt.

Every step logs a timestamped line to stderr: the environment, name resolution,
the HTTP exchange, each file read and write, and a tailored hint when something
fails. To keep a copy of that log for a machine you are diagnosing:

    python update_models.py --log-file update_models.log

Options that matter on a fresh machine:

    --api-key KEY    skip the interactive prompt (required when stdin is not a terminal)
    --timeout SEC    raise the HTTP timeout on a slow connection (default 30)
    --revert-codex   remove everything this script added to Codex and exit, so Codex is
                     back to its own model list (re-running the script puts it back)

Codex notes: its provider block stores the key inline (experimental_bearer_token),
because Codex only falls back to an env_key that is actually set. Models are
listed through model_catalog_json, a file that REPLACES Codex's built-in catalog,
so the script merges the models Codex already has into that file (which is why it
is called codex-models.json and not commandcode-models.json); re-run it to pick
up new codex models.
"""

import argparse
import json
import os
import platform
import re
import shutil
import socket
import ssl
import subprocess
import sys
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime

PROVIDER_ID = "commandcode"
PROVIDER_NAME = "CommandCode"
BASE_URL = "https://api.commandcode.ai/provider/v1/"
API_URL = BASE_URL + "models"
CONFIG_PATH = os.path.join(
    os.path.expanduser("~"), ".config", "opencode", "opencode.jsonc"
)
AUTH_PATH = os.path.join(
    os.path.expanduser("~"), ".local", "share", "opencode", "auth.json"
)
ZCODE_PROVIDER_CONFIG_PATH = os.path.join(
    os.path.expanduser("~"), ".zcode", "v2", "provider_config.json"
)
ZCODE_CLI_CONFIG_PATH = os.path.join(
    os.path.expanduser("~"), ".zcode", "cli", "config.json"
)
CODEX_HOME = os.path.join(os.path.expanduser("~"), ".codex")
CODEX_CONFIG_PATH = os.path.join(CODEX_HOME, "config.toml")
CODEX_PROFILE_NAME = "commandcode"
CODEX_PROFILE_PATH = os.path.join(CODEX_HOME, CODEX_PROFILE_NAME + ".config.toml")
# Codex picks a provider per session, never per model, so a catalog that mixes two
# providers' models can send a model to the wrong endpoint. The default setup
# therefore keeps everything in the profile file and gives it a catalog of its own
# (CODEX_PROFILE_CATALOG_PATH), leaving config.toml and Codex's own model list
# untouched; --codex-global instead writes the provider into config.toml and uses
# CODEX_CATALOG_PATH, which is what the desktop app picker reads.
CODEX_CATALOG_PATH = os.path.join(CODEX_HOME, "codex-models.json")
CODEX_PROFILE_CATALOG_PATH = os.path.join(CODEX_HOME, "commandcode-models.json")

# Shapes the ZCode app uses inside provider_config.json.
ZCODE_SCHEMA_VERSION = 1
ZCODE_ACCESS_TYPE = "api-key"
ZCODE_API_TYPE = "openai-chat-completions"
ZCODE_GROUP = "standard-personal"
ZCODE_BASE_URL = BASE_URL.rstrip("/")

# Codex only speaks the Responses API since 0.154 (wire_api = "chat" is rejected),
# and only models whose catalog entry names that endpoint can be used with it.
CODEX_BASE_URL = BASE_URL.rstrip("/")
CODEX_WIRE_API = "responses"
CODEX_RESPONSES_ENDPOINT = "/responses"
CODEX_CATALOG_KEY = "model_catalog_json"
CODEX_PROVIDER_TABLE = ("model_providers", PROVIDER_ID)
CODEX_DEBUG_MODELS_TIMEOUT = 60.0
# Codex sorts the model picker by priority; the built-in models use 1..12.
CODEX_PRIORITY_BASE = 100

USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"

TARGETS = (
    ("OpenCode config", "CONFIG_PATH"),
    ("OpenCode auth", "AUTH_PATH"),
    ("ZCode provider config", "ZCODE_PROVIDER_CONFIG_PATH"),
    ("ZCode CLI config", "ZCODE_CLI_CONFIG_PATH"),
    ("Codex config", "CODEX_CONFIG_PATH"),
    ("Codex model catalog", "CODEX_CATALOG_PATH"),
    ("Codex profile", "CODEX_PROFILE_PATH"),
)
PROXY_ENV_VARS = (
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "ALL_PROXY",
    "NO_PROXY",
    "http_proxy",
    "https_proxy",
    "all_proxy",
    "no_proxy",
)
TLS_ENV_VARS = (
    "SSL_CERT_FILE",
    "SSL_CERT_DIR",
    "REQUESTS_CA_BUNDLE",
    "CURL_CA_BUNDLE",
)

# Set by main() when --log-file is given; every log line is mirrored there.
_LOG_HANDLE = None


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------


def log(message, level="INFO"):
    """Write timestamped diagnostic lines to stderr and the log file, if any.

    Every line of a multi-line message is stamped, so a log file can be filtered
    by level with grep and multi-line blocks (JSON snippets, carets) stay readable.
    """
    stamp = f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')} [{level:<5}] "
    lines = [stamp + part for part in str(message).splitlines()] or [stamp]
    try:
        print("\n".join(lines), file=sys.stderr, flush=True)
    except Exception:
        # A console that cannot print must not hide the rest of the diagnosis.
        pass
    if _LOG_HANDLE is not None:
        try:
            _LOG_HANDLE.write("\n".join(lines) + "\n")
            _LOG_HANDLE.flush()
        except OSError:
            pass


def log_step(title):
    log("")
    log(f"=== {title} ===")


def log_block(text, level="DEBUG", limit=12):
    """Log the first `limit` lines of a multi-line blob (a response body, traceback)."""
    lines = text.rstrip().splitlines()
    for line in lines[:limit]:
        log(f"    {line}", level)
    if len(lines) > limit:
        log(f"    ... {len(lines) - limit} more lines", level)


def redact(secret):
    """Describe a secret without printing it in full."""
    if secret is None:
        return "<not set>"
    secret = str(secret)
    if not secret:
        return "<empty>"
    if len(secret) <= 8:
        return f"<{len(secret)} characters, hidden>"
    return f"{secret[:4]}...{secret[-4:]} ({len(secret)} characters)"


def short_list(items, limit=15):
    items = list(items)
    shown = ", ".join(items[:limit])
    if len(items) > limit:
        shown += f", ... ({len(items) - limit} more)"
    return shown or "none"


# ---------------------------------------------------------------------------
# Inspection helpers
# ---------------------------------------------------------------------------


def target_paths():
    """Label/path pairs for every config the script touches, read at call time."""
    return [(label, globals()[name]) for label, name in TARGETS]


def describe_path(path):
    """Summarise a path's state, so 'missing on the new PC' is obvious in the log."""
    try:
        if not os.path.exists(path):
            parent = os.path.dirname(path)
            if os.path.isdir(parent):
                return "missing (parent directory exists)"
            if os.path.exists(parent):
                return f"missing (parent {parent} is not a directory)"
            return f"missing (parent directory {parent} does not exist either)"
        if os.path.isdir(path):
            return "exists but is a DIRECTORY, not a file"
        stat = os.stat(path)
        modified = datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d %H:%M:%S")
        return f"exists, {stat.st_size} bytes, modified {modified}"
    except OSError as exc:
        return f"cannot be inspected: {exc!r}"


def describe_dict(data):
    if isinstance(data, dict):
        return f"{len(data)} keys: {short_list(sorted(data.keys()))}"
    return f"{type(data).__name__} (not an object)"


def describe_stdin():
    try:
        return bool(sys.stdin and sys.stdin.isatty())
    except Exception:
        return False


def log_environment():
    log(f"python: {sys.version.splitlines()[0]} at {sys.executable}")
    log(f"platform: {platform.platform()}")
    log(f"cwd: {os.getcwd()}")
    log(f"script: {os.path.abspath(__file__)}")
    log(f"home (~): {os.path.expanduser('~')}")
    for name in (
        "HOME",
        "USERPROFILE",
        "HOMEDRIVE",
        "HOMEPATH",
        "APPDATA",
        "XDG_CONFIG_HOME",
        "XDG_DATA_HOME",
    ):
        if os.environ.get(name):
            log(f"env {name}={os.environ[name]}")
    proxies = [f"{name}={os.environ[name]}" for name in PROXY_ENV_VARS if os.environ.get(name)]
    log(f"proxy environment: {short_list(proxies) if proxies else 'none set'}")
    for name in TLS_ENV_VARS:
        if os.environ.get(name):
            log(f"env {name}={os.environ[name]}")
    log(f"ssl: {ssl.OPENSSL_VERSION}")
    log(f"stdin is a terminal: {describe_stdin()}")


def log_dns(host):
    log(f"resolving {host} ...")
    try:
        infos = socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
    except OSError as exc:
        log(f"DNS lookup for {host} failed: {exc!r}", "ERROR")
        log(
            "  this machine cannot resolve the API host at all; check the network "
            "connection, DNS settings, VPN and proxy of the new PC",
            "ERROR",
        )
        return
    addresses = sorted({info[4][0] for info in infos})
    log(f"DNS: {host} -> {', '.join(addresses)}")


def ensure_dict(parent, key, label):
    """Return parent[key] as a dict, replacing (and logging) a non-dict value."""
    value = parent.get(key)
    if isinstance(value, dict):
        return value
    if value is not None:
        log(
            f"{label}: expected an object at {key!r} but found "
            f"{type(value).__name__}; replacing it with a fresh object",
            "WARN",
        )
    value = {}
    parent[key] = value
    return value


def ensure_list(parent, key, label):
    """Return parent[key] as a list, replacing (and logging) a non-list value."""
    value = parent.get(key)
    if isinstance(value, list):
        return value
    if value is not None:
        log(
            f"{label}: expected a list at {key!r} but found "
            f"{type(value).__name__}; replacing it with a fresh list",
            "WARN",
        )
    value = []
    parent[key] = value
    return value


def log_model_changes(previous, current, indent="  "):
    if previous is None:
        log(f"{indent}models: (none) -> {len(current)}")
        return
    added = sorted(set(current) - set(previous))
    removed = sorted(set(previous) - set(current))
    log(
        f"{indent}models: {len(previous)} -> {len(current)} "
        f"({len(added)} added, {len(removed)} removed)"
    )
    if added:
        log(f"{indent}added: {short_list(added)}", "DEBUG")
    if removed:
        log(f"{indent}removed: {short_list(removed)}", "DEBUG")


# ---------------------------------------------------------------------------
# Network
# ---------------------------------------------------------------------------


def fetch_models(timeout=30.0):
    log_step("Fetching the model catalog")
    log(f"GET {API_URL} (timeout {timeout:g}s)")
    host = urllib.parse.urlsplit(API_URL).hostname
    log_dns(host)

    request = urllib.request.Request(API_URL, headers={"User-Agent": USER_AGENT})
    started = time.time()
    try:
        with urllib.request.urlopen(request, timeout=timeout) as resp:
            status = getattr(resp, "status", None) or resp.getcode()
            final_url = resp.geturl()
            headers = resp.headers
            body = resp.read()
    except urllib.error.HTTPError as exc:
        log(f"HTTP {exc.code} {exc.reason} after {time.time() - started:.2f}s", "ERROR")
        if getattr(exc, "headers", None) is not None:
            log(f"  response headers: {dict(exc.headers.items())}", "ERROR")
        try:
            detail = exc.read()[:500]
        except Exception:
            detail = b"<could not read the error body>"
        log(f"  response body (first 500 bytes): {detail!r}", "ERROR")
        if exc.code in (401, 403):
            log("  a 401/403 here means the request needs credentials", "ERROR")
        elif exc.code == 404:
            log(f"  a 404 means the endpoint moved; check API_URL ({API_URL})", "ERROR")
        raise
    except urllib.error.URLError as exc:
        log(f"request failed after {time.time() - started:.2f}s: {exc.reason!r}", "ERROR")
        if isinstance(exc.reason, ssl.SSLError):
            log(
                "  TLS failed before any HTTP response: this Python cannot verify the "
                "server certificate. On a fresh Windows install that usually means a "
                "corporate TLS-inspecting proxy, or Python missing system certificates "
                "(run 'Install Certificates.command' on macOS, or install 'certifi').",
                "ERROR",
            )
        elif isinstance(exc.reason, socket.gaierror):
            log("  the host name could not be resolved (see the DNS lines above)", "ERROR")
        else:
            log(
                "  the connection never completed: check the firewall, proxy and VPN "
                "on this machine",
                "ERROR",
            )
        raise
    except (socket.timeout, TimeoutError):
        log(f"no response within {timeout:g}s (timed out)", "ERROR")
        log("  retry with a larger --timeout, or check for a slow proxy", "ERROR")
        raise

    elapsed = time.time() - started
    log(f"HTTP {status} in {elapsed:.2f}s, {len(body)} bytes")
    if final_url != API_URL:
        log(f"redirected to {final_url}", "WARN")
    log(f"content-type: {headers.get('Content-Type')!r}")
    log(f"server: {headers.get('Server')!r}")

    text = body.decode("utf-8", errors="replace")
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        log(f"the response is not valid JSON: {exc}", "ERROR")
        log(f"  first 500 characters: {text[:500]!r}", "ERROR")
        if "<html" in text[:500].lower():
            log(
                "  this looks like an HTML page: a proxy or captive portal is "
                "intercepting the request instead of the API answering it",
                "ERROR",
            )
        raise

    log(f"response parsed; top level: {describe_dict(data)}")
    if not isinstance(data, dict):
        raise ValueError(f"expected a JSON object from {API_URL}, got {type(data).__name__}")
    if "data" not in data:
        log(
            f"  the response has no 'data' key, so no models can be read "
            f"(keys: {short_list(sorted(data.keys()))})",
            "ERROR",
        )

    models = {}
    endpoints = {}
    skipped = []
    for item in data.get("data", []):
        if not isinstance(item, dict):
            skipped.append(repr(item)[:60])
            continue
        if item.get("object") == "model" and item.get("id"):
            context_length = item.get("context_length")
            model = {"name": item.get("name") or item["id"]}
            if context_length is not None:
                model["limit"] = {"context": context_length, "output": 0}
            models[item["id"]] = model
            supported = item.get("supported_endpoints")
            endpoints[item["id"]] = list(supported) if isinstance(supported, list) else None
            log(
                f"  model {item['id']!r}: name={model['name']!r} "
                f"context_length={context_length!r} "
                f"supported_endpoints={supported!r}",
                "DEBUG",
            )
            if context_length is None:
                log(f"    no context_length for {item['id']!r}; limit left unset", "DEBUG")
        else:
            skipped.append(item.get("id") or repr(item)[:60])
    if skipped:
        log(f"skipped {len(skipped)} entries without object=='model' or an id: {short_list(skipped)}", "WARN")
    log(f"usable models: {len(models)}")
    known = [model_id for model_id, value in endpoints.items() if value]
    if known:
        log(f"models naming their supported endpoints: {len(known)} of {len(models)}")
    return models, endpoints


# ---------------------------------------------------------------------------
# Config files
# ---------------------------------------------------------------------------


def _format_json_error(exc):
    """Render the offending line of a JSON parse failure with a caret."""
    lines = exc.doc.splitlines()
    if not 1 <= exc.lineno <= len(lines):
        return ""
    snippet = lines[exc.lineno - 1]
    caret = " " * (exc.colno - 1) + "^"
    return f"  line {exc.lineno}, column {exc.colno}:\n    {snippet}\n    {caret}"


def _read_text(path, label):
    """Read a text file, logging everything about it. Returns None if it is absent."""
    log(f"reading {label}: {os.path.normpath(path)}")
    log(f"  {describe_path(path)}")
    try:
        with open(path, "r", encoding="utf-8") as f:
            text = f.read()
    except FileNotFoundError:
        log(f"  {label} does not exist yet; it will be created")
        return None
    except PermissionError as exc:
        log(f"  cannot open {path}: {exc!r}", "ERROR")
        log("  check the file permissions and that no other program locked it", "ERROR")
        raise
    except OSError as exc:
        log(f"  cannot read {path}: {exc!r}", "ERROR")
        raise
    log(f"  read {len(text)} characters")
    return text


def _parse_json(text, path, label):
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        log(f"{label} is not valid JSON: {exc}", "ERROR")
        detail = _format_json_error(exc)
        if detail:
            log(detail, "ERROR")
        log(
            "  note: a .jsonc file may contain // comments or trailing commas, which "
            "Python's json module rejects; fix or move the file and re-run "
            "(it was not modified)",
            "ERROR",
        )
        raise
    if not isinstance(data, dict):
        raise ValueError(
            f"{label} ({os.path.normpath(path)}) must contain a JSON object at the top "
            f"level, but it contains {type(data).__name__}"
        )
    log(f"  parsed OK; top level: {describe_dict(data)}")
    return data


def load_config(path, label=None):
    label = label or os.path.basename(path)
    text = _read_text(path, label)
    if text is None:
        return None
    return _parse_json(text, path, label)


def load_auth(path):
    label = "OpenCode auth file"
    text = _read_text(path, label)
    if text is None:
        return {}
    return _parse_json(text, path, label)


def save_config(path, data, label=None):
    label = label or os.path.basename(path)
    payload = json.dumps(data, indent=2) + "\n"
    write_text(path, payload, label, f"top level: {describe_dict(data)}")


def write_text(path, payload, label, summary):
    """Write a config file, then read it back and compare, logging every step."""
    directory = os.path.dirname(path)

    log(f"writing {label}: {os.path.normpath(path)}")
    log(
        f"  {len(payload)} bytes, {summary}; "
        f"directory {'exists' if os.path.isdir(directory) else 'will be created'}"
    )
    try:
        os.makedirs(directory, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(payload)
    except PermissionError as exc:
        log(f"  cannot write {path}: {exc!r}", "ERROR")
        log(
            "  the file is read-only or locked by another program; close the app that "
            "owns it (or run this script as the user that owns the file) and re-run",
            "ERROR",
        )
        log(f"  target state: {describe_path(path)}", "ERROR")
        raise
    except OSError as exc:
        log(
            f"  cannot write {path}: {exc!r} "
            f"(errno={exc.errno}, winerror={getattr(exc, 'winerror', None)})",
            "ERROR",
        )
        raise

    try:
        with open(path, "r", encoding="utf-8") as f:
            written = f.read()
    except OSError as exc:
        log(f"  wrote the file but could not read it back: {exc!r}", "WARN")
        return
    if written == payload:
        log(f"  verified: wrote {len(written)} bytes and read back identical content")
    else:
        log(
            f"  MISMATCH after writing: expected {len(payload)} bytes, "
            f"the file on disk has {len(written)}",
            "ERROR",
        )


def save_auth(path, data):
    save_config(path, data, "OpenCode auth file")


# ---------------------------------------------------------------------------
# Config contents
# ---------------------------------------------------------------------------


def zcode_cli_models(models):
    """Convert the shared catalog to the ZCode CLI's custom-provider model shape."""
    converted = {}
    for model_id, model in models.items():
        zcode_model = {"name": model["name"]}
        limit = model.get("limit")
        if limit is not None and limit.get("context") is not None:
            zcode_model["limit"] = {"context": limit["context"]}
        converted[model_id] = zcode_model
    log(f"converted {len(converted)} models to the ZCode CLI model shape", "DEBUG")
    return converted


def update_zcode_cli_provider(config, models, label="ZCode CLI config", api_key=None):
    """Write the CLI provider block and model catalog. The key is stored later, if known."""
    providers = ensure_dict(config, "provider", label)
    commandcode = ensure_dict(providers, PROVIDER_ID, f"{label} provider {PROVIDER_ID!r}")
    commandcode.setdefault("name", PROVIDER_NAME)
    previous_kind = commandcode.get("kind")
    commandcode["kind"] = "openai-compatible"
    options = ensure_dict(commandcode, "options", f"{label} provider {PROVIDER_ID!r}.options")
    previous_base = options.get("baseURL")
    previous_key = options.get("apiKey")
    options["baseURL"] = BASE_URL
    options["apiKeyRequired"] = True
    if api_key is not None:
        options["apiKey"] = api_key
    commandcode.setdefault("enabled", True)
    commandcode.setdefault("source", "custom")
    previous_models = commandcode.get("models")
    commandcode["models"] = zcode_cli_models(models)

    log(
        f"{label}: provider {PROVIDER_ID!r} kind {previous_kind!r} -> 'openai-compatible', "
        f"enabled={commandcode.get('enabled')!r}, source={commandcode.get('source')!r}"
    )
    if previous_base != BASE_URL:
        log(f"  baseURL: {previous_base!r} -> {BASE_URL!r}")
    else:
        log(f"  baseURL unchanged: {BASE_URL!r}")
    if api_key is None:
        log(f"  apiKey: {redact(previous_key)} (left alone; asked for after the models)")
    else:
        log(f"  apiKey: {redact(previous_key)} -> {redact(api_key)}")
    log_model_changes(previous_models, commandcode["models"])


def apply_zcode_cli_api_key(config, api_key, label="ZCode CLI config"):
    """Store the key in an already-updated CLI provider block. True if the file must be rewritten."""
    providers = ensure_dict(config, "provider", label)
    commandcode = ensure_dict(providers, PROVIDER_ID, f"{label} provider {PROVIDER_ID!r}")
    options = ensure_dict(commandcode, "options", f"{label} provider {PROVIDER_ID!r}.options")
    previous_key = options.get("apiKey")
    if previous_key == api_key:
        log(f"{label}: stored apiKey is already {redact(api_key)}; file left as written")
        return False
    options["apiKey"] = api_key
    log(f"{label}: apiKey {redact(previous_key)} -> {redact(api_key)}")
    return True


def zcode_provider_rule(config, label="ZCode provider config"):
    """The provider_config.json rule for THIS provider, appended when it is absent.

    The app keeps one rule per provider in config.providerConfigRules.providerRules;
    every rule that belongs to another provider is left untouched.
    """
    root = ensure_dict(config, "config", label)
    parent = ensure_dict(root, "providerConfigRules", label)
    rules = ensure_list(parent, "providerRules", label)
    for rule in rules:
        if isinstance(rule, dict) and rule.get("providerId") == PROVIDER_ID:
            return rule
    rule = {
        "providerId": PROVIDER_ID,
        "providerName": PROVIDER_NAME,
        "enabled": True,
        "config": {},
    }
    rules.append(rule)
    log(f"{label}: no rule for {PROVIDER_ID!r} yet; appending a new one")
    return rule


def ordered_model_ids(models, previous_ids):
    """The catalog's model ids, with ids the file already listed kept in place.

    The app shows the models in this order, so one that is still in the catalog stays
    where the file put it and only new ids are appended.
    """
    ordered = []
    for model_id in previous_ids:
        if isinstance(model_id, str) and model_id in models and model_id not in ordered:
            ordered.append(model_id)
    for model_id in models:
        if model_id not in ordered:
            ordered.append(model_id)
    return ordered


def zcode_model_rules(models, previous_rules, label="ZCode provider config"):
    """One config.modelConfigRules entry per model, carrying its context window.

    An entry the file already has keeps whatever else the app stored on it (per-model
    switches, other properties); only the context window is refreshed. A model the
    file does not mention yet gets an entry only when the catalog knows its window,
    since an entry is otherwise an empty rule.
    """
    previous = {}
    for rule in previous_rules:
        if isinstance(rule, dict) and rule.get("providerId") == PROVIDER_ID:
            model_id = rule.get("modelId")
            if isinstance(model_id, str):
                previous.setdefault(model_id, rule)

    entries = []
    for model_id, model in models.items():
        limit = model.get("limit")
        context = limit.get("context") if isinstance(limit, dict) else None
        if model_id not in previous and context is None:
            log(f"{label}: {model_id!r} has no context_length; no context window recorded", "DEBUG")
            continue
        entry = dict(previous.get(model_id, {}))
        entry["modelId"] = model_id
        entry_label = f"{label} entry for {model_id!r}"
        properties = ensure_dict(
            ensure_dict(entry, "config", entry_label), "properties", entry_label
        )
        if context is None:
            if properties.pop("contextWindow", None) is not None:
                log(f"{label}: {model_id!r} lost its context_length; dropped the context window", "WARN")
        else:
            properties["contextWindow"] = context
        entry["providerId"] = PROVIDER_ID
        entries.append(entry)
    return entries


def merge_zcode_model_rules(entries, previous_rules):
    """Place the refreshed entries where the provider's old ones were.

    config.modelConfigRules.providerModelRules is one flat list shared by every
    provider, so the rules of the other providers have to survive the rewrite, in
    their original order.
    """
    foreign = [
        rule
        for rule in previous_rules
        if not (isinstance(rule, dict) and rule.get("providerId") == PROVIDER_ID)
    ]
    if len(foreign) == len(previous_rules):
        return list(entries)
    before = 0
    for rule in previous_rules:
        if isinstance(rule, dict) and rule.get("providerId") == PROVIDER_ID:
            break
        before += 1
    return foreign[:before] + list(entries) + foreign[before:]


def update_zcode_provider_config(config, models, label="ZCode provider config", api_key=None):
    """Write the provider rule and the model rules. The key is stored later, if known."""
    config.setdefault("schemaVersion", ZCODE_SCHEMA_VERSION)
    rule = zcode_provider_rule(config, label)
    rule.setdefault("providerName", PROVIDER_NAME)
    rule.setdefault("enabled", True)

    settings = ensure_dict(rule, "config", f"{label} provider {PROVIDER_ID!r}")
    settings.setdefault("group", ZCODE_GROUP)
    access = ensure_dict(settings, "access", f"{label} provider {PROVIDER_ID!r}.access")
    access.setdefault("type", ZCODE_ACCESS_TYPE)
    previous_key = access.get("apiKey")
    if api_key is not None:
        access["apiKey"] = api_key

    api = ensure_dict(settings, "api", f"{label} provider {PROVIDER_ID!r}.api")
    previous_type = api.get("type")
    previous_base = api.get("baseUrl")
    api["type"] = ZCODE_API_TYPE
    api["baseUrl"] = ZCODE_BASE_URL

    previous_ids = settings.get("personalModelIds")
    ids = ordered_model_ids(models, previous_ids if isinstance(previous_ids, list) else [])
    settings["personalModelIds"] = ids
    settings["modelOrder"] = ids

    model_rules_parent = ensure_dict(
        ensure_dict(config, "config", label), "modelConfigRules", label
    )
    previous_rules = ensure_list(model_rules_parent, "providerModelRules", label)
    ensure_list(model_rules_parent, "manualProviderModelRules", label)
    entries = zcode_model_rules(models, previous_rules, label)
    model_rules_parent["providerModelRules"] = merge_zcode_model_rules(entries, previous_rules)

    log(
        f"{label}: provider {PROVIDER_ID!r} api {previous_type!r} -> {ZCODE_API_TYPE!r}, "
        f"group={settings.get('group')!r}, enabled={rule.get('enabled')!r}"
    )
    if previous_base != ZCODE_BASE_URL:
        log(f"  baseUrl: {previous_base!r} -> {ZCODE_BASE_URL!r}")
    else:
        log(f"  baseUrl unchanged: {ZCODE_BASE_URL!r}")
    if api_key is None:
        log(f"  apiKey: {redact(previous_key)} (left alone; asked for after the models)")
    else:
        log(f"  apiKey: {redact(previous_key)} -> {redact(api_key)}")
    log_model_changes(previous_ids if isinstance(previous_ids, list) else None, ids)
    log(f"  context windows: {len(entries)} of {len(models)} models")


def apply_zcode_provider_api_key(config, api_key, label="ZCode provider config"):
    """Store the key in an already-updated provider rule. True if the file must be rewritten."""
    rule = zcode_provider_rule(config, label)
    settings = ensure_dict(rule, "config", f"{label} provider {PROVIDER_ID!r}")
    access = ensure_dict(settings, "access", f"{label} provider {PROVIDER_ID!r}.access")
    previous_key = access.get("apiKey")
    if previous_key == api_key:
        log(f"{label}: stored apiKey is already {redact(api_key)}; file left as written")
        return False
    access.setdefault("type", ZCODE_ACCESS_TYPE)
    access["apiKey"] = api_key
    log(f"{label}: apiKey {redact(previous_key)} -> {redact(api_key)}")
    return True


def has_valid_model_ref(config, model_ref):
    if not isinstance(model_ref, str) or "/" not in model_ref:
        log(f"  model.main {model_ref!r} is not a 'provider/model' string", "DEBUG")
        return False
    provider_id, model_id = model_ref.split("/", 1)
    if not provider_id or not model_id:
        log(f"  model.main {model_ref!r} has an empty provider or model part", "DEBUG")
        return False
    provider = config.get("provider", {}).get(provider_id)
    if not isinstance(provider, dict):
        log(f"  model.main {model_ref!r} names provider {provider_id!r}, which is not in this config", "DEBUG")
        return False
    models = provider.get("models")
    if isinstance(models, dict) and models and model_id not in models:
        log(f"  model.main {model_ref!r} names model {model_id!r}, which the provider does not list", "DEBUG")
        return False
    return True


def update_zcode_cli_config(config, models, api_key=None):
    update_zcode_cli_provider(config, models, "ZCode CLI config", api_key=api_key)
    model = ensure_dict(config, "model", "ZCode CLI config")
    current = model.get("main")
    if has_valid_model_ref(config, current):
        log(f"ZCode CLI config: keeping model.main = {current!r}")
    else:
        first_model_id = next(iter(models))
        model["main"] = f"{PROVIDER_ID}/{first_model_id}"
        log(
            f"ZCode CLI config: model.main {current!r} was not usable, so it was set to "
            f"{model['main']!r}"
        )


# ---------------------------------------------------------------------------
# Codex (CLI and desktop app)
# ---------------------------------------------------------------------------
#
# The Codex CLI and the Codex desktop app read the same ~/.codex/config.toml, so
# one update covers both. Two things have to happen there:
#
#   [model_providers.commandcode]  makes the provider usable. The key goes into
#       experimental_bearer_token: Codex looks at env_key first and aborts when
#       that variable is missing, even though the inline token is set.
#   model_catalog_json             makes the models show up in the model picker.
#       Codex REPLACES its whole catalog with that file, so the models Codex
#       already offers are copied into it before the CommandCode models are
#       appended; without that, switching the file on would hide every built-in
#       model from the picker. Codex's own GPT models are never duplicated with a
#       CommandCode entry (see CODEX_NATIVE_MODEL_PREFIXES).
#
# Next to config.toml, a <name>.config.toml is a Codex profile file: it is what
# `codex --profile <name>` loads on top of the main config, so one small file
# makes a whole session run on the CommandCode provider.

# Used when no Codex installation can be asked for its own model entries: the
# fields Codex requires on every catalog entry, with a plain agent prompt. There
# is no null in here: Codex rejects a null web_search_tool_type.
CODEX_MINIMAL_PROMPT = (
    "You are Codex, a coding agent working in the user's workspace.\n"
    "\n"
    "Solve the task end to end instead of describing how it could be solved. "
    "Inspect the relevant files first, then make the change and verify it. "
    "Use the provided tools for every file edit and command; do not print "
    "patches as prose. Keep answers short and factual, and say plainly when "
    "something could not be done."
)
CODEX_MINIMAL_MODEL = {
    "slug": "",
    "display_name": "",
    "description": "",
    "default_reasoning_level": None,
    "supported_reasoning_levels": [],
    "shell_type": "unified_exec",
    "visibility": "list",
    "supported_in_api": True,
    "priority": CODEX_PRIORITY_BASE,
    "truncation_policy": {"mode": "tokens", "limit": 10000},
    "support_verbosity": True,
    "default_verbosity": None,
    "apply_patch_tool_type": "freeform",
    "web_search_tool_type": "text",
    "input_modalities": ["text"],
    "context_window": None,
    "max_context_window": None,
    "experimental_supported_tools": [],
    "base_instructions": CODEX_MINIMAL_PROMPT,
    "model_messages": {"instructions_template": CODEX_MINIMAL_PROMPT},
}

# What a CommandCode entry takes from its own model rather than from the
# reference entry; everything else is inherited so the entry stays valid for
# this Codex. tool_mode and the multi-agent settings are cleared because the
# reference may ask for OpenAI-only tools (code mode, sub-agents) that a
# third-party model cannot drive.
CODEX_ENTRY_OVERRIDES = {
    "visibility": "list",
    "supported_in_api": True,
    "supported_reasoning_levels": [],
    "default_reasoning_level": None,
    "experimental_supported_tools": [],
    "service_tiers": [],
    "additional_speed_tiers": [],
    "default_service_tier": None,
    "available_access_programs": None,
    "availability_nux": None,
    "upgrade": None,
    "model_specialty": None,
    "tool_mode": None,
    "multi_agent_version": None,
    "multi_agent_reasoning_effort": None,
    "apply_patch_tool_type": "freeform",
    "web_search_tool_type": "text",
    "input_modalities": ["text"],
}


def toml_string(value):
    """Render a TOML basic string; JSON escaping is valid TOML for these values."""
    return json.dumps(value, ensure_ascii=False)


_TOML_HEADER = re.compile(r"^\s*(\[\[?)(.*?)(\]\]?)\s*(?:#.*)?$")
_TOML_KEY = re.compile(r"^\s*([A-Za-z0-9_-]+|\"[^\"]*\"|'[^']*')\s*=")


def _toml_name_parts(raw):
    """Split a dotted TOML key or table name into its (unquoted) parts."""
    parts = []
    buffer = ""
    quote = None
    for char in raw:
        if quote is not None:
            if char == quote:
                quote = None
            buffer += char
        elif char in "\"'":
            quote = char
            buffer += char
        elif char == ".":
            parts.append(buffer)
            buffer = ""
        else:
            buffer += char
    parts.append(buffer)

    unquoted = []
    for part in parts:
        part = part.strip()
        if len(part) >= 2 and part[0] == part[-1] and part[0] in "\"'":
            if part[0] == '"':
                try:
                    part = json.loads(part)
                except json.JSONDecodeError:
                    part = part[1:-1]
            else:
                part = part[1:-1]
        unquoted.append(part)
    return unquoted


def _toml_header(line):
    """The dotted name a table header opens, or None for any other line."""
    match = _TOML_HEADER.match(line)
    if not match:
        return None
    opening, raw, closing = match.groups()
    if len(opening) != len(closing):
        return None
    return _toml_name_parts(raw)


def _toml_table_span(lines, path):
    """(header index, one past the table's last body line) for `path`, or None."""
    wanted = list(path)
    for index, line in enumerate(lines):
        if _toml_header(line) == wanted:
            end = len(lines)
            for probe in range(index + 1, len(lines)):
                if _toml_header(lines[probe]) is not None:
                    end = probe
                    break
            return index, end
    return None


def _toml_body_span(lines):
    """The root table: everything before the first table header."""
    for index, line in enumerate(lines):
        if _toml_header(line) is not None:
            return 0, index
    return 0, len(lines)


def _toml_find_key(lines, start, end, key):
    """The index of a `key = ...` line inside [start, end), or None."""
    for index in range(start, end):
        match = _TOML_KEY.match(lines[index])
        if match and _toml_name_parts(match.group(1)) == [key]:
            return index
    return None


def _toml_read_string(line):
    """The string value of a key line, for the basic and literal string forms."""
    _, _, value = line.partition("=")
    value = value.strip()
    if not value:
        return None
    if value[0] == '"':
        end = 1
        while end < len(value):
            if value[end] == "\\":
                end += 2
                continue
            if value[end] == '"':
                break
            end += 1
        try:
            return json.loads(value[: end + 1])
        except json.JSONDecodeError:
            return None
    if value[0] == "'":
        end = value.find("'", 1)
        return value[1:end] if end > 0 else None
    return None


def _toml_read_key(lines, path, key):
    """Read a string setting out of one table, without parsing the whole file."""
    span = _toml_table_span(lines, path) if path else _toml_body_span(lines)
    if span is None:
        return None
    index = _toml_find_key(lines, span[0] + (1 if path else 0), span[1], key)
    return _toml_read_string(lines[index]) if index is not None else None


def _toml_block(path, body):
    """The lines of a table: its header, its settings and a blank line after it."""
    lines = [f"[{'.'.join(path)}]"] if path else []
    return lines + list(body) + [""]


def upsert_toml_table(text, path, body, label="TOML config"):
    """Replace every line of table `path` with `body`, or append the table."""
    lines = text.splitlines()
    span = _toml_table_span(lines, path)
    if span is None:
        if lines and lines[-1].strip():
            lines.append("")
        lines.extend(_toml_block(path, body))
        log(f"{label}: added the table [{'.'.join(path)}] ({len(body)} settings)")
    else:
        start, end = span
        nested = [line.strip() for line in lines[start + 1 : end] if _toml_header(line)]
        lines[start:end] = _toml_block(path, body)
        log(f"{label}: rewrote the table [{'.'.join(path)}] ({len(body)} settings)")
        if nested:
            log(
                f"  note: tables that were nested in it are left as they are: "
                f"{short_list(nested)}",
                "WARN",
            )
    return "\n".join(lines).rstrip("\n") + "\n"


def upsert_toml_key(text, key, value, label="TOML config"):
    """Set a root-level `key` (it has to stay above the first table header)."""
    lines = text.splitlines()
    start, end = _toml_body_span(lines)
    line = f"{key} = {toml_string(value)}"
    index = _toml_find_key(lines, start, end, key)
    if index is None:
        # Keep a blank line between the root settings and the first table.
        lines.insert(end, line if end == len(lines) else line + "\n")
        log(f"{label}: added the root setting {key} = {value!r}")
    else:
        previous = _toml_read_string(lines[index])
        if previous == value:
            log(f"{label}: root setting {key} already points at {value!r}; left as written")
            return text
        lines[index] = line
        log(f"{label}: root setting {key}: {previous!r} -> {value!r}")
    return "\n".join(lines).rstrip("\n") + "\n"


def remove_toml_table(text, path, label="TOML config"):
    """Drop a whole table. Returns the new text and whether anything was removed."""
    lines = text.splitlines()
    span = _toml_table_span(lines, path)
    if span is None:
        log(f"{label}: no [{'.'.join(path)}] table to remove", "DEBUG")
        return text, False
    start, end = span
    del lines[start:end]
    log(f"{label}: removed the table [{'.'.join(path)}]")
    return "\n".join(lines).rstrip("\n") + "\n", True


def remove_toml_key(text, key, label="TOML config"):
    """Drop a root-level setting. Returns the new text and whether anything was removed."""
    lines = text.splitlines()
    start, end = _toml_body_span(lines)
    index = _toml_find_key(lines, start, end, key)
    if index is None:
        log(f"{label}: no {key} setting to remove", "DEBUG")
        return text, False
    previous = _toml_read_string(lines[index])
    del lines[index]
    log(f"{label}: removed the root setting {key} (was {previous!r})")
    return "\n".join(lines).rstrip("\n") + "\n", True


def codex_binary_candidates():
    """Every plausible codex executable on this machine, newest desktop copy first."""
    candidates = []
    for name in ("codex", "codex.cmd", "codex.exe"):
        found = shutil.which(name)
        if found and found not in candidates:
            candidates.append(found)

    bins = []
    for root in (
        os.path.join(os.environ.get("LOCALAPPDATA", ""), "OpenAI", "Codex", "bin"),
        os.path.join(CODEX_HOME, "bin"),
    ):
        if not root:
            continue
        try:
            entries = os.listdir(root)
        except OSError:
            continue
        for entry in entries:
            binary = os.path.join(root, entry, "codex.exe")
            if os.path.isfile(binary):
                bins.append(binary)
    for binary in sorted(bins, key=_safe_mtime, reverse=True):
        if binary not in candidates:
            candidates.append(binary)
    return candidates


def _safe_mtime(path):
    try:
        return os.path.getmtime(path)
    except OSError:
        return 0.0


def _run_codex(binary, args, timeout):
    """Run a codex command, returning its stdout or None (logged) when it fails.

    The output is decoded as UTF-8 explicitly: codex prints UTF-8, and letting
    subprocess guess would use the console code page on Windows (cp1252), which
    makes the reader thread fail on the first non-ASCII byte and lose all output.
    """
    log(f"running: {os.path.normpath(binary)} {' '.join(args)}")
    try:
        proc = subprocess.run(
            [binary] + list(args),
            capture_output=True,
            timeout=timeout,
            encoding="utf-8",
            errors="replace",
        )
    except (OSError, subprocess.SubprocessError) as exc:
        log(f"  could not run it: {exc!r}", "WARN")
        return None
    if proc.returncode != 0:
        log(
            f"  it exited with {proc.returncode}: "
            f"{short_list((proc.stderr or '').strip().splitlines(), 3)}",
            "WARN",
        )
        return None
    return proc.stdout


def codex_installed_models():
    """The catalog this Codex shows today, via `codex debug models`.

    That command prints the effective catalog (the models the app would offer);
    the --bundled pass is the fallback for installs that are not signed in. Both
    are best effort: no Codex binary on this machine is a normal outcome.
    """
    candidates = codex_binary_candidates()
    if not candidates:
        log("no codex executable found on PATH or in the desktop app's install directory", "WARN")
        return None, None
    for binary in candidates:
        for args in (("debug", "models"), ("debug", "models", "--bundled")):
            output = _run_codex(binary, args, CODEX_DEBUG_MODELS_TIMEOUT)
            if not output:
                continue
            try:
                data = json.loads(output)
            except json.JSONDecodeError as exc:
                log(f"  its output is not JSON: {exc}", "WARN")
                continue
            models = [
                model
                for model in data.get("models", [])
                if isinstance(model, dict) and model.get("slug")
            ]
            if models:
                log(f"  it lists {len(models)} models: {short_list([m['slug'] for m in models])}")
                return models, f"{os.path.normpath(binary)} {' '.join(args)}"
            log("  it listed no models", "WARN")
    return None, None


def codex_generated_entry(entry):
    """True for a CommandCode entry an earlier run of this script wrote."""
    return (
        entry.get("priority", 0) >= CODEX_PRIORITY_BASE
        and "served by CommandCode." in str(entry.get("description", ""))
    )


def codex_cache_models(label="Codex catalog"):
    """Codex's own remote catalog cache, and when it was fetched.

    The app refreshes ~/.codex/models_cache.json from the server with the user's
    account, so it is where a model released since the last run turns up: while
    model_catalog_json is in use, `codex debug models` can only report the file
    this script wrote, which is why the cache is read as well.
    """
    path = os.path.join(CODEX_HOME, "models_cache.json")
    data = load_config(path, "Codex models cache")
    if not data:
        return [], None
    models = [
        model
        for model in data.get("models", [])
        if isinstance(model, dict) and model.get("slug") and not codex_generated_entry(model)
    ]
    return models, data.get("fetched_at")


def codex_seed_models(catalog_path, label="Codex catalog"):
    """The models to keep alongside the CommandCode ones, and where they came from.

    model_catalog_json replaces Codex's built-in catalog, so whatever Codex
    offers today has to be copied into the file this script writes, or the model
    picker would only show CommandCode models. Sources, best first: the live
    catalog from `codex debug models`, the app's own models_cache.json, and the
    catalog an earlier run of this script wrote. Whatever the catalog file in use
    holds beyond the CommandCode entries this script generates is kept too, so a
    model added to it by hand is not lost on the next run.

    Every source is read through codex_generated_entry: the entries of an earlier
    run are dropped instead of kept, or a stale copy would win over the model
    list just fetched from the API.
    """
    models, source = codex_installed_models()
    if models:
        stale = [model for model in models if codex_generated_entry(model)]
        if stale:
            log(
                f"{label}: {len(stale)} of the {len(models)} models it listed are "
                f"CommandCode entries from an earlier run; they are refreshed below"
            )
        models = [model for model in models if not codex_generated_entry(model)]

    if not models:
        for path, origin in (
            (os.path.join(CODEX_HOME, "models_cache.json"), "Codex models cache"),
            (catalog_path, "the catalog written by an earlier run"),
        ):
            data = load_config(path, origin)
            if not data:
                continue
            models = [
                model
                for model in data.get("models", [])
                if isinstance(model, dict)
                and model.get("slug")
                and not codex_generated_entry(model)
            ]
            if models:
                log(f"{label}: taking {len(models)} models from {origin} ({os.path.normpath(path)})")
                source = origin
                break
        else:
            models, source = [], None

    # Codex's own cache is merged in as well, not only used as a fallback: it is
    # where a model released since the last run appears, because Codex's answer to
    # `codex debug models` is this very catalog file once model_catalog_json is set.
    known = {entry.get("slug") for entry in models}
    cached, fetched_at = codex_cache_models(label)
    released = [
        model for model in cached if model.get("slug") not in known
    ]
    if released:
        log(
            f"{label}: {len(released)} models from Codex's own cache are not in the "
            f"catalog yet and are added: {short_list([m['slug'] for m in released])}"
        )
        models = models + released
    elif cached:
        log(
            f"{label}: Codex's own cache (fetched {fetched_at or 'at an unknown time'}) "
            f"holds no model that is missing here",
            "DEBUG",
        )

    extra = []
    if catalog_path and os.path.exists(catalog_path):
        data = load_config(catalog_path, "the catalog in use")
        if data:
            extra = [
                model
                for model in data.get("models", [])
                if isinstance(model, dict)
                and model.get("slug")
                and not codex_generated_entry(model)
                and model.get("slug") not in {entry.get("slug") for entry in models}
            ]
            if extra:
                log(
                    f"{label}: keeping the {len(extra)} models this catalog holds besides "
                    f"Codex's own: {short_list([m['slug'] for m in extra])}"
                )
    return models + extra, source


def codex_reference_entry(seed, label="Codex catalog"):
    """The leanest real Codex model entry to model the CommandCode entries on.

    Cloning one keeps every field this Codex version expects (prompt, prompt
    sections, limits) even for a version the script has never seen. The smallest
    listed model is chosen because a clone carries its whole entry with it.
    """
    usable = [
        entry
        for entry in seed
        if entry.get("visibility") == "list"
        and (entry.get("base_instructions") or entry.get("model_messages"))
    ]
    if usable:
        entry = min(usable, key=lambda item: len(json.dumps(item)))
        log(
            f"{label}: copying the shape of {entry['slug']!r} into the new entries "
            f"({len(json.dumps(entry))} bytes each)"
        )
        return entry
    log(
        f"{label}: no usable Codex model entry to copy; building minimal entries "
        f"from the fields Codex requires",
        "WARN",
    )
    return None


# Codex ships its own GPT models, so the CommandCode copies of those ids would
# only duplicate what the picker already lists: a model whose id starts with one
# of these prefixes is left to Codex itself and never added by this script.
CODEX_NATIVE_MODEL_PREFIXES = ("gpt-",)


def codex_supports_responses(endpoints):
    """Whether Codex can drive a model (it speaks /responses only since 0.154)."""
    if not endpoints:
        return True
    return CODEX_RESPONSES_ENDPOINT in endpoints


def codex_is_native_model(model_id):
    """True for a model id Codex provides itself (its own GPT models)."""
    return model_id.lower().startswith(CODEX_NATIVE_MODEL_PREFIXES)


def codex_model_entries(models, endpoints, reference, label="Codex catalog"):
    """One Codex catalog entry per CommandCode model Codex can actually call."""
    entries = []
    unsupported = []
    native = []
    for index, (model_id, model) in enumerate(models.items()):
        if codex_is_native_model(model_id):
            native.append(model_id)
            continue
        if not codex_supports_responses(endpoints.get(model_id)):
            unsupported.append(model_id)
            continue
        entry = dict(reference) if reference else dict(CODEX_MINIMAL_MODEL)
        limit = model.get("limit") if isinstance(model.get("limit"), dict) else {}
        context = limit.get("context")
        entry.update(CODEX_ENTRY_OVERRIDES)
        entry["slug"] = model_id
        entry["display_name"] = model.get("name") or model_id
        entry["description"] = f"{model.get('name') or model_id} served by CommandCode."
        entry["priority"] = CODEX_PRIORITY_BASE + index
        prompt_sections = entry.get("model_messages")
        if isinstance(prompt_sections, dict) and prompt_sections.get("instructions_template"):
            # Codex builds the agent prompt from the template, so the identical
            # base_instructions copy in every entry is dropped: it is half the file.
            entry.pop("base_instructions", None)
        if context is not None:
            entry["context_window"] = context
            entry["max_context_window"] = context
        else:
            entry.pop("context_window", None)
            entry.pop("max_context_window", None)
        entries.append(entry)
    if unsupported:
        log(
            f"{label}: {len(unsupported)} of {len(models)} models do not offer "
            f"{CODEX_RESPONSES_ENDPOINT}, which is all Codex speaks, and were left out: "
            f"{short_list(unsupported)}",
            "WARN",
        )
    if native:
        log(
            f"{label}: {len(native)} models are Codex's own (their id starts with "
            f"{short_list(CODEX_NATIVE_MODEL_PREFIXES)}), so no CommandCode entry was "
            f"added for them: {short_list(native)}"
        )
    log(f"{label}: {len(entries)} of {len(models)} models can be used by Codex")
    return entries


def merge_codex_catalog(seed, entries, label="Codex catalog"):
    """The entries Codex already had, then the CommandCode ones (slug wins once)."""
    seed_slugs = {entry.get("slug") for entry in seed}
    merged = []
    seen = set()
    for entry in list(seed) + list(entries):
        slug = entry.get("slug")
        if slug in seen:
            if slug in seed_slugs:
                log(f"{label}: {slug!r} is already a Codex model; keeping Codex's own entry", "DEBUG")
            continue
        seen.add(slug)
        merged.append(entry)
    from_codex = sum(1 for entry in merged if entry.get("slug") in seed_slugs)
    log(
        f"{label}: {len(merged)} entries, {from_codex} of them Codex's own models and "
        f"{len(merged) - from_codex} CommandCode's"
    )
    return merged


def update_codex_catalog(path, models, endpoints, label="Codex model catalog", include_codex_models=True):
    """Write the merged catalog; returns the usable CommandCode entries and the file.

    With include_codex_models the file is a full replacement catalog (Codex's own
    models are copied in, for the setup that points config.toml at it); without it
    the file holds the CommandCode models only, which is what a profile-scoped
    catalog should list.
    """
    seed, source = codex_seed_models(path, label)
    if seed:
        log(f"{label}: keeping the {len(seed)} models Codex offers today (from {source})")
    elif include_codex_models:
        log(
            f"{label}: no Codex model list could be read, so the catalog written here "
            f"lists the CommandCode models only; Codex's own models stay hidden from the "
            f"picker until this script can read them (install the Codex CLI or start the "
            f"desktop app once, then re-run)",
            "WARN",
        )
    entries = codex_model_entries(models, endpoints, codex_reference_entry(seed), label)
    if include_codex_models:
        merged = merge_codex_catalog(seed, entries, label)
    else:
        merged = entries
        log(f"{label}: {len(merged)} models, this catalog is for the {CODEX_PROFILE_NAME} profile")
    write_text(
        path,
        json.dumps({"models": merged}, indent=2) + "\n",
        label,
        f"{len(merged)} models",
    )
    return entries, merged


def codex_provider_body(api_key=None):
    """The settings of [model_providers.commandcode]."""
    body = ["# CommandCode, kept up to date by update_models.py."]
    if api_key is None:
        body += [
            "# No API key is stored in this file yet: re-run the script with --api-key,",
            "# or export COMMANDCODE_API_KEY and add",
            "# env_key = \"COMMANDCODE_API_KEY\" below.",
        ]
    else:
        body += [
            "# The key is stored inline in experimental_bearer_token, because Codex",
            "# aborts when a configured env_key is not set. To keep the key out of this",
            "# file instead, delete that line, export COMMANDCODE_API_KEY and add",
            "# env_key = \"COMMANDCODE_API_KEY\" below.",
        ]
    body += [
        f"name = {toml_string(PROVIDER_NAME)}",
        f"base_url = {toml_string(CODEX_BASE_URL)}",
        f"wire_api = {toml_string(CODEX_WIRE_API)}",
    ]
    if api_key is not None:
        body.append(f"experimental_bearer_token = {toml_string(api_key)}")
    return body


def codex_catalog_setting(text, label="Codex config"):
    """The catalog file this config already uses, so an existing one is kept."""
    lines = text.splitlines()
    value = _toml_read_key(lines, None, CODEX_CATALOG_KEY)
    if value:
        log(f"{label}: {CODEX_CATALOG_KEY} already points at {value!r}; the models go there")
    return value


def remove_catalog_file(path, label="Codex model catalog"):
    """Delete a catalog file this script wrote; anything else is left and reported."""
    if not path or not os.path.exists(path):
        return False
    data = load_config(path, os.path.basename(path))
    models = data.get("models") if isinstance(data, dict) else None
    ours = isinstance(models, list) and any(
        codex_generated_entry(model) for model in models if isinstance(model, dict)
    )
    if not ours:
        log(
            f"{label}: {os.path.normpath(path)} is not a file this script wrote; it is "
            f"left where it is",
            "WARN",
        )
        return False
    try:
        os.remove(path)
    except OSError as exc:
        log(f"{label}: could not remove {os.path.normpath(path)}: {exc!r}", "WARN")
        return False
    log(f"{label}: removed {os.path.normpath(path)}")
    return True


def update_codex_config(text, catalog_path, api_key=None, label="Codex config"):
    """Refresh the provider table and the catalog pointer, keeping the rest of the file."""
    lines = text.splitlines()
    previous_key = _toml_read_key(lines, CODEX_PROVIDER_TABLE, "experimental_bearer_token")
    previous_base = _toml_read_key(lines, CODEX_PROVIDER_TABLE, "base_url")
    previous_wire = _toml_read_key(lines, CODEX_PROVIDER_TABLE, "wire_api")

    # A key already stored in the table is kept: the rewrite must not drop it when
    # this run has no key yet (the prompt comes after the model files are written).
    body_key = api_key if api_key is not None else previous_key
    text = upsert_toml_table(text, CODEX_PROVIDER_TABLE, codex_provider_body(body_key), label)
    if previous_base not in (None, CODEX_BASE_URL):
        log(f"{label}: base_url {previous_base!r} -> {CODEX_BASE_URL!r}")
    if previous_wire not in (None, CODEX_WIRE_API):
        log(f"{label}: wire_api {previous_wire!r} -> {CODEX_WIRE_API!r}")
    if api_key is not None:
        log(f"{label}: experimental_bearer_token {redact(previous_key)} -> {redact(api_key)}")
    elif previous_key:
        log(f"{label}: experimental_bearer_token {redact(previous_key)} kept in the provider table")
    else:
        log(
            f"{label}: experimental_bearer_token <not set> "
            f"(left alone; asked for after the models)"
        )
    return upsert_toml_key(text, CODEX_CATALOG_KEY, catalog_path, label)


def apply_codex_api_key(text, api_key, label="Codex config"):
    """Store the key in the provider table. The new text, or None when unchanged."""
    lines = text.splitlines()
    previous_key = _toml_read_key(lines, CODEX_PROVIDER_TABLE, "experimental_bearer_token")
    if previous_key == api_key:
        log(f"{label}: stored experimental_bearer_token is already {redact(api_key)}; left as written")
        return None
    log(f"{label}: experimental_bearer_token {redact(previous_key)} -> {redact(api_key)}")
    return upsert_toml_table(text, CODEX_PROVIDER_TABLE, codex_provider_body(api_key), label)


def update_codex_profile(text, models, known=None, label="Codex profile", catalog_path=None, api_key=None):
    """The `<name>.config.toml` Codex loads for `codex --profile commandcode`.

    `models` is the CommandCode catalog in picker order and supplies the default;
    `known` is every model the catalog holds, so a model chosen by hand (Codex's
    own ones included) survives a refresh. With `catalog_path` the profile also
    carries the provider and points at its own catalog, which is what keeps a
    CommandCode session from seeing Codex's models and vice versa.
    """
    known = set(models) if known is None else set(known)
    fresh = not text.strip()
    if fresh:
        text = (
            f"# CommandCode profile for Codex; update_models.py keeps this file up to date.\n"
            f"# Start Codex with it: codex --profile {CODEX_PROFILE_NAME}\n"
        )
    previous = _toml_read_key(text.splitlines(), None, "model")
    if previous in known:
        log(f"{label}: keeping model = {previous!r}")
        model_id = previous
    else:
        model_id = models[0]
        if previous:
            log(f"{label}: model {previous!r} is not in the Codex catalog anymore")
    text = upsert_toml_key(text, "model", model_id, label)
    text = upsert_toml_key(text, "model_provider", PROVIDER_ID, label)
    if catalog_path:
        previous_key = _toml_read_key(
            text.splitlines(), CODEX_PROVIDER_TABLE, "experimental_bearer_token"
        )
        body_key = api_key if api_key is not None else previous_key
        text = upsert_toml_table(text, CODEX_PROVIDER_TABLE, codex_provider_body(body_key), label)
        text = upsert_toml_key(text, CODEX_CATALOG_KEY, catalog_path, label)
        if api_key is None:
            log(
                f"{label}: experimental_bearer_token {redact(previous_key)} "
                f"(left alone; asked for after the models)"
            )
    return text


def clean_codex_config(label="Codex config"):
    """Take this script's provider table and catalog pointer out of config.toml.

    Returns True when the file was rewritten. A model_catalog_json pointing at a
    file this script did not write is left alone.
    """
    text = _read_text(CODEX_CONFIG_PATH, label)
    if text is None:
        log(f"{label}: does not exist, nothing to clean")
        return False
    changed = False
    catalog = _toml_read_key(text.splitlines(), None, CODEX_CATALOG_KEY)
    ours = {
        os.path.normcase(os.path.abspath(path))
        for path in (CODEX_CATALOG_PATH, CODEX_PROFILE_CATALOG_PATH)
    }
    if catalog and os.path.normcase(os.path.abspath(catalog)) not in ours:
        log(
            f"{label}: {CODEX_CATALOG_KEY} points at {catalog!r}, which this script did "
            f"not write; leaving it alone",
            "WARN",
        )
    elif catalog:
        text, removed = remove_toml_key(text, CODEX_CATALOG_KEY, label)
        changed = changed or removed
    text, removed = remove_toml_table(text, CODEX_PROVIDER_TABLE, label)
    changed = changed or removed
    if not changed:
        log(f"{label}: nothing this script wrote is left in it")
        return False
    write_text(CODEX_CONFIG_PATH, text, label, "CommandCode removed")
    return True


def revert_codex():
    """Undo everything this script added to Codex, leaving Codex its own model list.

    Codex manages its own catalog (the bundled one plus the list it fetches with
    the user's account), so removing model_catalog_json is all it takes to get the
    original models back; the provider table and the profile file this script
    wrote go with it. Nothing Codex itself owns (auth.json, models_cache.json, the
    rest of config.toml) is touched, and a re-run of the script restores it all.
    """
    log_step("Reverting the Codex changes")
    if clean_codex_config():
        print(f"Removed CommandCode ({PROVIDER_ID!r} provider and catalog) from {CODEX_CONFIG_PATH}")

    remove_catalog_file(CODEX_CATALOG_PATH)
    remove_catalog_file(CODEX_PROFILE_CATALOG_PATH)
    revert_codex_profile()

    log_step("Done")
    log(
        "Codex is back on its own model list; start it again to see it (the desktop app "
        "and the CLI read the catalog when they start)"
    )


def revert_codex_profile(label="Codex profile"):
    """Take this script's settings out of the profile file, deleting it when it is empty.

    The profile can hold the provider table and the catalog pointer as well as the
    model, so all of them go; a profile the user added settings to is kept with
    only this script's parts removed.
    """
    text = _read_text(CODEX_PROFILE_PATH, label)
    if text is None:
        return False
    our_catalogs = {
        os.path.normcase(os.path.abspath(path))
        for path in (CODEX_CATALOG_PATH, CODEX_PROFILE_CATALOG_PATH)
    }
    catalog = _toml_read_key(text.splitlines(), None, CODEX_CATALOG_KEY)
    if catalog and os.path.normcase(os.path.abspath(catalog)) in our_catalogs:
        text, _ = remove_toml_key(text, CODEX_CATALOG_KEY, label)
    text, _ = remove_toml_key(text, "model", label)
    text, _ = remove_toml_key(text, "model_provider", label)
    text, _ = remove_toml_table(text, CODEX_PROVIDER_TABLE, label)

    left = [
        line.strip()
        for line in text.splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]
    if left:
        write_text(
            CODEX_PROFILE_PATH, text, label,
            f"{len(left)} settings that are not this script's",
        )
        log(f"{label}: it holds {short_list(left)} of its own, so the file was kept")
        return True
    try:
        os.remove(CODEX_PROFILE_PATH)
    except OSError as exc:
        log(f"{label}: could not remove {os.path.normpath(CODEX_PROFILE_PATH)}: {exc!r}", "WARN")
        return False
    log(f"{label}: removed {os.path.normpath(CODEX_PROFILE_PATH)}")
    return True


def update_codex_setup(models, endpoints, config_text, profile_text, api_key=None, global_mode=False):
    """Write the Codex catalog plus its provider setup, in the mode that was asked for.

    Returns (entries, catalog, config_text, profile_text).
    """
    if global_mode:
        # CommandCode as an ordinary provider in config.toml, with one catalog that
        # holds Codex's own models too: this is the setup the desktop app's picker
        # reads, and every session keeps whatever provider config.toml selects.
        catalog_path = codex_catalog_setting(config_text) or CODEX_CATALOG_PATH
        if not os.path.exists(catalog_path):
            print(f"Creating new model catalog at {catalog_path}")
        entries, catalog = update_codex_catalog(catalog_path, models, endpoints)
        native = sum(1 for entry in catalog if entry.get("priority", 0) < CODEX_PRIORITY_BASE)
        print(
            f"Wrote {len(catalog)} models to {catalog_path} "
            f"({len(catalog) - native} of them CommandCode's, {native} Codex's own)"
        )
        if not config_text:
            print(f"Creating new config at {CODEX_CONFIG_PATH}")
        config_text = update_codex_config(config_text, catalog_path, api_key=api_key)
        write_text(
            CODEX_CONFIG_PATH, config_text, "Codex config",
            f"provider {PROVIDER_ID!r} and {CODEX_CATALOG_KEY}",
        )
        print(f"Wrote provider {PROVIDER_ID} to {CODEX_CONFIG_PATH}")
        if not profile_text:
            print(f"Creating new profile at {CODEX_PROFILE_PATH}")
        profile_text = update_codex_profile(
            profile_text,
            [entry["slug"] for entry in catalog if entry.get("priority", 0) >= CODEX_PRIORITY_BASE],
            known={entry["slug"] for entry in catalog},
        )
        write_text(
            CODEX_PROFILE_PATH, profile_text, "Codex profile",
            f"profile {CODEX_PROFILE_NAME!r}",
        )
        print(f"Wrote the {CODEX_PROFILE_NAME} profile to {CODEX_PROFILE_PATH}")
        return entries, catalog, config_text, profile_text

    # Everything CommandCode lives in the profile file: config.toml is left exactly
    # as Codex had it, so ordinary sessions keep Codex's own catalog and the profile
    # session gets a catalog that only lists models CommandCode serves. Codex 0.154
    # and later also read a plain profile file, so only this one file is needed.
    if clean_codex_config():
        print(f"Removed the old CommandCode setup from {CODEX_CONFIG_PATH}")
    catalog_path = CODEX_PROFILE_CATALOG_PATH
    if not os.path.exists(catalog_path):
        print(f"Creating new model catalog at {catalog_path}")
    entries, catalog = update_codex_catalog(
        catalog_path, models, endpoints, include_codex_models=False
    )
    print(f"Wrote {len(catalog)} CommandCode models to {catalog_path}")
    if not profile_text:
        print(f"Creating new profile at {CODEX_PROFILE_PATH}")
    profile_text = update_codex_profile(
        profile_text,
        [entry["slug"] for entry in catalog],
        known={entry["slug"] for entry in catalog},
        catalog_path=catalog_path,
        api_key=api_key,
    )
    write_text(
        CODEX_PROFILE_PATH, profile_text, "Codex profile",
        f"profile {CODEX_PROFILE_NAME!r} with the provider and {len(catalog)} models",
    )
    print(
        f"Wrote the {CODEX_PROFILE_NAME} profile (provider + {len(catalog)} models) to "
        f"{CODEX_PROFILE_PATH}; config.toml was left as it was"
    )
    return entries, catalog, config_text, profile_text


def apply_codex_api_key_everywhere(config_text, profile_text, global_mode, api_key):
    """Store the key in whichever file carries the provider table. Returns both texts."""
    target = config_text if global_mode else profile_text
    updated = apply_codex_api_key(target, api_key)
    if updated is None:
        return config_text, profile_text
    if global_mode:
        write_text(
            CODEX_CONFIG_PATH, updated, "Codex config",
            f"provider {PROVIDER_ID!r} with the API key",
        )
        print(f"Wrote API key to {CODEX_CONFIG_PATH}")
        return updated, profile_text
    write_text(
        CODEX_PROFILE_PATH, updated, "Codex profile",
        f"profile {CODEX_PROFILE_NAME!r} with the API key",
    )
    print(f"Wrote API key to {CODEX_PROFILE_PATH}")
    return config_text, updated


# ---------------------------------------------------------------------------
# Run
# ---------------------------------------------------------------------------


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description=(
            "Refresh the CommandCode model catalog in the OpenCode, ZCode and Codex "
            "configs, logging every step to stderr."
        ),
        epilog=(
            "examples:\n"
            "  python update_models.py --log-file update_models.log\n"
            "  python update_models.py --api-key $COMMANDCODE_API_KEY\n"
            "  python update_models.py --timeout 120\n"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--log-file",
        metavar="PATH",
        help="also write the full diagnostic log to PATH (replacing an existing file)",
    )
    parser.add_argument(
        "--api-key",
        metavar="KEY",
        help="use KEY instead of prompting for it",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=30.0,
        metavar="SECONDS",
        help="HTTP timeout for the model request (default: 30)",
    )
    parser.add_argument(
        "--revert-codex",
        action="store_true",
        help=(
            "undo everything this script added to Codex (the commandcode provider, the "
            "model catalog pointer, the catalog file and the profile) and exit, so Codex "
            "is back on its own model list"
        ),
    )
    parser.add_argument(
        "--codex-global",
        action="store_true",
        help=(
            "put the provider in config.toml instead of the profile file, so the Codex "
            "desktop app lists the CommandCode models too; every session then uses "
            "whichever provider config.toml selects (default: profile only)"
        ),
    )
    return parser.parse_args(argv)


def read_api_key(cli_value):
    """Get the key from --api-key or the prompt. An empty result means 'store nothing'."""
    if cli_value is not None:
        key = cli_value.strip()
        log(f"using the API key from --api-key: {redact(key)}")
    else:
        log(f"stdin is a terminal: {describe_stdin()}")
        if not describe_stdin():
            log(
                "stdin is not a terminal, so the prompt may fail; pass --api-key instead",
                "WARN",
            )
        try:
            key = input("Enter API key (blank leaves stored keys unchanged): ").strip()
        except EOFError:
            log("no API key could be read: stdin ended before a line arrived", "ERROR")
            log(
                "the model catalog was written already; re-run with --api-key <key> to "
                "store the key, or pipe the key in on stdin",
                "ERROR",
            )
            raise SystemExit(2)
        log(f"received an API key from the prompt: {redact(key)}")
    return key


def main(api_key=None, timeout=30.0, log_file=None, revert_codex_only=False, codex_global=False):
    global _LOG_HANDLE

    if log_file:
        log_path = os.path.abspath(log_file)
        try:
            directory = os.path.dirname(log_path)
            if directory:
                os.makedirs(directory, exist_ok=True)
            _LOG_HANDLE = open(log_path, "w", encoding="utf-8")
        except (OSError, ValueError) as exc:
            # Logging is auxiliary: a bad path must not abort the actual work.
            _LOG_HANDLE = None
            log(f"cannot write the log file {log_path!r}: {exc!r}", "WARN")
            log("continuing with stderr logging only", "WARN")
        else:
            log(f"also writing this log to {log_path}")

    if revert_codex_only:
        # No model fetch and no key prompt: this only takes the script's own
        # additions back out of Codex.
        log_step("Environment")
        log_environment()
        revert_codex()
        return

    log_step("Environment")
    log_environment()

    log_step("Target files")
    paths = target_paths()
    for label, path in paths:
        log(f"{label}: {os.path.normpath(path)} ({describe_path(path)})")
    log(f"API: {API_URL}")

    models, endpoints = fetch_models(timeout)
    print(f"Fetched {len(models)} models from {API_URL}")
    if not models:
        log(
            "the endpoint answered but yielded no usable models; refusing to overwrite "
            "the configs with an empty catalog",
            "ERROR",
        )
        raise RuntimeError("The model endpoint returned no usable models")

    log_step("Reading the existing configs")
    config = load_config(CONFIG_PATH, "OpenCode config")
    if config is None:
        config = {}
    auth = load_auth(AUTH_PATH)
    zcode_provider_config = load_config(ZCODE_PROVIDER_CONFIG_PATH, "ZCode provider config")
    if zcode_provider_config is None:
        zcode_provider_config = {}
    zcode_cli_config = load_config(ZCODE_CLI_CONFIG_PATH, "ZCode CLI config")
    if zcode_cli_config is None:
        zcode_cli_config = {}
    codex_config_text = _read_text(CODEX_CONFIG_PATH, "Codex config")
    if codex_config_text is None:
        codex_config_text = ""
    codex_profile_text = _read_text(CODEX_PROFILE_PATH, "Codex profile")
    if codex_profile_text is None:
        codex_profile_text = ""

    # A key from the command line is known already, so the model files can be
    # written complete in one pass; otherwise the key is stored in a later pass.
    known_key = api_key.strip() if isinstance(api_key, str) and api_key.strip() else None

    log_step("Updating the model catalog (before the API key is requested)")
    provider = ensure_dict(config, "provider", "OpenCode config")
    cmd = ensure_dict(provider, PROVIDER_ID, f"OpenCode config provider {PROVIDER_ID!r}")
    cmd.setdefault("name", PROVIDER_NAME)
    cmd.setdefault("npm", "@ai-sdk/openai-compatible")
    options = ensure_dict(cmd, "options", f"OpenCode config provider {PROVIDER_ID!r}.options")
    previous_base = options.get("baseURL")
    options["baseURL"] = BASE_URL
    previous_models = cmd.get("models")
    cmd["models"] = models
    log(
        f"OpenCode config: provider {PROVIDER_ID!r} name={cmd.get('name')!r} "
        f"npm={cmd.get('npm')!r} (the key lives in the auth file, written later)"
    )
    if previous_base != BASE_URL:
        log(f"  baseURL: {previous_base!r} -> {BASE_URL!r}")
    else:
        log(f"  baseURL unchanged: {BASE_URL!r}")
    log_model_changes(previous_models, models)

    if not os.path.exists(CONFIG_PATH):
        print(f"Creating new config at {CONFIG_PATH}")
    save_config(CONFIG_PATH, config, "OpenCode config")
    print(f"Wrote {len(models)} models to {CONFIG_PATH}")

    log_step("Updating the ZCode configs")
    if not os.path.exists(ZCODE_PROVIDER_CONFIG_PATH):
        print(f"Creating new config at {ZCODE_PROVIDER_CONFIG_PATH}")
    update_zcode_provider_config(
        zcode_provider_config, models, "ZCode provider config", api_key=known_key
    )
    save_config(ZCODE_PROVIDER_CONFIG_PATH, zcode_provider_config, "ZCode provider config")
    print(f"Wrote {len(models)} models to {ZCODE_PROVIDER_CONFIG_PATH}")

    if not os.path.exists(ZCODE_CLI_CONFIG_PATH):
        print(f"Creating new config at {ZCODE_CLI_CONFIG_PATH}")
    update_zcode_cli_config(zcode_cli_config, models, api_key=known_key)
    save_config(ZCODE_CLI_CONFIG_PATH, zcode_cli_config, "ZCode CLI config")
    print(f"Wrote {len(models)} models to {ZCODE_CLI_CONFIG_PATH}")

    log_step("Updating the Codex configs (CLI and desktop app)")
    codex_entries, codex_catalog, codex_config_text, codex_profile_text = update_codex_setup(
        models, endpoints, codex_config_text, codex_profile_text,
        api_key=known_key, global_mode=codex_global,
    )

    log_step("Asking for the API key")
    api_key = read_api_key(api_key)

    log_step("Storing the API key")
    if not api_key:
        log(
            "no API key was given, so none is being stored: the model catalog is updated "
            "and every stored key is left as it was (the app cannot authenticate until a "
            "key is stored, so re-run with --api-key if this is a fresh machine)",
            "WARN",
        )
    else:
        previous_entry = auth.get(PROVIDER_ID)
        if isinstance(previous_entry, dict):
            log(
                f"replacing the existing auth entry for {PROVIDER_ID!r}: "
                f"type={previous_entry.get('type')!r} "
                f"key={redact(previous_entry.get('key'))}"
            )
        else:
            log(f"no auth entry for {PROVIDER_ID!r} yet")
        auth[PROVIDER_ID] = {"type": "api", "key": api_key}
        save_auth(AUTH_PATH, auth)
        print(f"Wrote API key for {PROVIDER_ID} to {AUTH_PATH}")

        if apply_zcode_provider_api_key(zcode_provider_config, api_key):
            save_config(ZCODE_PROVIDER_CONFIG_PATH, zcode_provider_config, "ZCode provider config")
            print(f"Wrote API key to {ZCODE_PROVIDER_CONFIG_PATH}")
        if apply_zcode_cli_api_key(zcode_cli_config, api_key):
            save_config(ZCODE_CLI_CONFIG_PATH, zcode_cli_config, "ZCode CLI config")
            print(f"Wrote API key to {ZCODE_CLI_CONFIG_PATH}")
        codex_config_text, codex_profile_text = apply_codex_api_key_everywhere(
            codex_config_text, codex_profile_text, codex_global, api_key
        )

    log_step("Done")
    log(f"models fetched: {len(models)}, usable by Codex: {len(codex_entries)}")
    for label, path in paths:
        log(f"  {label}: {os.path.normpath(path)} ({describe_path(path)})")
    log("every written file was read back; see the 'verified' lines above")
    if codex_global:
        log(
            f"Codex: provider {PROVIDER_ID!r} is in config.toml, and its catalog lists "
            f"Codex's own models next to the CommandCode ones. Codex picks a provider per "
            f"session, not per model, so a model only reaches CommandCode while "
            f"model_provider is {PROVIDER_ID!r} ('codex --profile {CODEX_PROFILE_NAME}' "
            f"switches a CLI session over)."
        )
    else:
        log(
            f"Codex: config.toml was left untouched, so ordinary sessions stay on Codex's "
            f"own model list. Run 'codex --profile {CODEX_PROFILE_NAME}' to start a "
            f"CommandCode session: it loads {os.path.normpath(CODEX_PROFILE_PATH)}, whose "
            f"own catalog lists the {len(codex_catalog)} models CommandCode serves, so "
            f"every model it offers is routed to CommandCode."
        )


def failure_hint(exc):
    """A short 'what to look at next' line for the failed run."""
    if isinstance(exc, urllib.error.HTTPError):
        return (
            "hint: the server answered with an error status; 401/403 usually means the "
            "API key is missing or invalid, 404 means the endpoint URL changed"
        )
    if isinstance(exc, urllib.error.URLError):
        return (
            "hint: the request never completed; check this machine's network, VPN, proxy "
            "and firewall (the DNS and TLS lines above show how far it got)"
        )
    if isinstance(exc, json.JSONDecodeError):
        return (
            "hint: one of the files above is not valid JSON; fix or move it and re-run "
            "(it was not modified)"
        )
    if isinstance(exc, PermissionError):
        return (
            "hint: a file could not be read or written; close the program that owns it, "
            "check permissions, and re-run"
        )
    if isinstance(exc, FileNotFoundError):
        return "hint: one of the paths above does not exist on this machine"
    return None


def run(argv=None):
    """Command-line entry point: parse the options, then run main()."""
    args = parse_args(argv)
    try:
        main(
            api_key=args.api_key,
            timeout=args.timeout,
            log_file=args.log_file,
            revert_codex_only=args.revert_codex,
            codex_global=args.codex_global,
        )
    except SystemExit:
        raise
    except KeyboardInterrupt:
        log("interrupted (Ctrl+C); no further files were written", "ERROR")
        raise SystemExit(130)
    except Exception as exc:
        log_step("Run failed")
        log(f"{type(exc).__name__}: {exc}", "ERROR")
        log_block(traceback.format_exc(), "ERROR", limit=20)
        hint = failure_hint(exc)
        if hint:
            log(hint, "ERROR")
        log(
            "the first ERROR line above names the cause; the lines before it show the "
            "step that failed",
            "ERROR",
        )
        raise SystemExit(1)


if __name__ == "__main__":
    run()
