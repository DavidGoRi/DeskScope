"""Tickets from the rest of the toolkit: PCScope, NetScope, ADFlow and HardenScope.

Each tool's output is turned into *findings* with a stable key, and
:func:`sync` reconciles them with the open tickets:

* a new key opens a ticket (as the ``integrations`` system account);
* a key that already has an active ticket is not duplicated; if the details
  changed (a disk went from 91 % to 97 %) an internal note says so;
* a problem the tool no longer reports is noted and resolved automatically,
  except for findings that need a person to look at them (a new device on
  the network, an onboarding).

Every import is idempotent: running it twice changes nothing the second time.
"""

from __future__ import annotations

import csv
import io
import json
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path

from . import tickets, users
from .db import utcnow
from .models import DEVICE_MAX, TITLE_MAX

ACTIVE = ("new", "open", "pending")


@dataclass(frozen=True)
class Finding:
    key: str
    title: str
    description: str
    detail: str  # short text compared between imports to notice changes
    category: str
    impact: str
    urgency: str
    device: str | None = None
    auto_resolve: bool = True


@dataclass
class ImportResult:
    source: str
    created: list[int] = field(default_factory=list)
    updated: list[int] = field(default_factory=list)
    unchanged: list[int] = field(default_factory=list)
    resolved: list[int] = field(default_factory=list)
    devices: int = 0
    skipped: int = 0

    def as_dict(self) -> dict:
        return {
            "source": self.source,
            "created": self.created,
            "updated": self.updated,
            "unchanged": self.unchanged,
            "resolved": self.resolved,
            "devices": self.devices,
            "skipped": self.skipped,
        }

    def summary(self) -> str:
        parts = [
            f"{len(self.created)} opened",
            f"{len(self.updated)} updated",
            f"{len(self.unchanged)} unchanged",
            f"{len(self.resolved)} resolved",
        ]
        if self.devices:
            parts.append(f"{self.devices} device sheets")
        if self.skipped:
            parts.append(f"{self.skipped} skipped")
        return f"{self.source}: " + ", ".join(parts)


class ImportFormatError(ValueError):
    """The file is not what the importer expected."""


# ---- texts (tickets are written in the desk's language) ------------------------

