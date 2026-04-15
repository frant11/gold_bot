"""
=============================================================
  GOLD EMA9/EMA25 + RSI + VOLUMEN BOT
  Versión Railway - Las credenciales se leen de variables
  de entorno, nunca hardcodeadas en el código.
=============================================================
"""

import os
import requests
import time
import pandas as pd
from datetime import datetime

# ─────────────────────────────────────────
#  CONFIG — leída desde variables de entorno
# ─────────────────────────────────────────
API_KEY      = os.environ["CAP_API_KEY"]
IDENTIFIER   = os.environ["CAP_EMAIL"]
PASSWORD     = os.environ["CAP_PASSWORD"]

CAPITAL_INICIAL  = float(os.getenv("CAPITAL_INICIAL",  "40"))
META_CAPITAL     = float(os.getenv("META_CAPITAL",    "100"))
PROFIT_POR_TRADE = float(os.getenv("PROFIT_POR_TRADE", "10"))
STOP_LOSS_USD    = float(os.getenv("STOP_LOSS_USD",     "5"))

EMA_RAPIDA   = 9
EMA_LENTA    = 25
RSI_PERIODO  = 14
VOL_PERIODO  = 20
RSI_MAX_BUY  = 55
RSI_MIN_SELL = 45

INTERVALO_SEG = 60
EPIC          = "GOLD"
RESOLUCION    = "MINUTE_5"
VELAS         = 60

BASE_URL = "https://demo-api-capital.backend-capital.com"

session_headers = {}

# ─────────────────────────────────────────
#  UTILIDADES
# ─────────────────────────────────────────
def log(msg: str):
    # Railway captura stdout automáticamente como logs
    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {msg}", flush=True)

def iniciar_sesion():
    url = f"{BASE_URL}/api/v1/session"
    payload = {
        "identifier": IDENTIFIER,
        "password": PASSWORD,
        "encryptedPassword": False
    }
    headers = {
        "Content-Type": "application/json",
        "X-CAP-API-KEY": API_KEY
    }
    r = requests.post(url, json=payload, headers=headers)
    r.raise_for_status()
    session_headers["X-SECURITY-TOKEN"] = r.headers.get("X-SECURITY-TOKEN", "")
    session_headers["CST"]              = r.headers.get("CST", "")
    session_headers["Content-Type"]     = "application/json"
    log("✅ Sesión iniciada.")

def get_headers():
    return session_headers

# ─────────────────────────────────────────
#  DATOS DE MERCADO
# ─────────────────────────────────────────
def obtener_velas(epic, resolucion, cantidad) -> pd.DataFrame:
    url = f"{BASE_URL}/api/v1/prices/{epic}"
    params = {"resolution": resolucion, "max": cantidad}
    r = requests.get(url, headers=get_headers(), params=params)
    r.raise_for_status()
    data = r.json().get("prices", [])

    rows = []
    for p in data:
        close = p["closePrice"]["bid"]
        vol   = p.get("lastTradedVolume", 0)
        high  = p["highPrice"]["bid"]
        low   = p["lowPrice"]["bid"]
        if vol == 0:
            vol = round((high - low) * 1000, 2)
        rows.append({"close": close, "volume": vol})
    return pd.DataFrame(rows)

def obtener_precio_actual(epic) -> float:
    url = f"{BASE_URL}/api/v1/markets/{epic}"
    r = requests.get(url, headers=get_headers())
    r.raise_for_status()
    return r.json()["snapshot"]["bid"]

# ─────────────────────────────────────────
#  INDICADORES
# ─────────────────────────────────────────
def calcular_ema(serie, periodo):
    return serie.ewm(span=periodo, adjust=False).mean()

def calcular_rsi(serie, periodo=14) -> float:
    delta    = serie.diff()
    ganancia = delta.clip(lower=0)
    perdida  = (-delta).clip(lower=0)
    avg_g    = ganancia.rolling(window=periodo).mean().iloc[-1]
    avg_p    = perdida.rolling(window=periodo).mean().iloc[-1]
    if avg_p == 0:
        return 100.0
    return round(100 - (100 / (1 + avg_g / avg_p)), 2)

def analizar(df) -> dict:
    closes  = df["close"]
    volumes = df["volume"]

    ema9  = calcular_ema(closes, EMA_RAPIDA)
    ema25 = calcular_ema(closes, EMA_LENTA)
    rsi   = calcular_rsi(closes, RSI_PERIODO)

    ema9_actual  = ema9.iloc[-1]
    ema9_prev    = ema9.iloc[-2]
    ema25_actual = ema25.iloc[-1]
    ema25_prev   = ema25.iloc[-2]

    vol_actual   = volumes.iloc[-1]
    vol_promedio = volumes.iloc[-VOL_PERIODO:].mean()
    vol_alto     = vol_actual > vol_promedio

    cruce_alcista = (ema9_prev <= ema25_prev) and (ema9_actual > ema25_actual)
    cruce_bajista = (ema9_prev >= ema25_prev) and (ema9_actual < ema25_actual)

    señal = None
    if cruce_alcista and rsi < RSI_MAX_BUY and vol_alto:
        señal = "BUY"
    elif cruce_bajista and rsi > RSI_MIN_SELL and vol_alto:
        señal = "SELL"

    return {
        "ema9": round(ema9_actual, 4), "ema25": round(ema25_actual, 4),
        "rsi": rsi, "vol_actual": round(vol_actual, 2),
        "vol_promedio": round(vol_promedio, 2), "vol_alto": vol_alto,
        "cruce_alcista": cruce_alcista, "cruce_bajista": cruce_bajista,
        "señal": señal
    }

