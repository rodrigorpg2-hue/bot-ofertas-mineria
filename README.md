# Bot de ofertas laborales – Minería / Geomensura (Chile)

Busca ofertas de **Ingeniero Civil en Minas** e **Ingeniero Geomensor** en LinkedIn y Computrabajo y las envía a Telegram.

- `buscador_ofertas_telegram.py`: script principal.
- `.github/workflows/ofertas.yml`: ejecución automática gratis con GitHub Actions (08:00 y 18:00 hora de Chile).
- `render.yaml`: alternativa como Cron Job en Render.

## Configuración
Variables / secretos necesarios (nunca escribir el token en el código):
- `TELEGRAM_TOKEN`: token del bot (@BotFather).
- `TELEGRAM_CHAT_ID`: opcional; si falta, se detecta del último mensaje enviado al bot.

Prueba local: `python buscador_ofertas_telegram.py --dry-run`