TEXTS = {
    "en": {
        "auto_note": "Opened automatically by {tool}. It is resolved automatically when a later "
        "import no longer reports the problem.",
        "manual_note": "Opened automatically by {tool}.",
        "now_reports": "{tool} now reports: {detail}",
        "cleared": "{tool} no longer reports this problem (import of {date}). Resolved "
        "automatically; reopen the ticket if it is still happening.",
        "pcscope_title": "{device}: {detail}",
        "pcscope_body": "PCScope found this problem on {device}:\n\n{detail}\n\nRule: {rule} "
        "(level: {level}).",
        "netscope_title": "{name}: {detail}",
        "netscope_body": "NetScope found this on the network:\n\n{detail}\n\nDevice: {name}\n"
        "IP address: {ip}\nMAC address: {mac}\nVendor: {vendor}\nType: {type}\nOpen ports: {ports}",
        "onboarding_title": "Onboarding: {name} ({account})",
        "onboarding_body": "ADFlow created the account of {name}.\n\nAccount: {account}\n"
        "Sign-in name: {upn}\nEmail: {mail}\nGroups: {groups}\nHome folder: {home}\n\n"
        "Checklist:\n[ ] Prepare and register a computer\n[ ] Install the standard software\n"
        "[ ] Hand over the initial password in person\n[ ] Check the welcome email arrived",
        "offboarding_title": "Offboarding: {name} ({account})",
        "offboarding_body": "ADFlow disabled the account of {name}.\n\nAccount: {account}\n"
        "Previous OU: {ou}\nGroups removed: {groups}\nData archived to: {archive}\n\n"
        "Checklist:\n[ ] Collect the laptop, phone and badge\n[ ] Forward or archive the mailbox\n"
        "[ ] Remove licences and shared mailbox access\n[ ] Confirm with the manager",
        "hardenscope_title": "{device}: failed check “{rule}”",
        "hardenscope_body": "HardenScope found a failed security check on {device}:\n\n{rule} "
        "({id}, severity: {severity})\n\nWhy it matters: {risk}\n\nHow to fix it: {fix}"
        "{command}\n\nHardening score of {device}: {score} ({grade}).",
        "hardenscope_command": "\n\nPowerShell (review it before running it as administrator):\n"
        "{command}",
        "none": "—",
    },
    "es": {
        "auto_note": "Abierto automáticamente por {tool}. Se resuelve solo cuando una importación "
        "posterior deja de informar del problema.",
        "manual_note": "Abierto automáticamente por {tool}.",
        "now_reports": "{tool} informa ahora: {detail}",
        "cleared": "{tool} ya no informa de este problema (importación del {date}). Resuelto "
        "automáticamente; reabre el ticket si sigue pasando.",
        "pcscope_title": "{device}: {detail}",
        "pcscope_body": "PCScope ha encontrado este problema en {device}:\n\n{detail}\n\n"
        "Regla: {rule} (nivel: {level}).",
        "netscope_title": "{name}: {detail}",
        "netscope_body": "NetScope ha encontrado esto en la red:\n\n{detail}\n\nDispositivo: {name}\n"
        "Dirección IP: {ip}\nDirección MAC: {mac}\nFabricante: {vendor}\nTipo: {type}\n"
        "Puertos abiertos: {ports}",
        "onboarding_title": "Alta: {name} ({account})",
        "onboarding_body": "ADFlow ha creado la cuenta de {name}.\n\nCuenta: {account}\n"
        "Inicio de sesión: {upn}\nCorreo: {mail}\nGrupos: {groups}\nCarpeta personal: {home}\n\n"
        "Lista de tareas:\n[ ] Preparar y registrar un equipo\n[ ] Instalar el software estándar\n"
        "[ ] Entregar la contraseña inicial en persona\n[ ] Comprobar que llegó el correo de bienvenida",
        "offboarding_title": "Baja: {name} ({account})",
        "offboarding_body": "ADFlow ha desactivado la cuenta de {name}.\n\nCuenta: {account}\n"
        "OU anterior: {ou}\nGrupos retirados: {groups}\nDatos archivados en: {archive}\n\n"
        "Lista de tareas:\n[ ] Recoger el portátil, el móvil y la tarjeta\n"
        "[ ] Redirigir o archivar el buzón\n[ ] Retirar licencias y buzones compartidos\n"
        "[ ] Confirmarlo con su responsable",
        "hardenscope_title": "{device}: falla «{rule}»",
        "hardenscope_body": "HardenScope ha encontrado una comprobación de seguridad fallida en "
        "{device}:\n\n{rule} ({id}, gravedad: {severity})\n\nPor qué importa: {risk}\n\n"
        "Cómo arreglarlo: {fix}{command}\n\nNota de bastionado de {device}: {score} ({grade}).",
        "hardenscope_command": "\n\nPowerShell (revísalo antes de ejecutarlo como administrador):\n"
        "{command}",
        "none": "—",
    },
}


def _text(lang: str, key: str, **params: object) -> str:
    table = TEXTS.get(lang, TEXTS["en"])
    return table[key].format(**params)


def _title(text: str) -> str:
    text = " ".join(text.split())
    return text if len(text) <= TITLE_MAX else text[: TITLE_MAX - 1].rstrip() + "…"


def _device_name(*candidates: str | None) -> str | None:
    """First candidate usable as a ticket device name (no spaces, not too long)."""
    for value in candidates:
        value = (value or "").strip()
        if value and len(value) <= DEVICE_MAX and not any(c.isspace() for c in value):
            return value.upper()
    return None


def _as_list(value) -> list:
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


# ---- reconciliation -------------------------------------------------------------