def hay_cruce_contrario(df, direccion) -> bool:
    ema9  = calcular_ema(df["close"], EMA_RAPIDA)
    ema25 = calcular_ema(df["close"], EMA_LENTA)
    a9, p9   = ema9.iloc[-1],  ema9.iloc[-2]
    a25, p25 = ema25.iloc[-1], ema25.iloc[-2]
    if direccion == "BUY":
        return (p9 >= p25) and (a9 < a25)
    return (p9 <= p25) and (a9 > a25)

# ─────────────────────────────────────────
#  OPERACIONES
# ─────────────────────────────────────────
def calcular_size(balance, precio) -> float:
    mov_usd  = precio * 0.005
    size     = round(PROFIT_POR_TRADE / mov_usd, 4)
    size_max = round((balance * 0.30) / precio, 4)
    return min(size, size_max)

def abrir_posicion(direccion, size, precio):
    if direccion == "BUY":
        sl = round(precio - STOP_LOSS_USD, 2)
        tp = round(precio + PROFIT_POR_TRADE, 2)
    else:
        sl = round(precio + STOP_LOSS_USD, 2)
        tp = round(precio - PROFIT_POR_TRADE, 2)

    payload = {
        "epic": EPIC, "direction": direccion,
        "size": size, "guaranteedStop": False,
        "stopLevel": sl, "profitLevel": tp
    }
    r = requests.post(f"{BASE_URL}/api/v1/positions", headers=get_headers(), json=payload)
    if r.status_code == 200:
        deal = r.json().get("dealReference", "N/A")
        log(f"  ✅ {direccion} abierta | Size: {size} | SL: {sl} | TP: {tp} | Deal: {deal}")
        return deal
    log(f"  ⚠️  Error: {r.status_code} - {r.text}")
    return None

def obtener_posiciones_abiertas():
    r = requests.get(f"{BASE_URL}/api/v1/positions", headers=get_headers())
    r.raise_for_status()
    return r.json().get("positions", [])

def cerrar_posicion(deal_id):
    r = requests.delete(f"{BASE_URL}/api/v1/positions/{deal_id}", headers=get_headers())
    if r.status_code == 200:
        log(f"  🔒 Posición {deal_id} cerrada.")
    else:
        log(f"  ⚠️  Error al cerrar {deal_id}: {r.text}")

def obtener_balance() -> float:
    r = requests.get(f"{BASE_URL}/api/v1/accounts", headers=get_headers())
    r.raise_for_status()
    cuentas = r.json().get("accounts", [])
    return cuentas[0].get("balance", {}).get("balance", 0.0) if cuentas else 0.0

# ─────────────────────────────────────────
#  BUCLE PRINCIPAL
# ─────────────────────────────────────────
def main():
    log("=" * 50)
    log("  🤖  GOLD EMA9/EMA25 + RSI + VOLUMEN BOT")
    log(f"  Capital: ${CAPITAL_INICIAL}  →  Meta: ${META_CAPITAL}")
    log(f"  TP: +${PROFIT_POR_TRADE} | SL: -${STOP_LOSS_USD}")
    log("=" * 50)

    iniciar_sesion()
    ultimo_refresh  = time.time()
    operaciones     = 0
    posicion_activa = None

    while True:
        try:
            if time.time() - ultimo_refresh > 480:
                iniciar_sesion()
                ultimo_refresh = time.time()

            balance = obtener_balance()
            log(f"💰 Balance: ${balance:.2f} | Ops: {operaciones}")

            if balance >= META_CAPITAL:
                log(f"🎯 ¡META ALCANZADA! ${balance:.2f}. Bot detenido.")
                break

            df       = obtener_velas(EPIC, RESOLUCION, VELAS)
            analisis = analizar(df)

            log(f"📊 EMA9:{analisis['ema9']} EMA25:{analisis['ema25']} "
                f"RSI:{analisis['rsi']} "
                f"Vol:{'🔊' if analisis['vol_alto'] else '🔇'}")

            posiciones = obtener_posiciones_abiertas()
            pos_gold   = [p for p in posiciones if p.get("market", {}).get("epic") == EPIC]

            if pos_gold and posicion_activa:
                if hay_cruce_contrario(df, posicion_activa["direccion"]):
                    log("🔁 Cruce contrario → cerrando")
                    cerrar_posicion(posicion_activa["deal_id"])
                    posicion_activa = None
                else:
                    log(f"⏳ Manteniendo {posicion_activa['direccion']}...")
                time.sleep(INTERVALO_SEG)
                continue

            if pos_gold:
                log("⏳ Posición externa activa, esperando...")
                time.sleep(INTERVALO_SEG)
                continue

            posicion_activa = None
            señal = analisis["señal"]

            if señal == "BUY":
                log(f"🟢 SEÑAL BUY | RSI:{analisis['rsi']}")
                precio = obtener_precio_actual(EPIC)
                deal   = abrir_posicion("BUY", calcular_size(balance, precio), precio)
                if deal:
                    posicion_activa = {"deal_id": deal, "direccion": "BUY"}
                    operaciones += 1

            elif señal == "SELL":
                log(f"🔴 SEÑAL SELL | RSI:{analisis['rsi']}")
                precio = obtener_precio_actual(EPIC)
                deal   = abrir_posicion("SELL", calcular_size(balance, precio), precio)
                if deal:
                    posicion_activa = {"deal_id": deal, "direccion": "SELL"}
                    operaciones += 1
            else:
                log("⚪ Sin señal.")

            time.sleep(INTERVALO_SEG)

        except requests.exceptions.HTTPError as e:
            log(f"❌ HTTP Error: {e}. Reintentando en 30s...")
            time.sleep(30)
            iniciar_sesion()
            ultimo_refresh = time.time()

        except Exception as e:
            log(f"❌ Error: {e}. Reintentando en 30s...")
            time.sleep(30)

if __name__ == "__main__":
    main()
