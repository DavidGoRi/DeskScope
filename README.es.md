# DeskScope

**Un sistema de tickets de soporte informático para oficinas pequeñas: tickets, roles, historial de cambios y, en las próximas fases, SLA, base de conocimiento e integración con el resto de herramientas.**

DeskScope es la cuarta pieza de un pequeño kit de sistemas: [PCScope](https://github.com/DavidGoRi/PCScope) (inventario de PCs), [NetScope](https://github.com/DavidGoRi/NetScope) (descubrimiento de red) y [ADFlow](https://github.com/DavidGoRi/ADFlow) (altas y bajas en Active Directory). La idea es el ciclo de un departamento de soporte real: *el inventario detecta un problema → se abre un ticket → un técnico lo resuelve → la solución queda documentada*.

*Read in [English](README.md).*

## Qué hace ya (v0.3, fases 1 a 3)

- **Tickets** con categoría, prioridad, equipo, estado y conversación.
- **Tres roles**: el empleado abre y sigue *sus* tickets; el técnico ve y gestiona todos; el administrador además gestiona las cuentas.
- **Flujo** `Nuevo → En curso → Esperando al usuario → Resuelto → Cerrado` con reglas por rol. El empleado confirma o rechaza la solución, y si responde a un ticket en espera, este vuelve a la cola.
- **Notas internas** que el empleado no ve, e **historial** de cada cambio: quién y cuándo.
- **Búsqueda y filtros** por número, texto, equipo, estado, "asignados a mí" y "sin asignar".
- Interfaz en **inglés y español**, modo claro y oscuro, y uso desde el móvil.

### SLA al estilo ITIL

- **Prioridad:** el empleado no la elige. Responde a dos preguntas:
  - *¿a quién afecta?* (impacto);
  - *¿puedes seguir trabajando?* (urgencia).

  La prioridad sale de la matriz ITIL y se ve mientras escribe. El técnico puede reclasificar el ticket y cada cambio queda en el historial.
- **Dos relojes por ticket:** primera respuesta y resolución.
- **Horario laboral:** solo cuenta el tiempo de trabajo, con los días, el horario, la zona horaria y los festivos de `sla.json`.
- **Pausa:** el reloj de resolución se para mientras el ticket espera al usuario.
- **Estados:** en plazo, en riesgo (al 75 % del plazo), incumplido, en pausa y cumplido.
- **En la lista:** contadores, filtro "en riesgo o incumplido" y orden por vencimiento.
- **En la ficha:** barras de progreso para los dos relojes.

`sla.example.json` trae el horario de Madrid y los festivos nacionales de 2026 y 2027. `deskscope sla` comprueba el archivo y muestra la política que se está usando.

### Integraciones: las herramientas abren sus propios tickets

- **PCScope** (`devices.csv` e `issues.csv` de `-ExportCsv`):
  - una ficha por equipo (modelo, número de serie, versión de Windows, disco, BitLocker, antivirus, último usuario…), visible dentro de cada ticket de ese equipo junto con sus otros tickets;
  - un ticket por equipo y regla para los avisos críticos, o para los niveles que elijas.
- **NetScope** (`scan-*.json`): tickets para los hallazgos críticos (Telnet abierto, SMBv1…) y para los dispositivos que aparecen por primera vez.
- **ADFlow** (`summary.json` de una ejecución real): un ticket de alta por cada cuenta creada, con sus grupos, buzón, carpeta y lista de tareas, y un ticket de baja por cada cuenta desactivada.

Las importaciones se pueden repetir sin efectos secundarios y se corrigen solas:
- cada hallazgo tiene una clave estable y la base de datos no admite dos tickets activos con la misma;
- si cambia el detalle (93 % → 97 %), se añade una nota interna en lugar de abrir otro ticket;
- cuando el problema desaparece, el ticket se resuelve automáticamente con una nota;
- si el problema vuelve, se abre un ticket nuevo;
- lo que necesita a una persona (un dispositivo nuevo, un alta) nunca se cierra solo.

```bash
deskscope import pcscope  \\servidor\pcscope\export --lang es
deskscope import netscope C:\NetScope\output --lang es
deskscope import adflow   C:\ADFlow\output\runs\<ejecución> --lang es
```

### API REST

Todo está disponible en `/api/v1`, con la documentación interactiva en `/api/docs`.
- **Tokens:** se crean en *Tokens de API*. Se muestran una sola vez y solo se guarda su SHA-256.
- **CSRF:** la API solo acepta la cabecera `Authorization`, nunca la cookie de sesión, así que no le afecta.
- **Permisos:** usa los mismos permisos que la web; el token de un empleado solo ve sus tickets.

Así, la tarea programada de PCScope puede enviar sus resultados directamente (hay un ejemplo con `Invoke-RestMethod` en el [README en inglés](README.md#rest-api)).

### Seguridad

- **Contraseñas:** PBKDF2-SHA256 con 600 000 iteraciones.
- **Sesión:**
  - token CSRF en cada formulario;
  - cookies `HttpOnly` y `SameSite=Lax`;
  - sesión nueva en cada inicio de sesión.
- **Fuerza bruta:** se bloquea tras 5 intentos fallidos en 15 minutos.
- **Cabeceras HTTP:** CSP estricta y `X-Frame-Options: DENY`.
- **Tickets ajenos:** responden 404 y no 403, para que no se puedan sondear los números.
- **Permisos:** se comprueban en la capa de negocio, no solo en las páginas.

## Probarlo

```bash
pip install -e .
deskscope demo --lang es        # empresa ficticia con 14 tickets
deskscope serve                 # http://127.0.0.1:8000
```

Todas las cuentas de la demo tienen la contraseña `deskscope-demo`:
- `carlos.ruiz` (técnico);
- `laura.sanchez` (administradora);
- `ana.garcia` (empleada).

## Hoja de ruta

1. ✅ **Núcleo:** tickets, roles, flujo, comentarios, historial e interfaz bilingüe.
2. ✅ **SLA (ITIL):** matriz impacto × urgencia, plazos en horario laboral con festivos y pausa mientras se espera al usuario.
3. ✅ **Integraciones:** ficha de PCScope en el ticket, tickets que se abren y se resuelven solos desde PCScope y NetScope, altas y bajas de ADFlow, y API REST con tokens.
4. **Base de conocimiento y métricas:** tiempo medio de resolución, % de SLA cumplido y exportación a CSV.
5. **Publicación:** correo, Telegram, Docker, demo en vivo y v1.0.0.

Licencia [MIT](LICENSE). Todos los nombres de la demo son ficticios.
