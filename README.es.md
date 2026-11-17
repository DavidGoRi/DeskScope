# DeskScope

**Un sistema de tickets de soporte informático para oficinas pequeñas: tickets, roles, historial de cambios y, en las próximas fases, SLA, base de conocimiento e integración con el resto de herramientas.**

DeskScope es la cuarta pieza de un pequeño kit de sistemas: [PCScope](https://github.com/DavidGoRi/PCScope) (inventario de PCs), [NetScope](https://github.com/DavidGoRi/NetScope) (descubrimiento de red) y [ADFlow](https://github.com/DavidGoRi/ADFlow) (altas y bajas en Active Directory). La idea es el ciclo de un departamento de soporte real: *el inventario detecta un problema → se abre un ticket → un técnico lo resuelve → la solución queda documentada*.

*Read in [English](README.md).*

## Qué hace ya (v0.1, fase 1)

- **Tickets** con categoría, prioridad, equipo, estado y conversación.
- **Tres roles**: el empleado abre y sigue *sus* tickets; el técnico ve y gestiona todos; el administrador además gestiona las cuentas.
- **Flujo** `Nuevo → En curso → Esperando al usuario → Resuelto → Cerrado` con reglas por rol. El empleado confirma o rechaza la solución, y si responde a un ticket en espera, este vuelve a la cola.
- **Notas internas** que el empleado no ve, e **historial** de cada cambio: quién y cuándo.
- **Búsqueda y filtros** por número, texto, equipo, estado, "asignados a mí" y "sin asignar".
- Interfaz en **inglés y español**, modo claro y oscuro, y uso desde el móvil.

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
2. **SLA (ITIL):** matriz impacto × urgencia y plazos en horario laboral con festivos.
3. **Integraciones:**
   - ficha de PCScope en el ticket;
   - tickets automáticos desde PCScope y NetScope;
   - altas de ADFlow;
   - API REST.
4. **Base de conocimiento y métricas:** tiempo medio de resolución, % de SLA cumplido y exportación a CSV.
5. **Publicación:** correo, Telegram, Docker, demo en vivo y v1.0.0.

Licencia [MIT](LICENSE). Todos los nombres de la demo son ficticios.