def sync(
    conn,
    source: str,
    tool: str,
    findings: Iterable[Finding],
    scope_prefixes: Iterable[str] | None = None,
    lang: str = "en",
    result: ImportResult | None = None,
) -> ImportResult:
    """Open, update or resolve tickets so they match ``findings``.

    ``scope_prefixes`` lists the key prefixes this import covers completely
    (e.g. one per device that was scanned): active tickets under those
    prefixes whose finding disappeared are resolved. Without it nothing is
    resolved, which is right for partial or event-like sources.
    """
    bot = users.system_user(conn)
    result = result or ImportResult(source)
    seen: set[str] = set()

    for finding in findings:
        if finding.key in seen:
            continue
        seen.add(finding.key)
        row = conn.execute(
            "SELECT id, description FROM tickets WHERE external_key = ?"
            " AND status IN ('new', 'open', 'pending')",
            (finding.key,),
        ).fetchone()
        if row is None:
            note = "auto_note" if finding.auto_resolve else "manual_note"
            description = f"{finding.description}\n\n{_text(lang, note, tool=tool)}"
            ticket_id = tickets.create_ticket(
                conn, bot, finding.title, description[:5000], finding.category,
                finding.impact, finding.urgency, finding.device,
                source=source, external_key=finding.key,
            )
            result.created.append(ticket_id)
            continue
        ticket_id = row["id"]
        last = conn.execute(
            "SELECT body FROM comments WHERE ticket_id = ? AND author_id = ?"
            " ORDER BY created_at DESC, id DESC LIMIT 1",
            (ticket_id, bot.id),
        ).fetchone()
        reference = last["body"] if last else row["description"]
        if finding.detail in reference:
            result.unchanged.append(ticket_id)
        else:
            tickets.add_comment(
                conn, bot, ticket_id,
                _text(lang, "now_reports", tool=tool, detail=finding.detail)[:5000],
                internal=True,
            )
            result.updated.append(ticket_id)

    if scope_prefixes is not None:
        for prefix in sorted(set(scope_prefixes)):
            like = prefix.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
            rows = conn.execute(
                "SELECT id, external_key FROM tickets WHERE source = ? AND status IN"
                " ('new', 'open', 'pending') AND external_key LIKE ? ESCAPE '\\'",
                (source, like),
            ).fetchall()
            for row in rows:
                if row["external_key"] in seen or not _auto_resolvable(row["external_key"]):
                    continue
                tickets.add_comment(
                    conn, bot, row["id"],
                    _text(lang, "cleared", tool=tool, date=utcnow()[:10]), internal=True,
                )
                tickets.change_status(conn, bot, row["id"], "resolved")
                result.resolved.append(row["id"])
    return result


# Findings that a person must close: they describe an event, not a condition
# that can "go away" (a new device stays new until someone checks it).
_MANUAL_SUFFIXES = (":new_device",)


def _auto_resolvable(key: str) -> bool:
    return not key.startswith("adflow:") and not key.endswith(_MANUAL_SUFFIXES)


# ---- PCScope ----------------------------------------------------------------------

# rule -> (category, impact). Security rules affect more than one person.
PCSCOPE_RULES: dict[str, tuple[str, str]] = {
    "diskSpace": ("hardware", "low"),
    "diskHealth": ("hardware", "low"),
    "batteryHealth": ("hardware", "low"),
    "memory": ("hardware", "low"),
    "antivirus": ("software", "medium"),
    "antivirusDefinitions": ("software", "medium"),
    "uac": ("software", "medium"),
    "windowsUpdates": ("software", "medium"),
    "unsupportedWindows": ("software", "medium"),
    "laptopEncryption": ("software", "medium"),
    "blockedSoftware": ("software", "medium"),
    "pendingReboot": ("software", "low"),
    "uptime": ("software", "low"),
    "firewall": ("network", "medium"),
    "localAdministrators": ("access", "medium"),
    "outdatedSoftware": ("software", "medium"),
    "staleInventory": ("other", "low"),
    "collectionErrors": ("other", "low"),
}
PCSCOPE_URGENCY = {"critical": "high", "warning": "medium", "info": "low"}
PCSCOPE_LEVEL_ORDER = {"critical": 0, "warning": 1, "info": 2}

# Columns of PCScope's devices.csv kept on the device sheet.
DEVICE_FIELDS = (
    "Status", "Critical", "Warnings", "Manufacturer", "Model", "SerialNumber", "Type", "CPU",
    "RamGB", "Windows", "Version", "Build", "LastUpdate", "SystemDriveUsedPercent",
    "SystemDriveFreeGB", "BitLocker", "Antivirus", "User", "IPv4", "CollectedAt",
)


def read_csv(text: str) -> list[dict]:
    """PowerShell CSV: UTF-8 with or without BOM, ',' or ';' depending on the culture."""
    text = text.lstrip("﻿")
    if not text.strip():
        return []
    header = text.splitlines()[0]
    delimiter = ";" if header.count(";") > header.count(",") else ","
    return [dict(row) for row in csv.DictReader(io.StringIO(text), delimiter=delimiter)]


def load_pcscope_export(folder: str | Path) -> tuple[list[dict], list[dict]]:
    """devices.csv and issues.csv from ``PCScope.ps1 -ExportCsv``."""
    folder = Path(folder)
    devices_csv, issues_csv = folder / "devices.csv", folder / "issues.csv"
    if not devices_csv.exists():
        raise ImportFormatError(f"{devices_csv} not found: export with PCScope.ps1 -ExportCsv")
    devices = read_csv(devices_csv.read_text("utf-8-sig"))
    issues = read_csv(issues_csv.read_text("utf-8-sig")) if issues_csv.exists() else []
    if devices and "Device" not in devices[0]:
        raise ImportFormatError(f"{devices_csv} has no Device column")
    return devices, issues


