"""
MAKTK Bot v5 — Telegram Bot + Flask API
Mini App için gerçek zamanlı veri sağlar
"""

import asyncio
import json
import logging
import threading
import time
from datetime import datetime, timedelta

import pandas as pd
import pytz
import schedule
import ta as ta_lib
import yfinance as yf
from flask import Flask, jsonify
from flask_cors import CORS
from telegram import Bot
from telegram.ext import (ApplicationBuilder, CommandHandler,
                           ContextTypes, MessageHandler, filters)

# ════════════════════════════════════════════
# ⚙️  AYARLAR
# ════════════════════════════════════════════
TELEGRAM_TOKEN  = "8689687643:AAFTdXNnmouBc4bvmv32sgxuz_MrXF0ITmc"
YONETICI_ID     = "1268937981"
USER_FILE       = "bot_users.json"
SEMBOL          = "MAKTK.IS"
PORT            = 8080

RSI_ASIRI_SATIM     = 35
RSI_ASIRI_ALIM      = 65
FIYAT_ALARM_YUKARI  = 0.0
FIYAT_ALARM_ASAGI   = 0.0
GUNLUK_RAPOR_SAATI  = "09:05"
HAFTALIK_OZET_GUNU  = "friday"
HAFTALIK_OZET_SAAT  = "18:00"
KONTROL_SIKLIGI_DK  = 15
SEVIYE_TOLERANS_PCT = 1.5
ILERIYE_BAK_GUN     = 5
MIN_TEST_SAYISI     = 2
ANOMALI_HACIM_KATI  = 3.0
CONFLUENCE_ESIK     = 3

