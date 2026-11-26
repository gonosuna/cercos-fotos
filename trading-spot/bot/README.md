# Bot spot — guía de instalación (Windows, modo TESTNET)

Modo **asistido**: el bot te propone cada compra/venta por Telegram y solo opera si tocás **Sí**. Sin respuesta, no hace nada.
Arranca en **testnet** (dinero ficticio). El modo real está bloqueado hasta que lo habilites vos.

## 1. Python
Abrí PowerShell: `python --version` (sirve 3.9 o superior). Después, dentro de esta carpeta:
```
python -m pip install -r requirements.txt
```

## 2. Claves de la testnet (dinero ficticio)
1. Entrá a https://testnet.binance.vision con tu cuenta de GitHub → **Generate HMAC-SHA-256 Key**.
2. Copiá `.env.example` como `.env` y pegá ahí `TESTNET_API_KEY` y `TESTNET_API_SECRET`.
   (Nunca pegues claves en el chat ni subas `.env` a GitHub.)

## 3. Telegram
1. En Telegram hablá con **@BotFather** → `/newbot` → copiá el token en `TELEGRAM_TOKEN` del `.env`.
2. Corré el bot (paso 5), mandale `/start` a tu bot: te responde tu **chat id**. Pegalo en `TELEGRAM_CHAT_ID`, y reiniciá el bot.

## 4. Planilla de Google Sheets (opcional al principio)
1. Creá una planilla nueva en tu cuenta de Google.
2. **Extensiones → Apps Script** → pegá el contenido de `apps_script/Code.gs`.
3. **Configuración del proyecto → Propiedades del script** → agregá `SECRET` con una clave larga inventada por vos.
4. Ejecutá la función `setup` una vez (te pide permisos).
5. **Implementar → Nueva implementación → Aplicación web** (ejecutar como: yo; acceso: cualquier persona). Copiá la URL.
6. En `.env`: `SHEETS_URL` = esa URL y `SHEETS_SECRET` = la misma clave del paso 3.

## 5. Correr
```
python run.py
```
Dejá la ventana abierta (si apagás la notebook, el bot se detiene). Para frenarlo: Ctrl+C.
Probar sin esperar señales: en Telegram `/comprar UNI 100` y `/vender UNI` (piden confirmación).
Nota: los precios de la testnet no son los del mercado real, así que las señales de caída pueden aparecer poco o raro.

## Comandos de Telegram
`/estado` `/reporte` `/pausa` `/reanudar` `/modo asistido` `/modo automatico si` `/congelar UNI` `/liberar UNI` `/comprar UNI 150` `/vender UNI`

## Reglas (en `config.json`)
- Compras solo lun-vie 10:00-18:00 (Argentina), máx. 3 por día. Ventas 24/7.
- Entrada: caída ≥ `entry_drop` desde el máximo de 30 días + RSI diario < `entry_rsi` + pico de volumen ya enfriado.
- DCA: 4 tramos (150/200/300/350 → ajustados a mín. 100) a 0 / -7% / -15% / -25% desde la 1ª compra. Tope 60% del capital por cripto.
- Venta: a +6,5% neto activa el seguimiento; vende si cae 1,5% desde el máximo; piso duro +5% neto. Sin stop-loss.
- ⚠️ `entry_drop` y `entry_rsi` están en los valores originales (15% / 35). El backtest los rechazó; hay que decidirlos antes del modo real.

## Seguridad
- El bot se niega a arrancar en modo real si la clave API de Binance tiene permiso de **retiros**.
- Modo real: requiere claves reales en `.env` **y** cambiar `mode` a `real` y `real_confirmed` a `true`. No lo hagas hasta aprobar backtest + testnet.
- Archivos que genera: `state.json` (posiciones), `decisions.csv` (tus Sí/No, para aprender tus patrones), `bot.log`, `outbox.json`.

## Pruebas
`python -m unittest discover -s tests` (usan un exchange simulado, no tocan Binance).