def save_device(conn, name: str, source: str, data: dict, collected_at: str | None) -> None:
    conn.execute(
        "INSERT INTO devices (name, source, data, collected_at, imported_at)"
        " VALUES (?, ?, ?, ?, ?) ON CONFLICT (name) DO UPDATE SET source = excluded.source,"
        " data = excluded.data, collected_at = excluded.collected_at,"
        " imported_at = excluded.imported_at",
        (name, source, json.dumps(data, ensure_ascii=False), collected_at, utcnow()),
    )


def get_device(conn, name: str | None) -> dict | None:
    if not name:
        return None
    row = conn.execute("SELECT * FROM devices WHERE name = ?", (name.upper(),)).fetchone()
    if row is None:
        return None
    device = dict(row)
    device["data"] = json.loads(row["data"])
    return device


def import_pcscope(
    conn,
    devices: list[dict],
    issues: list[dict],
    levels: Iterable[str] = ("critical",),
    lang: str = "en",
) -> ImportResult:
    result = ImportResult("pcscope")
    levels = set(levels)
    names = []
    for row in devices:
        name = _device_name(row.get("Device"))
        if not name:
            result.skipped += 1
            continue
        names.append(name)
        data = {k: row.get(k) for k in DEVICE_FIELDS if row.get(k) not in (None, "")}
        save_device(conn, name, "pcscope", data, row.get("CollectedAt") or None)
        result.devices += 1
    conn.commit()

    # One ticket per device and rule; several drives under one rule share it.
    grouped: dict[tuple[str, str], list[dict]] = {}
    for row in issues:
        name = _device_name(row.get("Device"))
        level = (row.get("Level") or "").lower()
        if level not in levels:
            continue  # below the threshold chosen for tickets
        if not name or not row.get("Rule"):
            result.skipped += 1
            continue
        grouped.setdefault((name, row["Rule"]), []).append({**row, "Level": level})

    findings = []
    for (name, rule), rows in sorted(grouped.items()):
        level = min((r["Level"] for r in rows), key=lambda lv: PCSCOPE_LEVEL_ORDER.get(lv, 9))
        detail = " / ".join(sorted({r.get("Issue", "").strip() for r in rows}))
        category, impact = PCSCOPE_RULES.get(rule, ("other", "low"))
        findings.append(Finding(
            key=f"pcscope:{name}:{rule}",
            title=_title(_text(lang, "pcscope_title", device=name, detail=detail)),
            description=_text(lang, "pcscope_body", device=name, detail=detail, rule=rule,
                              level=level),
            detail=detail,
            category=category,
            impact=impact,
            urgency=PCSCOPE_URGENCY.get(level, "medium"),
            device=name,
        ))
    return sync(conn, "pcscope", "PCScope", findings,
                scope_prefixes=[f"pcscope:{n}:" for n in names], lang=lang, result=result)


# ---- NetScope ---------------------------------------------------------------------

NETSCOPE_SEVERITIES = ("crit",)
NETSCOPE_IMPACT = {"crit": "medium", "warn": "low", "info": "low"}
NETSCOPE_URGENCY = {"crit": "high", "warn": "medium", "info": "low"}


def load_netscope_scan(path: str | Path) -> dict:
    """A scan-*.json file, or the newest one in a NetScope output folder."""
    path = Path(path)
    if path.is_dir():
        candidates = sorted([*path.glob("scan-*.json"), *path.glob("scans/scan-*.json")],
                            key=lambda p: p.name)
        if not candidates:
            raise ImportFormatError(f"no scan-*.json in {path}")
        path = candidates[-1]
    try:
        scan = json.loads(path.read_text("utf-8-sig"))
    except json.JSONDecodeError as exc:
        raise ImportFormatError(f"{path}: invalid JSON ({exc})") from exc
    if scan.get("tool") != "NetScope" or not isinstance(scan.get("devices"), list):
        raise ImportFormatError(f"{path} is not a NetScope scan")
    return scan