# ════════════════════════════════════════════
# 📋  LOGGING
# ════════════════════════════════════════════
logging.basicConfig(
    format="%(asctime)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

son_sinyaller: dict = {}
haftalik_sinyal_gecmisi: list = []
senaryo_bekliyor: dict = {}

# Cache — API her çağrıda yfinance'e gitmez
veri_cache: dict = {}
cache_zamani: dict = {}
CACHE_SURE = 60  # saniye

# ════════════════════════════════════════════
# 👥  KULLANICI YÖNETİMİ
# ════════════════════════════════════════════
def kullanici_kaydet(chat_id):
    chat_id = str(chat_id)
    try:
        with open(USER_FILE) as f:
            users = json.load(f)
    except:
        users = []
    if chat_id not in users:
        users.append(chat_id)
        with open(USER_FILE, "w") as f:
            json.dump(users, f)

def kullanicilari_getir():
    try:
        with open(USER_FILE) as f:
            return json.load(f)
    except:
        return [YONETICI_ID]

def kullanici_sil(chat_id):
    users = kullanicilari_getir()
    users = [u for u in users if u != str(chat_id)]
    with open(USER_FILE, "w") as f:
        json.dump(users, f)

# ════════════════════════════════════════════
# 🗓️  BORSA TAKVİMİ
# ════════════════════════════════════════════
def borsa_acik_mi():
    try:
        tz = pytz.timezone("Europe/Istanbul")
        simdi = datetime.now(tz)
        if simdi.weekday() >= 5:
            return False
        acilis  = simdi.replace(hour=10, minute=0,  second=0, microsecond=0)
        kapanis = simdi.replace(hour=18, minute=15, second=0, microsecond=0)
        return acilis <= simdi <= kapanis
    except:
        return False

def sonraki_acilis():
    try:
        tz = pytz.timezone("Europe/Istanbul")
        simdi = datetime.now(tz)
        gun = simdi
        for _ in range(7):
            gun += timedelta(days=1)
            if gun.weekday() < 5:
                break
        acilis = gun.replace(hour=10, minute=0, second=0, microsecond=0)
        fark   = acilis - simdi
        saat   = int(fark.total_seconds() // 3600)
        dakika = int((fark.total_seconds() % 3600) // 60)
        return f"{saat}s {dakika}dk"
    except:
        return "bilinmiyor"

# ════════════════════════════════════════════
# 📊  VERİ & İNDİKATÖRLER
# ════════════════════════════════════════════
def veri_cek(interval, period):
    cache_key = f"{interval}_{period}"
    now = time.time()
    if cache_key in veri_cache and now - cache_zamani.get(cache_key, 0) < CACHE_SURE:
        return veri_cache[cache_key]
    try:
        df = yf.download(SEMBOL, period=period, interval=interval, progress=False)
        if df is None or df.empty or len(df) < 30:
            return None
        df.columns = [c[0] if isinstance(c, tuple) else c for c in df.columns]
        veri_cache[cache_key] = df
        cache_zamani[cache_key] = now
        return df
    except Exception as e:
        logger.error(f"Veri hatası ({interval}): {e}")
        return None

def indiktorleri_hesapla(df):
    try:
        df = df.copy()
        df["RSI"]        = ta_lib.momentum.RSIIndicator(df["Close"], window=14).rsi()
        macd             = ta_lib.trend.MACD(df["Close"], window_fast=12, window_slow=26, window_sign=9)
        df["MACD"]       = macd.macd()
        df["MACD_signal"]= macd.macd_signal()
        df["MACD_hist"]  = macd.macd_diff()
        df["MA20"]       = ta_lib.trend.SMAIndicator(df["Close"], window=20).sma_indicator()
        df["MA50"]       = ta_lib.trend.SMAIndicator(df["Close"], window=50).sma_indicator()
        bb               = ta_lib.volatility.BollingerBands(df["Close"], window=20, window_dev=2)
        df["BB_upper"]   = bb.bollinger_hband()
        df["BB_lower"]   = bb.bollinger_lband()
        df["Hacim_MA20"] = df["Volume"].rolling(window=20).mean()
        return df
    except Exception as e:
        logger.error(f"İndikatör hatası: {e}")
        return df

def _f(val):
    try:
        v = float(val)
        return None if pd.isna(v) else v
    except:
        return None

def sinyal_uret(df, interval):
    try:
        son    = df.iloc[-1]
        onceki = df.iloc[-2]
        fiyat  = _f(son["Close"])
        s = dict(
            fiyat=fiyat,
            rsi=_f(son.get("RSI")),
            macd=_f(son.get("MACD")),
            macd_signal=_f(son.get("MACD_signal")),
            macd_hist=_f(son.get("MACD_hist")),
            ma20=_f(son.get("MA20")),
            ma50=_f(son.get("MA50")),
            bb_upper=_f(son.get("BB_upper")),
            bb_lower=_f(son.get("BB_lower")),
            hacim=_f(son["Volume"]),
            hacim_ma20=_f(son.get("Hacim_MA20")),
            rsi_asiri_satim=False, rsi_asiri_alim=False,
            macd_kesisim_al=False, macd_kesisim_sat=False,
            bb_alt_dokunu=False,   bb_ust_dokunu=False,
            yuksek_hacim=False,    trend="YATAY",
        )
        if s["rsi"]:
            s["rsi_asiri_satim"] = s["rsi"] < RSI_ASIRI_SATIM
            s["rsi_asiri_alim"]  = s["rsi"] > RSI_ASIRI_ALIM
        om  = _f(onceki.get("MACD"))
        osg = _f(onceki.get("MACD_signal"))
        if all([s["macd"], s["macd_signal"], om, osg]):
            s["macd_kesisim_al"]  = om < osg and s["macd"] > s["macd_signal"]
            s["macd_kesisim_sat"] = om > osg and s["macd"] < s["macd_signal"]
        if s["bb_lower"] and fiyat: s["bb_alt_dokunu"] = fiyat <= s["bb_lower"]
        if s["bb_upper"] and fiyat: s["bb_ust_dokunu"] = fiyat >= s["bb_upper"]
        if s["hacim"] and s["hacim_ma20"]:
            s["yuksek_hacim"] = s["hacim"] > s["hacim_ma20"] * 1.5
        if s["ma20"] and s["ma50"]:
            s["trend"] = "YÜKSELİŞ" if s["ma20"] > s["ma50"] else "DÜŞÜŞ"
        return s
    except Exception as e:
        logger.error(f"Sinyal hatası: {e}")
        return {"fiyat": None, "rsi": None, "trend": "HATA",
                "rsi_asiri_satim":False,"rsi_asiri_alim":False,
                "macd_kesisim_al":False,"macd_kesisim_sat":False,
                "bb_alt_dokunu":False,"bb_ust_dokunu":False,"yuksek_hacim":False}

def interval_adi(iv):
    return {"1d":"Günlük","4h":"4 Saatlik","1h":"1 Saatlik","15m":"15 Dakikalık"}.get(iv, iv)

def interval_period(iv):
    return {"1d":"6mo","4h":"60d","1h":"30d","15m":"8d"}.get(iv, "1mo")

# ════════════════════════════════════════════
# 🧠  FİYAT HAFIZASI
# ════════════════════════════════════════════
def destek_direnc_bul(df):
    try:
        seviyeler = []
        n = len(df)
        for i in range(2, n - 2):
            y = float(df["High"].iloc[i])
            d = float(df["Low"].iloc[i])
            if (df["High"].iloc[i] > df["High"].iloc[i-1] and
                df["High"].iloc[i] > df["High"].iloc[i-2] and
                df["High"].iloc[i] > df["High"].iloc[i+1] and
                df["High"].iloc[i] > df["High"].iloc[i+2]):
                seviyeler.append({"seviye": y, "tip": "DİRENÇ", "hacim": float(df["Volume"].iloc[i])})
            if (df["Low"].iloc[i] < df["Low"].iloc[i-1] and
                df["Low"].iloc[i] < df["Low"].iloc[i-2] and
                df["Low"].iloc[i] < df["Low"].iloc[i+1] and
                df["Low"].iloc[i] < df["Low"].iloc[i+2]):
                seviyeler.append({"seviye": d, "tip": "DESTEK", "hacim": float(df["Volume"].iloc[i])})
        if not seviyeler:
            return []
        seviyeler.sort(key=lambda x: x["seviye"])
        birlesik = []
        i = 0
        while i < len(seviyeler):
            grup = [seviyeler[i]]
            j = i + 1
            while j < len(seviyeler):
                if abs(seviyeler[j]["seviye"] - grup[0]["seviye"]) / grup[0]["seviye"] * 100 <= SEVIYE_TOLERANS_PCT:
                    grup.append(seviyeler[j]); j += 1
                else:
                    break
            tipler = [g["tip"] for g in grup]
            birlesik.append({
                "seviye":      round(sum(g["seviye"] for g in grup) / len(grup), 2),
                "tip":         "DİRENÇ" if tipler.count("DİRENÇ") >= tipler.count("DESTEK") else "DESTEK",
                "test_sayisi": len(grup),
            })
            i = j
        return [s for s in birlesik if s["test_sayisi"] >= MIN_TEST_SAYISI]
    except Exception as e:
        logger.error(f"Destek/direnç hatası: {e}")
        return []

def seviye_istatistik(df, seviye):
    try:
        yukari = []; asagi = []
        for i in range(len(df) - ILERIYE_BAK_GUN - 1):
            fiyat = _f(df["Close"].iloc[i])
            if fiyat and abs(fiyat - seviye) / seviye * 100 <= SEVIYE_TOLERANS_PCT:
                gelecek = _f(df["Close"].iloc[i + ILERIYE_BAK_GUN])
                if gelecek:
                    degisim = (gelecek - fiyat) / fiyat * 100
                    (yukari if degisim > 0 else asagi).append(degisim)
        toplam = len(yukari) + len(asagi)
        if not toplam:
            return None
        return {
            "toplam":         toplam,
            "yukari_ihtimal": round(len(yukari) / toplam * 100, 1),
            "asagi_ihtimal":  round(len(asagi)  / toplam * 100, 1),
            "ort_yukari":     round(sum(yukari) / len(yukari), 2) if yukari else 0,
            "ort_asagi":      round(sum(asagi)  / len(asagi),  2) if asagi  else 0,
        }
    except:
        return None

# ════════════════════════════════════════════
# 🔮  PATTERN ANALİZİ
# ════════════════════════════════════════════
def pattern_analiz(df):
    try:
        kapanislar = [_f(v) for v in df["Close"].values if _f(v)]
        n = len(kapanislar)
        if n < 10:
            return None
        gecmis_gun = 3
        def dizi(idx):
            return "".join("U" if kapanislar[i+1] > kapanislar[i] else "D"
                           for i in range(idx - gecmis_gun, idx))
        son_dizi = dizi(n - 1)
        eslesme = []
        for i in range(gecmis_gun, n - 2):
            if dizi(i) == son_dizi:
                d = (kapanislar[i+1] - kapanislar[i]) / kapanislar[i] * 100
                eslesme.append(round(d, 2))
        if not eslesme:
            return {"dizi": list(son_dizi), "bulunan": 0}
        yukari = [x for x in eslesme if x > 0]
        asagi  = [x for x in eslesme if x <= 0]
        return {
            "dizi":         list(son_dizi),
            "bulunan":      len(eslesme),
            "yukari_pct":   round(len(yukari) / len(eslesme) * 100, 1),
            "asagi_pct":    round(len(asagi)  / len(eslesme) * 100, 1),
            "ort_yukari":   round(sum(yukari) / len(yukari), 2) if yukari else 0,
            "ort_asagi":    round(sum(asagi)  / len(asagi),  2) if asagi  else 0,
        }
    except Exception as e:
        logger.error(f"Pattern hatası: {e}")
        return None

# ════════════════════════════════════════════
# 🌐  FLASK API — Mini App için
# ════════════════════════════════════════════
app_flask = Flask(__name__)
CORS(app_flask)

@app_flask.route("/")
def index():
    return jsonify({"status": "MAKTK Bot API çalışıyor", "sembol": SEMBOL})

@app_flask.route("/api/veri")
def api_veri():
    try:
        df = veri_cek("1d", "2y")
        if df is None:
            return jsonify({"hata": "Veri alınamadı"}), 500

        df = indiktorleri_hesapla(df)
        s  = sinyal_uret(df, "1d")

        fiyat  = s["fiyat"]
        onceki = _f(df["Close"].iloc[-2])
        degisim_pct = ((fiyat - onceki) / onceki * 100) if onceki else 0

        # Zaman dilimleri
        tf_sonuclar = []
        for interval in ["1d", "4h", "1h", "15m"]:
            try:
                dft = veri_cek(interval, interval_period(interval))
                if dft is None:
                    continue
                dft = indiktorleri_hesapla(dft)
                st  = sinyal_uret(dft, interval)
                al  = sum([st["rsi_asiri_satim"], st["macd_kesisim_al"], st["bb_alt_dokunu"]])
                sat = sum([st["rsi_asiri_alim"],  st["macd_kesisim_sat"], st["bb_ust_dokunu"]])
                tf_sonuclar.append({
                    "ad":     interval_adi(interval),
                    "rsi":    round(st["rsi"], 1) if st["rsi"] else None,
                    "sinyal": "AL" if al > sat else ("SAT" if sat > al else "NÖTR"),
                    "trend":  st["trend"],
                })
            except:
                pass

        # Confluence
        conf_al  = sum(1 for t in tf_sonuclar if t["sinyal"] == "AL")
        conf_sat = sum(1 for t in tf_sonuclar if t["sinyal"] == "SAT")
        if s["rsi"] and s["rsi"] < RSI_ASIRI_SATIM: conf_al  += 1
        if s["rsi"] and s["rsi"] > RSI_ASIRI_ALIM:  conf_sat += 1

        # Seviyeler
        seviyeler_ham = destek_direnc_bul(df)
        seviyeler = []
        for sv in seviyeler_ham:
            ist = seviye_istatistik(df, sv["seviye"])
            if not ist:
                continue
            uzak = (sv["seviye"] - fiyat) / fiyat * 100 if fiyat else 0
            seviyeler.append({
                "tip":            sv["tip"],
                "fiyat":          sv["seviye"],
                "test":           sv["test_sayisi"],
                "yukari_ihtimal": ist["yukari_ihtimal"],
                "asagi_ihtimal":  ist["asagi_ihtimal"],
                "ort_yukari":     ist["ort_yukari"],
                "ort_asagi":      ist["ort_asagi"],
                "uzaklik":        round(uzak, 2),
            })
        seviyeler = sorted(seviyeler, key=lambda x: abs(x["uzaklik"]))[:6]

        # Pattern
        pattern = pattern_analiz(df)

        return jsonify({
            "fiyat":       round(fiyat, 2) if fiyat else None,
            "onceki":      round(onceki, 2) if onceki else None,
            "degisim_pct": round(degisim_pct, 2),
            "hacim":       int(s["hacim"]) if s["hacim"] else None,
            "hacim_ort":   int(s["hacim_ma20"]) if s["hacim_ma20"] else None,
            "rsi":         round(s["rsi"], 2) if s["rsi"] else None,
            "macd":        round(s["macd"], 4) if s["macd"] else None,
            "macd_signal": round(s["macd_signal"], 4) if s["macd_signal"] else None,
            "macd_hist":   round(s["macd_hist"], 4) if s["macd_hist"] else None,
            "ma20":        round(s["ma20"], 2) if s["ma20"] else None,
            "ma50":        round(s["ma50"], 2) if s["ma50"] else None,
            "bb_upper":    round(s["bb_upper"], 2) if s["bb_upper"] else None,
            "bb_lower":    round(s["bb_lower"], 2) if s["bb_lower"] else None,
            "borsa_acik":  borsa_acik_mi(),
            "conf_al":     conf_al,
            "conf_sat":    conf_sat,
            "tf":          tf_sonuclar,
            "seviyeler":   seviyeler,
            "pattern":     pattern,
            "guncelleme":  datetime.now().strftime("%H:%M:%S"),
        })

    except Exception as e:
        logger.error(f"API hatası: {e}")
        return jsonify({"hata": str(e)}), 500

def flask_baslat():
    app_flask.run(host="0.0.0.0", port=PORT, debug=False, use_reloader=False)

# ════════════════════════════════════════════
# 📨  BİLDİRİM & TELEGRAM KOMUTLARI
# ════════════════════════════════════════════
def bildirim_gonder_sync(mesaj):
    users = kullanicilari_getir()
    for user_id in users:
        try:
            async def _g(uid=user_id):
                try:
                    bot = Bot(token=TELEGRAM_TOKEN)
                    await bot.send_message(chat_id=uid, text=mesaj, parse_mode="HTML")
                except Exception as e:
                    logger.warning(f"Gönderilemedi {uid}: {e}")
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            loop.run_until_complete(_g())
            loop.close()
        except Exception as e:
            logger.error(f"Bildirim hatası: {e}")

async def cmd_start(update, context: ContextTypes.DEFAULT_TYPE):
    kullanici_kaydet(update.message.chat_id)
    acik = borsa_acik_mi()
    durum = "🟢 Borsa açık" if acik else f"🔴 Borsa kapalı (açılış: {sonraki_acilis()})"
    await update.message.reply_html(
        f"👋 <b>MAKTK Teknik Analiz Botu'na Hoş Geldin!</b>\n\n"
        f"📌 <b>MAKTK.IS</b> takip ediyorum.\n{durum}\n\n"
        f"<b>Komutlar:</b>\n"
        f"/analiz — 4 zaman dilimi analiz\n"
        f"/fiyat  — Anlık fiyat\n"
        f"/rapor  — Günlük rapor\n"
        f"/stop   — Bildirimleri durdur\n"
        f"/yardim — Tüm komutlar\n\n"
        f"⚠️ <i>Yatırım tavsiyesi değildir.</i>"
    )

async def cmd_stop(update, context: ContextTypes.DEFAULT_TYPE):
    kullanici_sil(update.message.chat_id)
    await update.message.reply_text("🔕 Bildirimler durduruldu. Tekrar için /start yaz.")

async def cmd_analiz(update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🔄 Analiz yapılıyor...")
    df = veri_cek("1d", "6mo")
    if df is None:
        await update.message.reply_text("❌ Veri alınamadı.")
        return
    df = indiktorleri_hesapla(df)
    mesajlar = []
    for interval in ["1d", "4h", "1h", "15m"]:
        dft = veri_cek(interval, interval_period(interval))
        if dft is None: continue
        dft = indiktorleri_hesapla(dft)
        s   = sinyal_uret(dft, interval)
        al  = sum([s["rsi_asiri_satim"], s["macd_kesisim_al"], s["bb_alt_dokunu"]])
        sat = sum([s["rsi_asiri_alim"],  s["macd_kesisim_sat"], s["bb_ust_dokunu"]])
        ozet = "🟢 AL" if al > sat else ("🔴 SAT" if sat > al else "⚪ NÖTR")
        f_str = f"{s['fiyat']:.2f} ₺" if s["fiyat"] else "—"
        r_str = f"{s['rsi']:.1f}"     if s["rsi"]   else "—"
        mesajlar.append(f"<b>{interval_adi(interval)}</b> — {ozet}\n  Fiyat:{f_str}  RSI:{r_str}\n  Trend:{s['trend']}")
    await update.message.reply_html(
        "📊 <b>MAKTK Anlık Analiz</b>\n" + "─"*28 + "\n" + "\n\n".join(mesajlar)
    )

async def cmd_fiyat(update, context: ContextTypes.DEFAULT_TYPE):
    df = veri_cek("1d", "5d")
    if df is None:
        await update.message.reply_text("❌ Fiyat alınamadı.")
        return
    fiyat  = _f(df["Close"].iloc[-1])
    hacim  = _f(df["Volume"].iloc[-1])
    yuksek = float(df["High"].iloc[-1])
    dusuk  = float(df["Low"].iloc[-1])
    await update.message.reply_html(
        f"💹 <b>MAKTK Anlık Fiyat</b>\n"
        f"  Fiyat  : <b>{fiyat:.2f} ₺</b>\n"
        f"  Yüksek : {yuksek:.2f} ₺  Düşük: {dusuk:.2f} ₺\n"
        f"  Hacim  : {hacim:,.0f}\n"
        f"  {'🟢 Borsa açık' if borsa_acik_mi() else '🔴 Borsa kapalı'}\n"
        f"  <i>{datetime.now().strftime('%H:%M:%S')}</i>"
    )

async def cmd_rapor(update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("📋 Rapor hazırlanıyor...")
    satirlar = [f"📋 <b>MAKTK Rapor</b> — {datetime.now().strftime('%d.%m.%Y %H:%M')}\n{'─'*28}"]
    for interval in ["1d", "4h", "1h", "15m"]:
        df = veri_cek(interval, interval_period(interval))
        if df is None: continue
        df = indiktorleri_hesapla(df)
        s  = sinyal_uret(df, interval)
        al  = sum([s["rsi_asiri_satim"], s["macd_kesisim_al"]])
        sat = sum([s["rsi_asiri_alim"],  s["macd_kesisim_sat"]])
        ozet = "🟢 AL" if al >= 2 else "🟡 ZAYIF AL" if al == 1 else "🔴 SAT" if sat >= 2 else "🟡 ZAYIF SAT" if sat == 1 else "⚪ NÖTR"
        f_str = f"{s['fiyat']:.2f}₺" if s["fiyat"] else "—"
        r_str = f"{s['rsi']:.1f}"    if s["rsi"]   else "—"
        satirlar.append(f"\n<b>{interval_adi(interval)}</b> | {ozet}\n  Fiyat:{f_str}  RSI:{r_str}  Trend:{s['trend']}")
    satirlar.append(f"\n{'─'*28}\n⚠️ <i>Yatırım tavsiyesi değildir.</i>")
    await update.message.reply_html("\n".join(satirlar))

async def cmd_senaryo(update, context: ContextTypes.DEFAULT_TYPE):
    senaryo_bekliyor[str(update.message.chat_id)] = True
    await update.message.reply_text("🔮 Hedef fiyatı gir (₺):\nÖrnek: 95.50")

async def mesaj_isle(update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = str(update.message.chat_id)
    if not senaryo_bekliyor.get(chat_id):
        return
    senaryo_bekliyor[chat_id] = False
    try:
        hedef = float(update.message.text.replace(",", ".").strip())
    except:
        await update.message.reply_text("❌ Geçersiz fiyat.")
        return
    df = veri_cek("1d", "2y")
    if df is None:
        await update.message.reply_text("❌ Veri alınamadı.")
        return
    df = indiktorleri_hesapla(df)
    s  = sinyal_uret(df, "1d")
    guncel = s["fiyat"]
    dk = (hedef - guncel) / guncel * 100
    yon  = "📈 Yukarı" if dk >= 0 else "📉 Aşağı"
    renk = "+" if dk >= 0 else ""
    t_rsi = min(100, max(0, (s["rsi"] or 50) + dk * 0.8))
    uyari = " ⚠️ Aşırı alım!" if t_rsi > 65 else " ⚠️ Aşırı satım!" if t_rsi < 35 else ""
    kapanislar = [_f(v) for v in df["Close"].values if _f(v)]
    gunluk = [abs((kapanislar[i+1]-kapanislar[i])/kapanislar[i]*100) for i in range(len(kapanislar)-1) if kapanislar[i]]
    ort_g = sum(gunluk)/len(gunluk) if gunluk else 1
    t_gun = abs(dk) / ort_g
    await update.message.reply_html(
        f"🔮 <b>Senaryo — MAKTK</b>\n\n"
        f"💹 Güncel : <b>{guncel:.2f} ₺</b>\n"
        f"🎯 Hedef  : <b>{hedef:.2f} ₺</b>\n"
        f"📐 Hareket: <b>{renk}{dk:.2f}%</b> {yon}\n\n"
        f"⏱ Tahmini süre: ~<b>{t_gun:.0f}</b> işlem günü\n"
        f"📊 Tahmini RSI : {t_rsi:.1f}{uyari}\n\n"
        f"⚠️ <i>Yatırım tavsiyesi değildir.</i>"
    )

async def cmd_yardim(update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_html(
        "📖 <b>MAKTK Bot Komutları</b>\n\n"
        "/analiz  — 4 zaman dilimi analizi\n"
        "/fiyat   — Anlık fiyat\n"
        "/rapor   — Günlük rapor\n"
        "/senaryo — Hedef fiyat analizi\n"
        "/stop    — Bildirimleri durdur\n\n"
        "<b>Otomatik Bildirimler:</b>\n"
        "• RSI/MACD sinyalleri\n"
        "• Hacim anomalisi\n"
        "• Destek/direnç yakınlaşması\n"
        "• Her sabah 09:05 rapor\n\n"
        "⚠️ <i>Yatırım tavsiyesi değildir.</i>"
    )

async def cmd_kullanicilar(update, context: ContextTypes.DEFAULT_TYPE):
    if str(update.message.chat_id) != YONETICI_ID:
        return
    users = kullanicilari_getir()
    await update.message.reply_text(f"👥 Toplam kullanıcı: {len(users)}")

# ════════════════════════════════════════════
# 🔁  OTOMATİK TARAMA
# ════════════════════════════════════════════
def sinyal_tara():
    if not borsa_acik_mi():
        logger.info("Borsa kapalı — tarama atlandı")
        return
    logger.info("Sinyal taraması başladı...")
    try:
        for interval in ["1d", "4h", "1h", "15m"]:
            try:
                df = veri_cek(interval, interval_period(interval))
                if df is None: continue
                df = indiktorleri_hesapla(df)
                s  = sinyal_uret(df, interval)
                ad = interval_adi(interval)
                f  = s["fiyat"] or 0
                for key, flag, tip in [
                    (f"rsi_al_{interval}",   s["rsi_asiri_satim"], "AL"),
                    (f"macd_al_{interval}",  s["macd_kesisim_al"], "AL"),
                    (f"macd_sat_{interval}", s["macd_kesisim_sat"],"SAT"),
                ]:
                    if flag and not son_sinyaller.get(key):
                        mesaj = (f"🟢 <b>AL SİNYALİ</b> [{ad}]\n📌 MAKTK: <b>{f:.2f} ₺</b>"
                                 if tip == "AL" else
                                 f"🔴 <b>SAT SİNYALİ</b> [{ad}]\n📌 MAKTK: <b>{f:.2f} ₺</b>")
                        bildirim_gonder_sync(mesaj + "\n⚠️ <i>Yatırım tavsiyesi değildir.</i>")
                        haftalik_sinyal_gecmisi.append({"yon": tip})
                    son_sinyaller[key] = flag
            except Exception as e:
                logger.error(f"Tarama hatası [{interval}]: {e}")

        df2y = veri_cek("1d", "2y")
        if df2y is None: return
        df2y = indiktorleri_hesapla(df2y)
        guncel = _f(df2y["Close"].iloc[-1])

        # Hacim anomalisi
        son_h = _f(df2y["Volume"].iloc[-1])
        ort_h = _f(df2y["Hacim_MA20"].iloc[-1])
        if son_h and ort_h and son_h > ort_h * ANOMALI_HACIM_KATI:
            kat = son_h / ort_h
            if not son_sinyaller.get("anomali"):
                bildirim_gonder_sync(
                    f"🚨 <b>HAClM ANOMALİSİ — MAKTK</b>\n"
                    f"💹 Fiyat: <b>{guncel:.2f} ₺</b>\n"
                    f"⚡ Hacim normalin <b>{kat:.1f}x</b> üzerinde!\n"
                    f"⚠️ <i>Yatırım tavsiyesi değildir.</i>"
                )
            son_sinyaller["anomali"] = True
        else:
            son_sinyaller["anomali"] = False

        # Fiyat alarmları
        for alarm, yukarimi in [(FIYAT_ALARM_YUKARI, True), (FIYAT_ALARM_ASAGI, False)]:
            if alarm <= 0: continue
            tetik = guncel >= alarm if yukarimi else guncel <= alarm
            key   = "alarm_yukari" if yukarimi else "alarm_asagi"
            if tetik and not son_sinyaller.get(key):
                bildirim_gonder_sync(
                    f"🔔 <b>FİYAT ALARMI</b>\nMAKTK: <b>{guncel:.2f} ₺</b>\n"
                    f"{'↑' if yukarimi else '↓'} {alarm:.2f} ₺ {'aşıldı' if yukarimi else 'altına düştü'}!"
                )
            son_sinyaller[key] = tetik

    except Exception as e:
        logger.error(f"Genel tarama hatası: {e}")

def gunluk_rapor_gonder():
    try:
        satirlar = [f"📋 <b>MAKTK Günlük Rapor</b> — {datetime.now().strftime('%d.%m.%Y')}\n{'─'*28}"]
        for interval in ["1d", "4h"]:
            df = veri_cek(interval, interval_period(interval))
            if df is None: continue
            df = indiktorleri_hesapla(df)
            s  = sinyal_uret(df, interval)
            al  = sum([s["rsi_asiri_satim"], s["macd_kesisim_al"]])
            sat = sum([s["rsi_asiri_alim"],  s["macd_kesisim_sat"]])
            ozet = "🟢 AL" if al >= 2 else "🟡 ZAYIF" if al == 1 else "🔴 SAT" if sat >= 2 else "⚪ NÖTR"
            satirlar.append(f"\n<b>{interval_adi(interval)}</b> | {ozet}\n  RSI:{s['rsi']:.1f if s['rsi'] else '—'}  Trend:{s['trend']}")
        satirlar.append(f"\n⚠️ <i>Yatırım tavsiyesi değildir.</i>")
        bildirim_gonder_sync("\n".join(satirlar))
    except Exception as e:
        logger.error(f"Rapor hatası: {e}")

def zamanlayici_baslat():
    schedule.every(KONTROL_SIKLIGI_DK).minutes.do(sinyal_tara)
    schedule.every().day.at(GUNLUK_RAPOR_SAATI).do(gunluk_rapor_gonder)
    getattr(schedule.every(), HAFTALIK_OZET_GUNU).at(HAFTALIK_OZET_SAAT).do(
        lambda: bildirim_gonder_sync("📅 <b>Haftalık özet hazırlanıyor...</b>")
    )
    logger.info("Zamanlayıcı aktif")
    while True:
        try:
            schedule.run_pending()
        except Exception as e:
            logger.error(f"Zamanlayıcı hatası: {e}")
        time.sleep(30)

# ════════════════════════════════════════════
# 🚀  BAŞLAT
# ════════════════════════════════════════════
def main():
    logger.info("🚀 MAKTK Bot v5 başlatılıyor...")
    kullanici_kaydet(YONETICI_ID)

    # Flask API thread
    threading.Thread(target=flask_baslat, daemon=True).start()
    logger.info(f"✅ Flask API başladı: port {PORT}")

    # Zamanlayıcı thread
    threading.Thread(target=zamanlayici_baslat, daemon=True).start()

    # İlk tarama
    threading.Thread(target=sinyal_tara, daemon=True).start()

    # Telegram bot
    telegram_app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    telegram_app.add_handler(CommandHandler("start",        cmd_start))
    telegram_app.add_handler(CommandHandler("stop",         cmd_stop))
    telegram_app.add_handler(CommandHandler("analiz",       cmd_analiz))
    telegram_app.add_handler(CommandHandler("fiyat",        cmd_fiyat))
    telegram_app.add_handler(CommandHandler("rapor",        cmd_rapor))
    telegram_app.add_handler(CommandHandler("senaryo",      cmd_senaryo))
    telegram_app.add_handler(CommandHandler("yardim",       cmd_yardim))
    telegram_app.add_handler(CommandHandler("kullanicilar", cmd_kullanicilar))
    telegram_app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, mesaj_isle))

    logger.info("✅ Bot aktif!")
    telegram_app.run_polling()

if __name__ == "__main__":
    main()
