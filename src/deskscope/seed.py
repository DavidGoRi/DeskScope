"""Fictitious demo data: a small company (example.com) and its recent tickets.

Device names follow the PCScope demo fleet (FIN-LT03, SAL-LT05...), so the
integration in phase 3 can link both demos. Every name here is invented.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone

from . import security
from .models import priority_for

DEMO_PASSWORD = "deskscope-demo"

# username, full name, role
DEMO_USERS = [
    ("laura.sanchez", "Laura Sánchez", "admin"),
    ("carlos.ruiz", "Carlos Ruiz", "agent"),
    ("nora.khan", "Nora Khan", "agent"),
    ("ana.garcia", "Ana García", "requester"),
    ("luis.moreau", "Luis Moreau", "requester"),
    ("marta.silva", "Marta Silva", "requester"),
    ("john.smith", "John Smith", "requester"),
    ("elena.rossi", "Elena Rossi", "requester"),
    ("omar.tanaka", "Omar Tanaka", "requester"),
]


def _t(en: str, es: str) -> dict[str, str]:
    return {"en": en, "es": es}


# Each scenario: who opened it, when (hours ago), and what happened after
# (hours after opening). Steps: ("assign", agent) | ("status", new_status) |
# ("classify", impact, urgency) | ("comment", author, text, internal).
SCENARIOS = [
    {
        "requester": "ana.garcia", "category": "hardware", "impact": "low", "urgency": "high", "device": "FIN-LT03",
        "age": 30,
        "title": _t("Laptop battery drains in about an hour", "La batería del portátil dura una hora"),
        "description": _t(
            "Since last week the battery goes from 100% to 10% in about an hour, even with only Outlook and Excel open. The laptop also gets hot near the hinge.",
            "Desde la semana pasada la batería pasa del 100 % al 10 % en una hora, aunque solo tenga abiertos Outlook y Excel. Además, el portátil se calienta cerca de la bisagra.",
        ),
        "steps": [
            (1, "assign", "carlos.ruiz"),
            (1.5, "comment", "carlos.ruiz", _t(
                "Hi Ana, could you leave it plugged in and send me a screenshot of Settings > System > Power > Battery usage?",
                "Hola, Ana. ¿Puedes dejarlo enchufado y mandarme una captura de Configuración > Sistema > Energía > Uso de la batería?",
            ), False),
            (4, "comment", "ana.garcia", _t(
                "Done, Teams is using 40% of the battery even when I'm not in a call.",
                "Hecho: Teams gasta el 40 % de la batería aunque no esté en ninguna llamada.",
            ), False),
            (5, "comment", "carlos.ruiz", _t(
                "Battery report shows 61% of design capacity. Order a replacement if the Teams fix is not enough.",
                "El informe de batería da un 61 % de la capacidad original. Si no basta con arreglar Teams, pedir una batería nueva.",
            ), True),
        ],
    },
    {
        "requester": "luis.moreau", "category": "software", "impact": "low", "urgency": "high", "device": "MKT-PC02",
        "age": 70,
        "title": _t("Acrobat keeps asking me to sign in", "Acrobat me pide iniciar sesión todo el rato"),
        "description": _t(
            "Every time I open a PDF, Adobe Acrobat asks me to sign in again and then closes.",
            "Cada vez que abro un PDF, Adobe Acrobat me pide que inicie sesión otra vez y luego se cierra.",
        ),
        "steps": [
            (2, "assign", "nora.khan"),
            (3, "comment", "nora.khan", _t(
                "I cleared the cached Adobe credentials and reassigned your licence. Can you try again?",
                "He borrado las credenciales de Adobe guardadas y te he vuelto a asignar la licencia. ¿Puedes probar otra vez?",
            ), False),
            (3.2, "status", "resolved"),
        ],
    },
    {
        "requester": "marta.silva", "category": "access", "impact": "medium", "urgency": "high", "device": None,
        "age": 26,
        "title": _t("New hire needs access to the HR shared folder", "Una nueva incorporación necesita acceso a la carpeta de RR. HH."),
        "description": _t(
            "Paula Costa starts on Monday in HR. She needs read and write access to \\\\files\\HR\\Payroll.",
            "Paula Costa empieza el lunes en RR. HH. Necesita permisos de lectura y escritura en \\\\files\\HR\\Nominas.",
        ),
        "steps": [
            (0.5, "assign", "carlos.ruiz"),
            (1, "comment", "carlos.ruiz", _t(
                "Payroll is a restricted folder: I need the approval of the HR manager. I've asked for it.",
                "Nóminas es una carpeta restringida: necesito la aprobación de la responsable de RR. HH. Ya se la he pedido.",
            ), False),
            (1.1, "status", "pending"),
            (1.2, "comment", "carlos.ruiz", _t(
                "Approval requested by email. Group: GG-HR-Payroll-RW (created by ADFlow onboarding).",
                "Aprobación pedida por correo. Grupo: GG-HR-Payroll-RW (lo crea el alta de ADFlow).",
            ), True),
        ],
    },
    {
        "requester": "john.smith", "category": "network", "impact": "low", "urgency": "high", "device": "SAL-LT05",
        "age": 5,
        "title": _t("VPN disconnects every 10 minutes", "La VPN se desconecta cada 10 minutos"),
        "description": _t(
            "Working from a client's office. The VPN drops every 10 minutes and I lose the CRM session each time.",
            "Estoy trabajando en la oficina de un cliente. La VPN se cae cada 10 minutos y cada vez pierdo la sesión del CRM.",
        ),
        "steps": [
            (0.2, "assign", "carlos.ruiz"),
            (0.3, "classify", "medium", "high"),
            (0.5, "comment", "carlos.ruiz", _t(
                "Client network blocks UDP 4500. Testing the TCP 443 fallback profile.",
                "La red del cliente bloquea el UDP 4500. Probando el perfil alternativo por TCP 443.",
            ), True),
            (0.8, "comment", "carlos.ruiz", _t(
                "John, I've pushed a new VPN profile. Please disconnect, wait a minute and connect with “Office (TCP)”.",
                "John, te he enviado un perfil de VPN nuevo. Desconéctate, espera un minuto y conéctate con «Oficina (TCP)».",
            ), False),
        ],
    },
    {
        "requester": "elena.rossi", "category": "hardware", "impact": "medium", "urgency": "medium", "device": "LEG-PC01",
        "age": 190,
        "title": _t("Printer on floor 2 prints blank pages", "La impresora de la planta 2 imprime hojas en blanco"),
        "description": _t(
            "The printer next to the legal department prints blank pages. The display says the toner is fine.",
            "La impresora que está junto a Legal saca las hojas en blanco. En la pantalla pone que el tóner está bien.",
        ),
        "steps": [
            (1, "assign", "nora.khan"),
            (2, "comment", "nora.khan", _t(
                "The new toner still had the protective tape on. Removed it and printed a test page.",
                "El tóner nuevo aún tenía la cinta de protección. La he quitado y he impreso una página de prueba.",
            ), False),
            (2.1, "status", "resolved"),
            (5, "comment", "elena.rossi", _t("Works perfectly, thanks!", "Funciona perfectamente, ¡gracias!"), False),
            (5.1, "status", "closed"),
        ],
    },
    {
        "requester": "omar.tanaka", "category": "software", "impact": "low", "urgency": "low", "device": "OPS-PC04",
        "age": 3,
        "title": _t("Request: install 7-Zip", "Solicitud: instalar 7-Zip"),
        "description": _t(
            "Suppliers send us .7z files with the delivery notes. Could you install 7-Zip on my computer?",
            "Los proveedores nos mandan los albaranes en archivos .7z. ¿Podéis instalarme 7-Zip?",
        ),
        "steps": [],
    },
    {
        "requester": "ana.garcia", "category": "access", "impact": "low", "urgency": "high", "device": None,
        "age": 240,
        "title": _t("Password expired while I was on holiday", "Mi contraseña caducó durante las vacaciones"),
        "description": _t(
            "I'm back from holiday and Windows says my password has expired. I can't sign in.",
            "He vuelto de vacaciones y Windows dice que mi contraseña ha caducado. No puedo entrar.",
        ),
        "steps": [
            (0.1, "assign", "carlos.ruiz"),
            (0.3, "comment", "carlos.ruiz", _t(
                "Identity checked by phone. Temporary password given in person; you'll be asked to change it.",
                "Identidad comprobada por teléfono. Te he dado una contraseña temporal en persona; te pedirá cambiarla.",
            ), False),
            (0.4, "status", "resolved"),
            (1, "status", "closed"),
        ],
    },
    {
        "requester": "luis.moreau", "category": "network", "impact": "medium", "urgency": "high", "device": "MKT-LT01",
        "age": 1.5,
        "title": _t("Wi-Fi in meeting room B is very slow", "El wifi de la sala de reuniones B va muy lento"),
        "description": _t(
            "Video calls freeze in meeting room B. Speed test: 2 Mbps down. In the rest of the office it's fine.",
            "Las videollamadas se congelan en la sala B. El test de velocidad da 2 Mbps de bajada. En el resto de la oficina va bien.",
        ),
        "steps": [],
    },
    {
        "requester": "marta.silva", "category": "hardware", "impact": "low", "urgency": "medium", "device": "HR-PC02",
        "age": 50,
        "title": _t("Second monitor flickers", "El segundo monitor parpadea"),
        "description": _t(
            "The monitor on the right flickers every few seconds, especially when I move the cable.",
            "El monitor de la derecha parpadea cada pocos segundos, sobre todo si muevo el cable.",
        ),
        "steps": [
            (3, "assign", "nora.khan"),
            (4, "comment", "nora.khan", _t(
                "I left a new DisplayPort cable on your desk. Could you swap it and tell me if it still happens?",
                "Te he dejado un cable DisplayPort nuevo en la mesa. ¿Puedes cambiarlo y decirme si sigue pasando?",
            ), False),
            (4.1, "status", "pending"),
        ],
    },
    {
        "requester": "john.smith", "category": "software", "impact": "medium", "urgency": "high", "device": "SAL-LT05",
        "age": 20,
        "title": _t("Outlook crashes when I open the shared sales calendar", "Outlook se cierra al abrir el calendario compartido de ventas"),
        "description": _t(
            "Outlook closes as soon as I click on the “Sales team” calendar. My own calendar works.",
            "Outlook se cierra en cuanto pulso en el calendario «Equipo de ventas». El mío funciona.",
        ),
        "steps": [
            (2, "assign", "nora.khan"),
            (2.5, "comment", "nora.khan", _t(
                "Event log: Outlook 0xc0000005 in a COM add-in. Disabling the CRM add-in as a test.",
                "Visor de eventos: Outlook 0xc0000005 en un complemento COM. Desactivo el complemento del CRM para probar.",
            ), True),
        ],
    },
    {
        "requester": "elena.rossi", "category": "other", "impact": "low", "urgency": "low", "device": None,
        "age": 100,
        "title": _t("Headset for video calls", "Auriculares para videollamadas"),
        "description": _t(
            "I'm on video calls with clients most of the day. Could I get a headset with a microphone?",
            "Paso gran parte del día en videollamadas con clientes. ¿Podría tener unos auriculares con micrófono?",
        ),
        "steps": [
            (5, "assign", "carlos.ruiz"),
            (20, "comment", "carlos.ruiz", _t(
                "Approved. You can pick it up at the IT desk on the ground floor.",
                "Aprobado. Puedes recogerlos en el puesto de informática de la planta baja.",
            ), False),
            (20.1, "status", "resolved"),
        ],
    },
    {
        "requester": "omar.tanaka", "category": "hardware", "impact": "medium", "urgency": "high", "device": "OPS-PC01",
        "age": 8,
        "title": _t("Warehouse label printer not printing", "La impresora de etiquetas del almacén no imprime"),
        "description": _t(
            "The label printer at the loading dock doesn't print. Today's shipments are waiting for labels.",
            "La impresora de etiquetas del muelle de carga no imprime. Los envíos de hoy están esperando etiquetas.",
        ),
        "steps": [
            (0.1, "assign", "carlos.ruiz"),
            (0.2, "classify", "high", "high"),
            (0.4, "comment", "carlos.ruiz", _t(
                "On my way. Meanwhile you can print the labels from OPS-PC02, it uses the same template.",
                "Voy para allá. Mientras tanto puedes imprimir las etiquetas desde OPS-PC02, que tiene la misma plantilla.",
            ), False),
        ],
    },
    {
        "requester": "ana.garcia", "category": "software", "impact": "medium", "urgency": "medium", "device": "FIN-PC02",
        "age": 0.7,
        "title": _t("Excel macros blocked after the update", "Excel bloquea las macros desde la actualización"),
        "description": _t(
            "Since this morning's update Excel blocks the macros of the monthly closing workbook (“Macros from the internet are blocked”).",
            "Desde la actualización de esta mañana, Excel bloquea las macros del libro del cierre mensual («Se han bloqueado las macros de Internet»).",
        ),
        "steps": [],
    },
    {
        "requester": "marta.silva", "category": "network", "impact": "low", "urgency": "medium", "device": None,
        "age": 400,
        "title": _t("Cannot reach the intranet from home", "No puedo entrar en la intranet desde casa"),
        "description": _t(
            "From home the intranet shows “This site can't be reached”, even with the VPN connected.",
            "Desde casa la intranet dice «No se puede acceder a este sitio web», aunque tenga la VPN conectada.",
        ),
        "steps": [
            (1, "assign", "nora.khan"),
            (2, "comment", "nora.khan", _t(
                "Your home router used the same range as the office (192.168.1.0/24). I changed the VPN profile to use the office DNS and routes.",
                "Tu router de casa usaba el mismo rango que la oficina (192.168.1.0/24). He cambiado el perfil de la VPN para que use el DNS y las rutas de la oficina.",
            ), False),
            (2.1, "status", "resolved"),
            (26, "status", "closed"),
        ],
    },
]


def _iso(moment: datetime) -> str:
    return moment.replace(microsecond=0).isoformat()


def seed_demo(
    conn: sqlite3.Connection,
    lang: str = "en",
    now: datetime | None = None,
    password: str = DEMO_PASSWORD,
) -> dict[str, int]:
    """Fill an empty database with the demo company. Returns the user ids."""
    if conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]:
        raise RuntimeError("the database is not empty; use a new file for the demo")
    lang = lang if lang in ("en", "es") else "en"
    now = now or datetime.now(timezone.utc)
    oldest = now - timedelta(hours=max(s["age"] for s in SCENARIOS) + 24)
    # One hash for every demo account keeps seeding fast; it is a public password anyway.
    password_hash = security.hash_password(password)

    ids: dict[str, int] = {}
    for username, full_name, role in DEMO_USERS:
        cur = conn.execute(
            "INSERT INTO users (username, full_name, email, role, password_hash, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (username, full_name, f"{username}@example.com", role, password_hash, _iso(oldest)),
        )
        ids[username] = cur.lastrowid
    names = {u: n for u, n, _ in DEMO_USERS}

    # Oldest first, so ticket numbers grow with time like in a real desk.
    for sc in sorted(SCENARIOS, key=lambda s: -s["age"]):
        opened = now - timedelta(hours=sc["age"])
        impact, urgency = sc["impact"], sc["urgency"]
        priority = priority_for(impact, urgency)
        cur = conn.execute(
            "INSERT INTO tickets (title, description, category, impact, urgency, priority,"
            " status, requester_id, device, created_at, updated_at)"
            " VALUES (?, ?, ?, ?, ?, ?, 'new', ?, ?, ?, ?)",
            (sc["title"][lang], sc["description"][lang], sc["category"], impact, urgency,
             priority, ids[sc["requester"]], sc["device"], _iso(opened), _iso(opened)),
        )
        tid = cur.lastrowid
        requester = ids[sc["requester"]]
        conn.execute(
            "INSERT INTO ticket_events (ticket_id, actor_id, field, new_value, created_at)"
            " VALUES (?, ?, 'created', 'new', ?)",
            (tid, requester, _iso(opened)),
        )
        status, assignee = "new", None
        resolved_at = closed_at = first_response = None
        updated = opened

        def log(actor, field, old, new, at, tid=tid):
            conn.execute(
                "INSERT INTO ticket_events (ticket_id, actor_id, field, old_value, new_value,"
                " created_at) VALUES (?, ?, ?, ?, ?, ?)",
                (tid, actor, field, old, new, _iso(at)),
            )

        for step in sc["steps"]:
            at = opened + timedelta(hours=step[0])
            kind = step[1]
            if kind == "assign":
                assignee = step[2]
                log(ids[assignee], "assignee", None, names[assignee], at)
                if status == "new":
                    log(ids[assignee], "status", "new", "open", at)
                    status = "open"
            elif kind == "status":
                actor = requester if step[2] == "closed" and resolved_at else ids[assignee]
                log(actor, "status", status, step[2], at)
                status = step[2]
                if status == "resolved":
                    resolved_at = at
                    first_response = first_response or at
                elif status == "closed":
                    closed_at = at
            elif kind == "classify":
                new_priority = priority_for(step[2], step[3])
                for field, old, new in (("impact", impact, step[2]), ("urgency", urgency, step[3]),
                                        ("priority", priority, new_priority)):
                    if old != new:
                        log(ids[assignee], field, old, new, at)
                impact, urgency, priority = step[2], step[3], new_priority
            elif kind == "comment":
                _, _, author, text, internal = step
                conn.execute(
                    "INSERT INTO comments (ticket_id, author_id, body, internal, created_at)"
                    " VALUES (?, ?, ?, ?, ?)",
                    (tid, ids[author], text[lang], int(internal), _iso(at)),
                )
                if not internal and author != sc["requester"]:
                    first_response = first_response or at
            updated = at

        conn.execute(
            "UPDATE tickets SET status = ?, impact = ?, urgency = ?, priority = ?, assignee_id = ?,"
            " updated_at = ?, resolved_at = ?, closed_at = ?, first_response_at = ? WHERE id = ?",
            (status, impact, urgency, priority, ids[assignee] if assignee else None,
             _iso(updated), _iso(resolved_at) if resolved_at else None,
             _iso(closed_at) if closed_at else None,
             _iso(first_response) if first_response else None, tid),
        )
    conn.commit()
    return ids