def import_netscope(
    conn,
    scan: dict,
    severities: Iterable[str] = NETSCOPE_SEVERITIES,
    include_new_devices: bool = True,
    lang: str = "en",
) -> ImportResult:
    result = ImportResult("netscope")
    severities = set(severities)
    none = _text(lang, "none")
    findings, prefixes = [], []
    for device in scan.get("devices", []):
        ip = device.get("ip")
        if not ip:
            result.skipped += 1
            continue
        device_key = device.get("mac") or f"ip:{ip}"
        prefixes.append(f"netscope:{device_key}:")
        name = device.get("label") or device.get("hostname") or ip
        for item in device.get("findings") or []:
            code, severity = item.get("code"), item.get("severity")
            wanted = (include_new_devices and code == "new_device") or (
                code != "new_device" and severity in severities
            )
            if not wanted or not code:
                continue
            detail = item.get("message") or code
            is_new = code == "new_device"
            findings.append(Finding(
                key=f"netscope:{device_key}:{code}",
                title=_title(_text(lang, "netscope_title", name=name, detail=detail)),
                description=_text(
                    lang, "netscope_body", detail=detail, name=name, ip=ip,
                    mac=device.get("mac") or none, vendor=device.get("vendor") or none,
                    type=device.get("type") or none,
                    ports=", ".join(str(p) for p in device.get("open_ports") or []) or none,
                ),
                detail=detail,
                category="network",
                impact="medium" if is_new else NETSCOPE_IMPACT.get(severity, "low"),
                urgency="medium" if is_new else NETSCOPE_URGENCY.get(severity, "medium"),
                device=_device_name(device.get("hostname"), ip),
                auto_resolve=not is_new,
            ))
    return sync(conn, "netscope", "NetScope", findings, scope_prefixes=prefixes, lang=lang,
                result=result)


# ---- ADFlow -----------------------------------------------------------------------


def load_adflow_run(path: str | Path) -> dict:
    """summary.json of an ADFlow run (the file or its run folder)."""
    path = Path(path)
    if path.is_dir():
        path = path / "summary.json"
    if not path.exists():
        raise ImportFormatError(f"{path} not found")
    try:
        summary = json.loads(path.read_text("utf-8-sig"))
    except json.JSONDecodeError as exc:
        raise ImportFormatError(f"{path}: invalid JSON ({exc})") from exc
    if summary.get("operation") not in ("Onboarding", "Offboarding"):
        raise ImportFormatError(f"{path} is not an ADFlow run summary")
    return summary


def import_adflow(conn, summary: dict, lang: str = "en", include_dry_runs: bool = False
                  ) -> ImportResult:
    """One onboarding or offboarding ticket per person the run changed."""
    result = ImportResult("adflow")
    operation = summary.get("operation")
    live = summary.get("mode") == "Live"
    wanted = {"Onboarding": {"Created"}, "Offboarding": {"Offboarded"}}.get(operation, set())
    if include_dry_runs:
        wanted |= {"Planned"}
    elif not live:
        result.skipped = len(_as_list(summary.get("results")))
        return result

    none = _text(lang, "none")
    findings = []
    for row in _as_list(summary.get("results")):
        if row.get("Status") not in wanted:
            result.skipped += 1
            continue
        account = row.get("SamAccountName") or row.get("EmployeeId") or none
        person = row.get("EmployeeId") or row.get("SamAccountName")
        name = row.get("DisplayName") or account
        groups = ", ".join(str(g) for g in _as_list(row.get("Groups"))) or none
        if operation == "Onboarding":
            title = _text(lang, "onboarding_title", name=name, account=account)
            body = _text(lang, "onboarding_body", name=name, account=account,
                         upn=row.get("UserPrincipalName") or none, mail=row.get("Mail") or none,
                         groups=groups, home=row.get("HomeFolder") or none)
            impact, urgency = "medium", "medium"
        else:
            title = _text(lang, "offboarding_title", name=name, account=account)
            body = _text(lang, "offboarding_body", name=name, account=account,
                         ou=row.get("PreviousOu") or none, groups=groups,
                         archive=row.get("ArchivedTo") or none)
            impact, urgency = "medium", "high"
        findings.append(Finding(
            key=f"adflow:{operation.lower()}:{person}",
            # An onboarding is an event: re-importing the same run must not add notes.
            title=_title(title), description=body, detail=name, category="access",
            impact=impact, urgency=urgency, auto_resolve=False,
        ))
    return sync(conn, "adflow", "ADFlow", findings, lang=lang, result=result)


IMPORTERS: dict[str, Callable] = {
    "pcscope": import_pcscope,
    "netscope": import_netscope,
    "adflow": import_adflow,
}


