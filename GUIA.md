# Guía de instalación (español)

Tu secretario personal en Telegram, con Claude. Vas a juntar 4 claves y pegarlas en Railway.
Guardalas como contraseñas: nunca van en el código ni en GitHub.

Tiempo total: unos 40 minutos. Costo: Railway unos USD 5/mes + el uso de Claude (pocos dólares
al mes con Haiku) + la voz si la activás (centavos).

---

## Paso 1: Crear el bot de Telegram (2 min)

1. En Telegram abrí un chat con **@BotFather**.
2. Mandale `/newbot`, elegí un nombre (por ejemplo *Secretario*) y un usuario que termine en `bot`.
3. Te da un **token** tipo `123456:ABC-xyz…`. Ese es tu `TELEGRAM_BOT_TOKEN`.

## Paso 2: Clave de Claude (5 min)

1. Entrá a **platform.claude.com** (es aparte de tu suscripción de Claude).
2. **Billing** → cargá crédito (USD 5–10 para empezar) y poné un límite mensual.
3. **API Keys → Create Key**. Esa es tu `ANTHROPIC_API_KEY`.

## Paso 3: Permiso para Google Calendar (15 min)

1. Entrá a **console.cloud.google.com** → arriba, *Nuevo proyecto* → nombre "Secretario".
2. **APIs y servicios → Biblioteca**: habilitá **Google Calendar API**, **Gmail API** y **Google Drive API**.
3. **APIs y servicios → Pantalla de consentimiento de OAuth** (puede llamarse *Google Auth Platform*):
   - Tipo de usuario: **Externo**. Nombre: "Secretario". Tu mail en soporte y contacto.
   - En **Público / Usuarios de prueba**, agregá tu Gmail.
   - Tocá **Publicar app** (estado *En producción*).
     ⚠️ **No te saltees esto**: si queda en "Prueba", Google desconecta el secretario cada 7 días.
     No hace falta verificación para uso personal.
4. **APIs y servicios → Credenciales → Crear credenciales → ID de cliente de OAuth**:
   - Tipo: **App de escritorio** → Crear.
   - El **ID de cliente** es tu `GOOGLE_CLIENT_ID` y el **Secreto** es tu `GOOGLE_CLIENT_SECRET`.

## Paso 4 (opcional): Audios (5 min)

Claude no escucha audio, así que la voz pasa por OpenAI (medio centavo de dólar por minuto escuchado).

1. Entrá a **platform.openai.com** → **Billing** → cargá un poco de crédito.
2. **API keys → Create new secret key**. Esa es tu `OPENAI_API_KEY`.

## Paso 5: Subirlo a Railway (10 min)

1. En **railway.com**: **New Project → Deploy from GitHub repo** → elegí **Cosacomarcoux/personal-assistant**.
2. Abrí el servicio → **Variables** → **Raw Editor** y pegá esto, completando tus claves:

```
ANTHROPIC_API_KEY=tu-clave-de-claude
TELEGRAM_BOT_TOKEN=tu-token-de-telegram
GOOGLE_CLIENT_ID=tu-client-id
GOOGLE_CLIENT_SECRET=tu-client-secret
OPENAI_API_KEY=tu-clave-de-openai
TIMEZONE=America/Argentina/Buenos_Aires
OWNER_NAME=Roberto
ASSISTANT_NAME=Secretario
BRIEFING_TIME=07:00
MODEL=claude-haiku-5-5
ENABLED_SKILLS=google,tasks,people,admin,content,research,writing,ideas
CURRENCY=ARS
DATA_DIR=/data
```

3. **Disco para que no se olvide nada**: clic derecho en el servicio → **Attach Volume** → ruta `/data`.
   (Sin esto, cada deploy borra tareas, cumpleaños y la conexión con Google.)
4. Esperá a que el deploy quede en verde.

## Paso 6: Que solo te responda a vos (3 min)

1. En Telegram, mandale cualquier mensaje a tu bot. Te responde con **tu ID de Telegram**.
2. En Railway → Variables agregá `OWNER_TELEGRAM_ID=ese-número`. Railway reinicia solo.
3. Mandale `/connect_google` y seguí los 3 pasos que te indica.
4. Probá: *"¿Qué tengo hoy en el calendario?"* o `/briefing`.

---

## Para empezar a usarlo

Mandale cosas así (escritas o en audio):

- *"El cumple de Sofi es el 14 de marzo, agendalo todos los años"* → evento anual en Google Calendar
  con aviso de Google al celu (te pide ✅ antes de crearlo).
- *"Todos los 5 a las 9 recordame pagar los sueldos"* → recordatorio mensual por Telegram.
- *"Mi hermana Daniela cumple el 2 de agosto, le gustan las plantas"* → la guarda en tus contactos
  con ideas de regalo; aparece en el resumen de la mañana una semana antes.
- *"Armame un guion de 60 segundos para un reel sobre…"*
- *"Gasté 15.000 en el súper"*

Cada mañana a las 7:00 te manda el resumen del día.

## Seguridad

- Mandar mails, archivar y crear o borrar eventos **siempre** te pide ✅ Aprobar / ❌ Rechazar.
- Solo te responde a vos (tu `OWNER_TELEGRAM_ID`).
- El modo manos libres (`/talk`) necesita generar un dominio en Railway. El botón
  "Abrir en el navegador" da un link que sirve 30 días: **no lo compartas**.

## Si algo falla

- **No contesta**: Railway → Deployments → Logs. "Missing settings" = falta una variable.
- **"Google is not connected"**: mandá `/connect_google`. Si pasa cada semana, falta *Publicar app* (Paso 3.3).
- **Se olvidó todo después de un deploy**: falta el disco en `/data` (Paso 5.3).
- **Querés que piense mejor**: cambiá `MODEL=claude-sonnet-5-5` (cuesta más).