# ---- HardenScope ------------------------------------------------------------------

# HardenScope severity -> (impact, urgency): a failed check exposes one computer,
# but a critical one (firewall off, SMBv1, automatic logon) must be fixed fast.
HARDENSCOPE_PRIORITY = {
    "critical": ("medium", "high"),
    "high": ("medium", "medium"),
    "medium": ("low", "medium"),
    "low": ("low", "low"),
}
HARDENSCOPE_CATEGORY = {"Network": "network", "Accounts": "access", "Credentials": "access"}
HARDENSCOPE_SEVERITIES = ("critical",)
HARDENSCOPE_SEVERITY_NAMES = {
    "en": {"critical": "critical", "high": "high", "medium": "medium", "low": "low"},
    "es": {"critical": "crítica", "high": "alta", "medium": "media", "low": "baja"},
}


def _localized(value, lang: str) -> str:
    """HardenScope texts are {"en": ..., "es": ...}."""
    if isinstance(value, dict):
        return str(value.get(lang) or value.get("en") or "")
    return str(value or "")


def _is_hardenscope_fleet(data) -> bool:
    return (isinstance(data, dict) and data.get("tool") == "HardenScope"
            and data.get("kind") == "fleet" and isinstance(data.get("computers"), list)
            and isinstance(data.get("failing"), list))


def load_hardenscope_fleet(path: str | Path) -> dict:
    """A fleet-*.json written by HardenScope, or the newest one in its output folder."""
    path = Path(path)
    if path.is_dir():
        candidates = sorted(path.glob("fleet-*.json"), key=lambda p: p.name)
        if not candidates:
            raise ImportFormatError(f"no fleet-*.json in {path}")
        path = candidates[-1]
    try:
        fleet = json.loads(path.read_text("utf-8-sig"))
    except json.JSONDecodeError as exc:
        raise ImportFormatError(f"{path}: invalid JSON ({exc})") from exc
    if not _is_hardenscope_fleet(fleet):
        raise ImportFormatError(f"{path} is not a HardenScope fleet report")
    return fleet


def import_hardenscope(
    conn,
    fleet: dict,
    severities: Iterable[str] = HARDENSCOPE_SEVERITIES,
    lang: str = "en",
) -> ImportResult:
    """One ticket per computer and failed check of the chosen severities.

    Every computer in the report was audited completely, so a ticket of one of
    them whose check no longer fails is resolved. Computers that could not be
    reached are not in ``computers`` and keep their tickets.
    """
    result = ImportResult("hardenscope")
    severities = set(severities)
    computers: dict[str, dict] = {}
    for computer in fleet.get("computers", []):
        device = _device_name(computer.get("name"))
        if device is None:
            result.skipped += 1
            continue
        computers[device] = computer

    findings = []
    for rule in fleet.get("failing", []):
        rule_id, severity = rule.get("id"), rule.get("severity")
        if not rule_id or severity not in severities:
            continue
        impact, urgency = HARDENSCOPE_PRIORITY.get(severity, ("low", "medium"))
        title = _localized(rule.get("title"), lang) or rule_id
        command = rule.get("command")
        for name in _as_list(rule.get("failedOn")):
            device = _device_name(name)
            if device not in computers:
                continue
            computer = computers[device]
            findings.append(Finding(
                key=f"hardenscope:{device}:{rule_id}",
                title=_title(_text(lang, "hardenscope_title", device=device, rule=title)),
                description=_text(
                    lang, "hardenscope_body", device=device, rule=title, id=rule_id,
                    severity=HARDENSCOPE_SEVERITY_NAMES.get(lang, HARDENSCOPE_SEVERITY_NAMES["en"])
                    .get(severity, severity),
                    risk=_localized(rule.get("risk"), lang),
                    fix=_localized(rule.get("fix"), lang),
                    command=_text(lang, "hardenscope_command", command=command) if command else "",
                    score=computer.get("score", _text(lang, "none")),
                    grade=computer.get("grade") or _text(lang, "none"),
                ),
                # Part of the description and stable while the check keeps
                # failing, so a repeated audit adds no notes
                detail=f"{title} ({rule_id}",
                category=HARDENSCOPE_CATEGORY.get(rule.get("category"), "software"),
                impact=impact,
                urgency=urgency,
                device=device,
            ))
    prefixes = [f"hardenscope:{device}:" for device in computers]
    return sync(conn, "hardenscope", "HardenScope", findings, scope_prefixes=prefixes, lang=lang,
                result=result)

